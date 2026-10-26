//! Offline oracle: execute single original functions of the pinned `X4.exe` in this process.
//!
//! [`Image::load`] reads the executable, refuses anything but the pinned build (size, SHA-256,
//! PE timestamp, SizeOfImage), places its sections the way the loader would, applies the base
//! relocations and makes the code sections executable. Nothing of the game runs: no imports are
//! bound, no TLS callbacks, no entry point. Only self-contained functions may be called; code that
//! reaches an import, the allocator or the CRT's error paths would fault. The file is read, never
//! modified or copied.
//!
//! The engine reads its per-thread clocks and the view-cache selection from the TLS block of the
//! executable's TLS index 0 (`gs:[0x58]` -> slot 0: game time +0x300, cache selector +0x314).
//! [`with_tls_block`] points the calling thread's `ThreadLocalStoragePointer` at a private block
//! for the duration of one call only.
//!
//! Evidence produced with this module is OFFLINE (original machine code, synthetic inputs), not a
//! native game result. Same method as the combat agent's oracle (contrib/ferrite-rw-combat).

use std::arch::asm;
use std::ffi::c_void;
use std::path::{Path, PathBuf};

use x4mp_core::pins::windows as pinned;
use x4mp_core::sha256;

#[link(name = "kernel32")]
extern "system" {
    fn VirtualAlloc(address: *mut c_void, size: usize, allocation: u32, protect: u32) -> *mut c_void;
    fn VirtualFree(address: *mut c_void, size: usize, free_type: u32) -> i32;
    fn VirtualProtect(address: *mut c_void, size: usize, protect: u32, old: *mut u32) -> i32;
    fn FlushInstructionCache(process: *mut c_void, base: *const c_void, size: usize) -> i32;
    fn GetCurrentProcess() -> *mut c_void;
}

const MEM_COMMIT: u32 = 0x1000;
const MEM_RESERVE: u32 = 0x2000;
const MEM_RELEASE: u32 = 0x8000;
const PAGE_READWRITE: u32 = 0x04;
const PAGE_EXECUTE_READ: u32 = 0x20;
const SCN_MEM_EXECUTE: u32 = 0x2000_0000;

/// Size of the private TLS block handed to original code.
pub const TLS_BLOCK_SIZE: usize = 0x8000;

