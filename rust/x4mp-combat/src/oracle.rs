//! Offline oracle: run single original functions of the pinned `X4.exe` in this process.
//!
//! [`Image::load`] maps a local copy of the executable the way the loader would place
//! its sections (no import binding, no TLS callbacks, no entry point, nothing of the
//! game runs), applies its base relocations, and verifies size and SHA-256 against
//! build 23660954 first; any mismatch is refused. Only self-contained leaf functions
//! may be called: code that reaches an import, the CRT's error paths or the game's
//! allocator would fault. The file is read from disk and never modified or copied.
//!
//! The engine reads its game clocks from the TLS block of executable TLS index 0
//! (`gs:[0x58]` -> slot 0). [`Image::call_ik_rotate_toward`] points the calling thread's
//! `ThreadLocalStoragePointer` at a private block for the duration of the call only.
//!
//! This is test tooling for the differential method of reports/REWRITE-PROGRAM.md: it
//! proves bit-identity against the original machine code offline. It is not a native
//! game result.

use std::arch::asm;
use std::ffi::c_void;
use std::path::{Path, PathBuf};

use crate::ikjoint::{FrameClock, JointCommand, JointState};
use x4mp_core::pins::windows as pinned;
use x4mp_core::sha256;

/// Pinned RVAs used by the oracle (build 23660954).
pub mod rva {
    /// `IKValue_RotateToward` (free function, joint in RCX).
    pub const IK_ROTATE_TOWARD: u32 = 0x00e1_f840;
    /// vbtable written by the `U::IKValue` constructor 0xe6acc0.
    pub const IKVALUE_VBTABLE: u32 = 0x02c4_50c0;
    /// `U::IKValue::vftable` of its `ValueSource<float>` virtual base at +0x678.
    pub const IKVALUE_VFTABLE: u32 = 0x02c4_50d0;
    /// Global byte selecting the sub-stepped joint branch (0 = kinematic default).
    pub const JOINT_MODE: u32 = 0x03d1_9b14;
    /// .rdata constants of the step.
    pub const TWO_PI: u32 = 0x02cb_ebdc;
    pub const PI: u32 = 0x02cb_eab4;
    pub const EPSILON: u32 = 0x02cb_e0ac;
    pub const BRAKE_MARGIN: u32 = 0x02cb_e550;
}

/// SHA-256 of the first 32 code bytes at [`rva::IK_ROTATE_TOWARD`] in the pinned file.
pub const IK_ROTATE_TOWARD_HEAD: &str = "205cd3d06b0fa29ba103a4cdfe381705a7143cb22499d950a90b3e7861320e11";

const IKVALUE_SIZE: usize = 0x680;
const IKVALUE_VBASE: usize = 0x678;
const TLS_BLOCK_SIZE: usize = 0x8000;

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

