//! One recording/shadow session of the joint step inside X4 (or inside a mapped image in
//! the offline tests). Owner-thread only: start, stats, write and stop are called from the
//! thread that runs the simulation (X4's UI Lua runs on the MoveWorker thread).

use std::ffi::c_void;
use std::sync::atomic::Ordering;

use super::patch::Patch;
use super::recorder::{self, Recorder, FOREIGN, OWNER, RECORDER, TRAMPOLINE};
use crate::capture::{self, Header, Source};
use crate::oracle::{rva, IK_ROTATE_TOWARD_HEAD};
use x4mp_core::pins::windows as pinned;
use x4mp_core::sha256;

/// The pinned first 16 bytes at 0xe1f840; the first 15 are five complete,
/// position-independent instructions (mov rax,rsp; two stores; push rbp/rdi/r12).
pub const SITE_BYTES: [u8; 16] =
    [0x48, 0x8b, 0xc4, 0x48, 0x89, 0x58, 0x10, 0x48, 0x89, 0x70, 0x18, 0x55, 0x57, 0x41, 0x54, 0x41];
pub const RELOCATED: usize = 15;

#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThreadId() -> u32;
    fn GetModuleHandleExW(flags: u32, name: *const u16, module: *mut *mut c_void) -> i32;
    fn VirtualQuery(address: *const c_void, info: *mut MemoryInfo, length: usize) -> usize;
}

#[repr(C)]
struct MemoryInfo {
    base: *mut c_void,
    allocation_base: *mut c_void,
    allocation_protect: u32,
    partition: u16,
    region_size: usize,
    state: u32,
    protect: u32,
    kind: u32,
}

const MEM_COMMIT: u32 = 0x1000;
const GET_MODULE_HANDLE_EX_FLAG_PIN: u32 = 0x1;
const GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS: u32 = 0x4;

fn readable(address: *const u8, length: usize) -> bool {
    let mut info = std::mem::MaybeUninit::<MemoryInfo>::uninit();
    // SAFETY: VirtualQuery only writes the info struct.
    if unsafe { VirtualQuery(address as *const c_void, info.as_mut_ptr(), std::mem::size_of::<MemoryInfo>()) } == 0 {
        return false;
    }
    let info = unsafe { info.assume_init() };
    let end = info.base as usize + info.region_size;
    info.state == MEM_COMMIT && info.protect & 0x101 == 0 && info.protect != 0 && address as usize + length <= end
}

/// How the image the session patches was verified.
pub enum Image {
    /// The running X4.exe: headers in memory and the file on disk against the pins.
    Process,
    /// A copy mapped by [`crate::oracle::Image`] (offline tests), already verified.
    Mapped(*mut u8),
}

pub struct Stats {
    pub calls: u64,
    pub recorded: usize,
    pub dropped: u64,
    pub foreign: u64,
    pub uninitialised_tls: u64,
    pub shadowed: u64,
    pub mismatched: u64,
    pub skipped_mode: u64,
    pub mxcsr: u32,
    pub mxcsr_changes: u64,
    pub mode: u8,
    pub failed: Option<&'static str>,
    pub first_mismatch: Option<String>,
}

pub struct Session {
    patch: Patch,
    recorder: *mut Recorder,
    base: *mut u8,
}

fn verify_process() -> Result<*mut u8, &'static str> {
    let mut module = std::ptr::null_mut();
    // SAFETY: null name = the process executable; no reference taken.
    if unsafe { GetModuleHandleExW(0x2, std::ptr::null(), &mut module) } == 0 || module.is_null() {
        return Err("no process image");
    }
    let base = module as *mut u8;
    if !readable(base, 0x1000) {
        return Err("image headers unreadable");
    }
    let headers = unsafe { std::slice::from_raw_parts(base, 0x1000) };
    let word = |at: usize| u32::from_le_bytes(headers[at..at + 4].try_into().unwrap());
    let nt = word(0x3c) as usize;
    if &headers[..2] != b"MZ" || nt > 0x800 || &headers[nt..nt + 4] != b"PE\0\0" {
        return Err("not a PE image");
    }
    if u16::from_le_bytes([headers[nt + 4], headers[nt + 5]]) != 0x8664
        || word(nt + 8) != pinned::PE_TIMESTAMP
        || word(nt + 24 + 56) != pinned::SIZE_OF_IMAGE
    {
        return Err("unsupported executable (headers)");
    }
    let exe = std::env::current_exe().map_err(|_| "executable path unavailable")?;
    let size = std::fs::metadata(&exe).map_err(|_| "executable unreadable")?.len();
    if size != pinned::FILE_SIZE {
        return Err("unsupported executable (size)");
    }
    match sha256::file_hex(&exe) {
        Ok(hex) if hex == pinned::FILE_SHA256 => Ok(base),
        _ => Err("unsupported executable (SHA-256)"),
    }
}