/// `X4MP_X4_EXE`, else the RE workspace copy next to the checkout (`../x4-re/bin/X4.exe`).
pub fn default_exe() -> PathBuf {
    if let Some(path) = std::env::var_os("X4MP_X4_EXE") {
        return PathBuf::from(path);
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../x4-re/bin/X4.exe")
}

fn rd<const N: usize>(d: &[u8], at: usize) -> Result<[u8; N], String> {
    d.get(at..at + N)
        .map(|b| {
            let mut out = [0u8; N];
            out.copy_from_slice(b);
            out
        })
        .ok_or_else(|| format!("truncated at {at:#x}"))
}
fn u16_at(d: &[u8], at: usize) -> Result<u16, String> {
    rd::<2>(d, at).map(u16::from_le_bytes)
}
fn u32_at(d: &[u8], at: usize) -> Result<u32, String> {
    rd::<4>(d, at).map(u32::from_le_bytes)
}
fn u64_at(d: &[u8], at: usize) -> Result<u64, String> {
    rd::<8>(d, at).map(u64::from_le_bytes)
}

/// A mapped, relocated copy of the pinned executable.
pub struct Image {
    base: *mut u8,
    size: usize,
}

// The mapping is read-only after load (code is execute-read; data is only read by the tests).
unsafe impl Send for Image {}
unsafe impl Sync for Image {}

impl Image {
    /// Map `path` after verifying it is exactly the pinned build.
    pub fn load(path: &Path) -> Result<Image, String> {
        let file = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
        if file.len() as u64 != pinned::FILE_SIZE {
            return Err(format!("{}: size {} is not the pinned {}", path.display(), file.len(), pinned::FILE_SIZE));
        }
        let mut hash = sha256::Sha256::new();
        hash.update(&file);
        let digest = sha256::hex(&hash.finish());
        if digest != pinned::FILE_SHA256 {
            return Err(format!("{}: SHA-256 {digest} is not the pinned build", path.display()));
        }
        if u16_at(&file, 0)? != 0x5a4d {
            return Err("no MZ header".into());
        }
        let nt = u32_at(&file, 0x3c)? as usize;
        if u32_at(&file, nt)? != 0x0000_4550 || u16_at(&file, nt + 4)? != 0x8664 {
            return Err("not a PE32+ x64 image".into());
        }
        if u32_at(&file, nt + 8)? != pinned::PE_TIMESTAMP {
            return Err("PE timestamp differs from the pinned build".into());
        }
        let sections = u16_at(&file, nt + 6)? as usize;
        let optional_size = u16_at(&file, nt + 20)? as usize;
        let optional = nt + 24;
        if u16_at(&file, optional)? != 0x20b {
            return Err("optional header is not PE32+".into());
        }
        let image_base = u64_at(&file, optional + 24)?;
        let size_of_image = u32_at(&file, optional + 56)? as usize;
        let size_of_headers = u32_at(&file, optional + 60)? as usize;
        if size_of_image as u32 != pinned::SIZE_OF_IMAGE {
            return Err("SizeOfImage differs from the pinned build".into());
        }
        let reloc_rva = u32_at(&file, optional + 112 + 5 * 8)? as usize;
        let reloc_size = u32_at(&file, optional + 112 + 5 * 8 + 4)? as usize;

        // SAFETY: plain allocation, released in Drop.
        let base =
            unsafe { VirtualAlloc(std::ptr::null_mut(), size_of_image, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE) }
                as *mut u8;
        if base.is_null() {
            return Err("VirtualAlloc failed".into());
        }
        let image = Image { base, size: size_of_image };
        // SAFETY: the allocation is size_of_image bytes and exclusively ours.
        let memory = unsafe { std::slice::from_raw_parts_mut(base, size_of_image) };
        memory[..size_of_headers].copy_from_slice(&file[..size_of_headers]);
        let table = optional + optional_size;
        let mut executable = Vec::new();
        for index in 0..sections {
            let header = table + index * 40;
            let virtual_size = u32_at(&file, header + 8)? as usize;
            let virtual_address = u32_at(&file, header + 12)? as usize;
            let raw_size = u32_at(&file, header + 16)? as usize;
            let raw_pointer = u32_at(&file, header + 20)? as usize;
            let characteristics = u32_at(&file, header + 36)?;
            let length = if virtual_size == 0 { raw_size } else { raw_size.min(virtual_size) };
            let length = length.min(size_of_image.saturating_sub(virtual_address));
            let source = file.get(raw_pointer..raw_pointer + length).ok_or("section outside the file")?;
            memory[virtual_address..virtual_address + length].copy_from_slice(source);
            if characteristics & SCN_MEM_EXECUTE != 0 {
                executable.push((virtual_address, virtual_size.max(length)));
            }
        }
        let delta = (base as u64).wrapping_sub(image_base);
        let mut offset = 0;
        while offset + 8 <= reloc_size {
            let page = u32_at(memory, reloc_rva + offset)? as usize;
            let block = u32_at(memory, reloc_rva + offset + 4)? as usize;
            if block < 8 {
                break;
            }
            for entry in (8..block).step_by(2) {
                let value = u16_at(memory, reloc_rva + offset + entry)?;
                match value >> 12 {
                    0 => {}
                    10 => {
                        let at = page + (value & 0x0fff) as usize;
                        let patched = u64_at(memory, at)?.wrapping_add(delta);
                        memory[at..at + 8].copy_from_slice(&patched.to_le_bytes());
                    }
                    other => return Err(format!("unsupported relocation type {other}")),
                }
            }
            offset += block;
        }
        for (address, length) in executable {
            let mut old = 0;
            // SAFETY: inside our own allocation.
            let ok = unsafe { VirtualProtect(base.add(address) as *mut c_void, length, PAGE_EXECUTE_READ, &mut old) };
            if ok == 0 {
                return Err("VirtualProtect failed".into());
            }
        }
        // SAFETY: our own mapping.
        unsafe { FlushInstructionCache(GetCurrentProcess(), base as *const c_void, size_of_image) };
        Ok(image)
    }

    /// Mapped address of `rva`.
    pub fn at(&self, rva: u32) -> *mut u8 {
        assert!((rva as usize) < self.size, "rva {rva:#x} outside the image");
        // SAFETY: bounds checked above.
        unsafe { self.base.add(rva as usize) }
    }

    /// Absolute address of `rva` (for values the original code compares with pointers).
    pub fn va(&self, rva: u32) -> u64 {
        self.at(rva) as u64
    }

    /// Mapped bytes at `rva`.
    pub fn bytes(&self, rva: u32, length: usize) -> &[u8] {
        assert!(rva as usize + length <= self.size);
        // SAFETY: bounds checked above; the mapping lives as long as self.
        unsafe { std::slice::from_raw_parts(self.at(rva), length) }
    }

    /// SHA-256 (hex) of `length` mapped bytes at `rva` (the native probe's pinned code hashes).
    pub fn sha256_at(&self, rva: u32, length: usize) -> String {
        let mut hash = sha256::Sha256::new();
        hash.update(self.bytes(rva, length));
        sha256::hex(&hash.finish())
    }

    /// The f32 / f64 stored at `rva` (.rdata constants).
    pub fn f32_at(&self, rva: u32) -> f32 {
        f32::from_le_bytes(self.bytes(rva, 4).try_into().unwrap())
    }
    pub fn f64_at(&self, rva: u32) -> f64 {
        f64::from_le_bytes(self.bytes(rva, 8).try_into().unwrap())
    }
}

impl Drop for Image {
    fn drop(&mut self) {
        // SAFETY: allocated by VirtualAlloc in load.
        unsafe { VirtualFree(self.base as *mut c_void, 0, MEM_RELEASE) };
    }
}

/// A zeroed, 64-byte aligned TLS block for original code (see the module docs).
pub struct TlsBlock {
    storage: Vec<u8>,
    offset: usize,
}

impl TlsBlock {
    pub fn new() -> TlsBlock {
        let storage = vec![0u8; TLS_BLOCK_SIZE + 64];
        let offset = (64 - (storage.as_ptr() as usize & 63)) & 63;
        let mut block = TlsBlock { storage, offset };
        block.bytes_mut()[0x10] = 1; // initialised: no __dyn_tls_on_demand_init
        block
    }
    pub fn bytes_mut(&mut self) -> &mut [u8] {
        &mut self.storage[self.offset..self.offset + TLS_BLOCK_SIZE]
    }
    pub fn set_f64(&mut self, at: usize, v: f64) {
        self.bytes_mut()[at..at + 8].copy_from_slice(&v.to_le_bytes());
    }
    pub fn set_u32(&mut self, at: usize, v: u32) {
        self.bytes_mut()[at..at + 4].copy_from_slice(&v.to_le_bytes());
    }
    fn ptr(&mut self) -> *mut u8 {
        self.bytes_mut().as_mut_ptr()
    }
}

impl Default for TlsBlock {
    fn default() -> Self {
        TlsBlock::new()
    }
}

/// Run `call` with this thread's `ThreadLocalStoragePointer` (TEB+0x58) pointing at a one-slot
/// array holding `block`.
///
/// # Safety
/// `call` must not touch Rust thread-locals or anything else reached through the thread's real TLS
/// (no panics, no allocation, no std I/O) and must not unwind.
pub unsafe fn with_tls_block<R>(block: &mut TlsBlock, call: impl FnOnce() -> R) -> R {
    let slots: [*mut u8; 1] = [block.ptr()];
    let saved: usize;
    asm!("mov {}, gs:[0x58]", out(reg) saved, options(nostack, preserves_flags, readonly));
    asm!("mov gs:[0x58], {}", in(reg) slots.as_ptr(), options(nostack, preserves_flags));
    let result = call();
    asm!("mov gs:[0x58], {}", in(reg) saved, options(nostack, preserves_flags));
    result
}

/// 16-byte aligned scratch object for original code (`this` blobs, vectors, matrices).
#[repr(C, align(16))]
pub struct Blob<const N: usize>(pub [u8; N]);

impl<const N: usize> Blob<N> {
    pub fn zeroed() -> Box<Self> {
        Box::new(Blob([0u8; N]))
    }
    pub fn ptr(&mut self) -> *mut u8 {
        self.0.as_mut_ptr()
    }
    pub fn put_u64(&mut self, at: usize, v: u64) {
        self.0[at..at + 8].copy_from_slice(&v.to_le_bytes());
    }
    pub fn put_u32(&mut self, at: usize, v: u32) {
        self.0[at..at + 4].copy_from_slice(&v.to_le_bytes());
    }
    pub fn put_f32(&mut self, at: usize, v: f32) {
        self.0[at..at + 4].copy_from_slice(&v.to_le_bytes());
    }
    pub fn put_f64(&mut self, at: usize, v: f64) {
        self.0[at..at + 8].copy_from_slice(&v.to_le_bytes());
    }
    pub fn get_f32(&self, at: usize) -> f32 {
        f32::from_le_bytes(self.0[at..at + 4].try_into().unwrap())
    }
    pub fn get_f64(&self, at: usize) -> f64 {
        f64::from_le_bytes(self.0[at..at + 8].try_into().unwrap())
    }
    pub fn get_u32(&self, at: usize) -> u32 {
        u32::from_le_bytes(self.0[at..at + 4].try_into().unwrap())
    }
    pub fn get_u64(&self, at: usize) -> u64 {
        u64::from_le_bytes(self.0[at..at + 8].try_into().unwrap())
    }
}

/// Pinned RVAs of original functions the flight tests call (build 23660954).
pub mod rva {
    /// `U::Path::ForcedFlightControlModel::Update` (vtable 0x2c4f7d0 slot +0x70).
    pub const FORCED_UPDATE: u32 = 0x00eb_04c0;
    /// Statically linked CRT `atan2f` / `asinf` called by the flight models.
    pub const ATAN2F: u32 = 0x01a1_a398;
    pub const ASINF: u32 = 0x01b4_3ce0;
}

/// SHA-256 of the first 32 code bytes at [`rva::FORCED_UPDATE`] (native/engine/signatures.py).
pub const FORCED_UPDATE_HEAD: &str = "ac3fd226dd09d17861cd229954eec4430efc62e1ecb057d26ea502a8d58cf140";

/// X4's own CRT math, executed from the mapped image (finite arguments only: the NaN error paths reach
/// the CRT's error handling, which is not initialised in this process).
pub struct MappedMath<'a> {
    atan2f: extern "C" fn(f32, f32) -> f32,
    asinf: extern "C" fn(f32) -> f32,
    _image: &'a Image,
}