/// The default location of the RE workspace copy next to this checkout, or `X4MP_X4_EXE`.
pub fn default_exe() -> PathBuf {
    if let Some(path) = std::env::var_os("X4MP_X4_EXE") {
        return PathBuf::from(path);
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../x4-re/bin/X4.exe")
}

fn u16_at(d: &[u8], at: usize) -> Result<u16, String> {
    d.get(at..at + 2).map(|b| u16::from_le_bytes([b[0], b[1]])).ok_or_else(|| format!("truncated at {at:#x}"))
}
fn u32_at(d: &[u8], at: usize) -> Result<u32, String> {
    d.get(at..at + 4)
        .map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
        .ok_or_else(|| format!("truncated at {at:#x}"))
}
fn u64_at(d: &[u8], at: usize) -> Result<u64, String> {
    let lo = u32_at(d, at)? as u64;
    let hi = u32_at(d, at + 4)? as u64;
    Ok(lo | hi << 32)
}

/// A mapped copy of the pinned executable.
pub struct Image {
    base: *mut u8,
    size: usize,
}

// The mapping is never mutated after load except through explicit unsafe pokes in tests.
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
        let base = unsafe {
            VirtualAlloc(std::ptr::null_mut(), size_of_image, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE) as *mut u8
        };
        if base.is_null() {
            return Err("VirtualAlloc failed".into());
        }
        let image = Image { base, size: size_of_image };
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
            // Raw data beyond VirtualSize is file padding; memory beyond the raw data stays zero.
            let length = if virtual_size == 0 { raw_size } else { raw_size.min(virtual_size) };
            let length = length.min(size_of_image.saturating_sub(virtual_address));
            let source = file.get(raw_pointer..raw_pointer + length).ok_or("section outside file")?;
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
        unsafe { FlushInstructionCache(GetCurrentProcess(), base as *const c_void, size_of_image) };
        Ok(image)
    }

    /// Mapped address of `rva`.
    pub fn at(&self, rva: u32) -> *mut u8 {
        assert!((rva as usize) < self.size);
        unsafe { self.base.add(rva as usize) }
    }

    /// Mapped bytes at `rva`.
    pub fn bytes(&self, rva: u32, length: usize) -> &[u8] {
        assert!(rva as usize + length <= self.size);
        unsafe { std::slice::from_raw_parts(self.at(rva), length) }
    }

    /// The f32 stored at `rva`.
    pub fn f32_at(&self, rva: u32) -> f32 {
        let b = self.bytes(rva, 4);
        f32::from_le_bytes([b[0], b[1], b[2], b[3]])
    }

    /// SHA-256 (hex) of `length` mapped bytes at `rva`.
    pub fn head_sha256(&self, rva: u32, length: usize) -> String {
        let mut hash = sha256::Sha256::new();
        hash.update(self.bytes(rva, length));
        sha256::hex(&hash.finish())
    }

    /// The joint mode byte (0 = the kinematic branch reimplemented in [`crate::ikjoint`]).
    pub fn joint_mode(&self) -> u8 {
        self.bytes(rva::JOINT_MODE, 1)[0]
    }

    /// Run the original `IKValue_RotateToward` on a synthetic `U::IKValue` built from
    /// `state` and return the joint afterwards plus the function's result. Only the
    /// kinematic branch is supported (the mode byte must be 0).
    pub fn call_ik_rotate_toward(
        &self,
        state: &JointState,
        cmd: &JointCommand,
        clock: FrameClock,
    ) -> (JointState, bool) {
        assert_eq!(self.joint_mode(), 0, "sub-stepped joint branch is not callable offline");
        let mut joint = Aligned::<IKVALUE_SIZE>::zeroed();
        let base = self.base as u64;
        joint.put_u64(0x000, base + rva::IKVALUE_VBTABLE as u64);
        joint.put_f32(0x008, state.value);
        joint.put_f64(0x010, state.time);
        joint.put_f32(0x018, state.velocity);
        joint.put_f32(0x01c, state.target);
        joint.0[0x021] = state.wrap as u8;
        joint.put_u64(IKVALUE_VBASE, base + rva::IKVALUE_VFTABLE as u64);
        let mut tls = vec![0u8; TLS_BLOCK_SIZE + 64];
        let block = {
            let address = tls.as_mut_ptr() as usize;
            let aligned = (address + 63) & !63;
            unsafe { std::slice::from_raw_parts_mut(aligned as *mut u8, TLS_BLOCK_SIZE) }
        };
        block[0x10] = 1; // TLS initialised: skip __dyn_tls_on_demand_init
        block[0x300..0x308].copy_from_slice(&clock.now.to_le_bytes());
        block[0x328..0x330].copy_from_slice(&clock.previous.to_le_bytes());
        let slots: [*mut u8; 1] = [block.as_mut_ptr()];
        type Step = unsafe extern "C" fn(*mut u8, f32, f32, f32, f32, f32, f32, u8) -> u8;
        // SAFETY: the pinned code at this RVA was verified by `load` (file hash); the joint
        // blob carries the vbtable/vftable it dispatches through; the TLS slot array lives
        // until the pointer is restored.
        let result = unsafe {
            let step: Step = std::mem::transmute(self.at(rva::IK_ROTATE_TOWARD));
            let joint_ptr = joint.0.as_mut_ptr();
            with_tls_slots(slots.as_ptr(), || {
                step(
                    joint_ptr,
                    cmd.target,
                    cmd.min,
                    cmd.max,
                    cmd.max_speed,
                    cmd.accel,
                    cmd.feed_forward,
                    cmd.snap as u8,
                )
            })
        };
        let after = JointState {
            value: joint.get_f32(0x008),
            time: joint.get_f64(0x010),
            velocity: joint.get_f32(0x018),
            target: joint.get_f32(0x01c),
            wrap: joint.0[0x021] != 0,
        };
        (after, result != 0)
    }
}

impl Drop for Image {
    fn drop(&mut self) {
        unsafe { VirtualFree(self.base as *mut c_void, 0, MEM_RELEASE) };
    }
}

/// Point this thread's `ThreadLocalStoragePointer` (TEB+0x58) at `slots` while `call` runs.
///
/// # Safety
/// `call` must not use Rust thread-locals or anything else that reaches the thread's real
/// TLS (no panics, no allocation through TLS caches, no std I/O) and must not unwind.
unsafe fn with_tls_slots<R>(slots: *const *mut u8, call: impl FnOnce() -> R) -> R {
    let saved: usize;
    asm!("mov {}, gs:[0x58]", out(reg) saved, options(nostack, preserves_flags, readonly));
    asm!("mov gs:[0x58], {}", in(reg) slots, options(nostack, preserves_flags));
    let result = call();
    asm!("mov gs:[0x58], {}", in(reg) saved, options(nostack, preserves_flags));
    result
}

#[repr(C, align(16))]
struct Aligned<const N: usize>([u8; N]);

impl<const N: usize> Aligned<N> {
    fn zeroed() -> Box<Self> {
        Box::new(Aligned([0u8; N]))
    }
    fn put_u64(&mut self, at: usize, v: u64) {
        self.0[at..at + 8].copy_from_slice(&v.to_le_bytes());
    }
    fn put_f64(&mut self, at: usize, v: f64) {
        self.0[at..at + 8].copy_from_slice(&v.to_le_bytes());
    }
    fn put_f32(&mut self, at: usize, v: f32) {
        self.0[at..at + 4].copy_from_slice(&v.to_le_bytes());
    }
    fn get_f32(&self, at: usize) -> f32 {
        f32::from_le_bytes(self.0[at..at + 4].try_into().unwrap())
    }
    fn get_f64(&self, at: usize) -> f64 {
        f64::from_le_bytes(self.0[at..at + 8].try_into().unwrap())
    }
}