impl Session {
    /// Verify, prepare, publish the recorder and arm the detour, on the calling thread.
    pub fn start(image: Image, capacity: usize, record: bool, shadow: bool) -> Result<Session, &'static str> {
        if !RECORDER.load(Ordering::Acquire).is_null() {
            return Err("a session is already running");
        }
        let base = match image {
            Image::Process => verify_process()?,
            Image::Mapped(base) => base,
        };
        let site = unsafe { base.add(rva::IK_ROTATE_TOWARD as usize) };
        if !readable(site, 32) || !readable(unsafe { base.add(rva::JOINT_MODE as usize) }, 1) {
            return Err("pinned addresses unreadable");
        }
        let mut hash = sha256::Sha256::new();
        hash.update(unsafe { std::slice::from_raw_parts(site, 32) });
        if sha256::hex(&hash.finish()) != IK_ROTATE_TOWARD_HEAD {
            return Err("modified engine signature at 0xe1f840");
        }
        // Keep this module loaded for good: the detour lives here.
        let mut pinned_module = std::ptr::null_mut();
        let detour_fn: recorder::StepFn = recorder::detour;
        let anchor = detour_fn as *const u16;
        if unsafe {
            GetModuleHandleExW(
                GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                anchor,
                &mut pinned_module,
            )
        } == 0
        {
            return Err("cannot pin the module");
        }
        let mut patch = unsafe { Patch::prepare(site, SITE_BYTES, RELOCATED)? };
        let mode_byte = unsafe { base.add(rva::JOINT_MODE as usize) } as usize;
        let recorder = Box::into_raw(Box::new(Recorder::new(capacity, record, shadow, mode_byte)));
        TRAMPOLINE.store(patch.trampoline() as usize, Ordering::Release);
        OWNER.store(unsafe { GetCurrentThreadId() }, Ordering::Release);
        FOREIGN.store(0, Ordering::Relaxed);
        RECORDER.store(recorder, Ordering::Release);
        let detour: recorder::StepFn = recorder::detour;
        if let Err(reason) = unsafe { patch.arm(detour as usize) } {
            RECORDER.store(std::ptr::null_mut(), Ordering::Release);
            drop(unsafe { Box::from_raw(recorder) });
            return Err(reason);
        }
        Ok(Session { patch, recorder, base })
    }

    fn recorder(&self) -> &Recorder {
        // SAFETY: owner thread only, alive until stop.
        unsafe { &*self.recorder }
    }

    pub fn stats(&self) -> Stats {
        let r = self.recorder();
        Stats {
            calls: r.calls,
            recorded: r.records.len(),
            dropped: r.dropped,
            foreign: FOREIGN.load(Ordering::Relaxed),
            uninitialised_tls: r.uninitialised_tls,
            shadowed: r.shadowed,
            mismatched: r.mismatched,
            skipped_mode: r.skipped_mode,
            mxcsr: r.mxcsr,
            mxcsr_changes: r.mxcsr_changes,
            mode: unsafe { self.base.add(rva::JOINT_MODE as usize).read() },
            failed: r.failed.or(if self.patch.intact() { None } else { Some("patch bytes changed") }),
            first_mismatch: r.mismatches.first().map(|(record, ours, our_result)| {
                format!(
                    "original={:?}->{} rust={:?}->{} before={:?} command={:?} clock={:?}",
                    record.after, record.result, ours, our_result, record.before, record.command, record.clock
                )
            }),
        }
    }

    /// Records so far (owner thread).
    pub fn records(&self) -> &[capture::Record] {
        &self.recorder().records
    }

    /// Write the records as a text capture (`X4CJSTEP-HEX`), stopping further recording.
    pub fn write(&mut self, path: &std::path::Path) -> std::io::Result<usize> {
        let r = unsafe { &mut *self.recorder };
        r.record = false;
        let header = Header {
            steam_build: 23_660_954,
            mxcsr: r.mxcsr,
            joint_mode: unsafe { self.base.add(rva::JOINT_MODE as usize).read() },
            source: Source::Native,
        };
        let file = std::fs::File::create(path)?;
        capture::write_capture_hex(std::io::BufWriter::new(file), &header, &r.records)?;
        Ok(r.records.len())
    }

    /// Restore the site. The recorder is freed only when the bytes were restored.
    pub fn stop(mut self) -> Result<(), &'static str> {
        let result = self.patch.remove();
        if result.is_ok() {
            RECORDER.store(std::ptr::null_mut(), Ordering::Release);
            drop(unsafe { Box::from_raw(self.recorder) });
        }
        result
    }
}