impl<'a> MappedMath<'a> {
    pub fn new(image: &'a Image) -> MappedMath<'a> {
        // SAFETY: pinned build verified by Image::load; both are leaf math functions (Windows x64 ABI).
        unsafe {
            MappedMath {
                atan2f: std::mem::transmute::<*mut u8, extern "C" fn(f32, f32) -> f32>(image.at(rva::ATAN2F)),
                asinf: std::mem::transmute::<*mut u8, extern "C" fn(f32) -> f32>(image.at(rva::ASINF)),
                _image: image,
            }
        }
    }
}

impl crate::flight::CrtMath for MappedMath<'_> {
    fn atan2f(&self, y: f32, x: f32) -> f32 {
        (self.atan2f)(y, x)
    }
    fn asinf(&self, x: f32) -> f32 {
        (self.asinf)(x)
    }
}

impl Image {
    /// Run the original forced Update on a synthetic model object and a 0x1a0-byte slot image.
    /// `model` bytes: +0x20 target position, +0x30..+0x5f target rows; +0x10 is pointed at a private
    /// agent blob holding `max_forward` at +0x364. Returns the slot image afterwards.
    #[allow(clippy::too_many_arguments)]
    pub fn call_forced_update(&self, model: &[u8; 0x60], slot: &[u8; 0x1a0], dt: f32, t: f64, cheap: u8, p: u8,
                              max_forward: f32) -> [u8; 0x1a0] {
        let mut agent = Blob::<0x400>::zeroed();
        agent.put_f32(0x364, max_forward);
        let mut m = Blob::<0x60>::zeroed();
        m.0.copy_from_slice(model);
        m.put_u64(0x10, agent.ptr() as u64);
        let mut s = Blob::<0x1a0>::zeroed();
        s.0.copy_from_slice(slot);
        type Update = extern "C" fn(*mut u8, *mut u8, f32, f64, u8, u8);
        // SAFETY: pinned code (file hash checked at load); the function reads the model, the agent field
        // and the slot only, and calls the two CRT leaf functions.
        unsafe {
            let update = std::mem::transmute::<*mut u8, Update>(self.at(rva::FORCED_UPDATE));
            update(m.ptr(), s.ptr(), dt, t, cheap, p);
        }
        s.0
    }
}
