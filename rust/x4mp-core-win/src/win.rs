//! The Windows-only C ABI. See the crate documentation.

use std::ffi::c_void;
use std::panic::catch_unwind;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::Mutex;
use std::time::Instant;

use x4mp_core::config::{self, Parsed};
use x4mp_core::ffi::{Api, VerifiedBuild};
use x4mp_core::frame::FrameInput;
use x4mp_core::logbuf::LogBuf;
use x4mp_core::pins::{self, Platform};
use x4mp_core::resolve::MainModule;
use x4mp_core::runtime;
use x4mp_core::sha256;

use crate::code;
use crate::pe::{check_headers, HeaderCheck};

#[link(name = "kernel32")]
extern "system" {
    fn GetModuleHandleW(name: *const u16) -> *mut c_void;
    fn GetModuleFileNameW(module: *mut c_void, path: *mut u16, size: u32) -> u32;
    fn GetModuleHandleExW(flags: u32, name: *const u16, module: *mut *mut c_void) -> i32;
}

const GET_MODULE_HANDLE_EX_FLAG_PIN: u32 = 1;
const GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS: u32 = 4;

static STARTED: AtomicI32 = AtomicI32::new(code::NOT_STARTED);
static RUNNING: AtomicBool = AtomicBool::new(false);
static LOG: Mutex<Option<LogBuf>> = Mutex::new(None);

fn epoch() -> &'static Instant {
    static EPOCH: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    EPOCH.get_or_init(Instant::now)
}

fn log(line: String) {
    let mut guard = LOG.lock().unwrap_or_else(|p| p.into_inner());
    guard.get_or_insert_with(LogBuf::default).push(line);
}

fn verify_build() -> Result<(), String> {
    // SAFETY: null = the process's main module; its headers are mapped readable.
    let base = unsafe { GetModuleHandleW(std::ptr::null()) } as usize;
    if base == 0 {
        return Err("no_main_module".into());
    }
    let headers = unsafe { std::slice::from_raw_parts(base as *const u8, 0x1000) };
    match check_headers(headers) {
        HeaderCheck::Ok => {}
        other => return Err(format!("pe_header:{other:?}")),
    }
    let mut path = vec![0u16; 32768];
    // SAFETY: buffer of the stated length.
    let len = unsafe { GetModuleFileNameW(std::ptr::null_mut(), path.as_mut_ptr(), path.len() as u32) } as usize;
    if len == 0 || len >= path.len() {
        return Err("module_path".into());
    }
    let path = String::from_utf16_lossy(&path[..len]);
    let data = std::fs::read(&path).map_err(|e| format!("read_executable:{e}"))?;
    if data.len() as u64 != pins::windows::FILE_SIZE {
        return Err(format!("file_size:{}", data.len()));
    }
    let mut hasher = sha256::Sha256::new();
    hasher.update(&data);
    let digest = sha256::hex(&hasher.finish());
    if digest != pins::windows::FILE_SHA256 {
        return Err(format!("file_sha256:{digest}"));
    }
    Ok(())
}

fn pin_self() -> bool {
    let mut module = std::ptr::null_mut();
    // SAFETY: address inside this DLL; PIN keeps it loaded for the process lifetime.
    unsafe {
        GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_PIN | GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
            x4mp_core_win_start as *const () as *const u16,
            &mut module,
        ) != 0
    }
}

fn start() -> i32 {
    match config::parse(|key| std::env::var(key).ok()) {
        Parsed::Disabled => {
            log(format!("FEATURES process=x4mp-core-win {}", runtime::features_fields_disabled("off")));
            code::SWITCHED_OFF
        }
        Parsed::Invalid(reason) => {
            log(format!("FEATURE x4mp_core DISABLED reason=invalid_config:{reason}"));
            log(format!(
                "FEATURES process=x4mp-core-win {}",
                runtime::features_fields_disabled("auto-disabled:invalid_config")
            ));
            code::INVALID_CONFIG
        }
        Parsed::Enabled(core_config) => {
            if let Err(reason) = verify_build() {
                log(format!("FEATURE x4mp_core DISABLED reason=build_refused:{reason}"));
                log(format!(
                    "FEATURES process=x4mp-core-win {}",
                    runtime::features_fields_disabled("auto-disabled:build_refused")
                ));
                return code::WRONG_BUILD;
            }
            if !pin_self() {
                log("FEATURE x4mp_core DISABLED reason=cannot_pin_module".into());
                return code::INSTALL_FAILED;
            }
            let Some(source) = MainModule::new() else {
                log("FEATURE x4mp_core DISABLED reason=no_main_module".into());
                return code::INSTALL_FAILED;
            };
            // SAFETY: verify_build() matched PE headers, size and SHA-256 of X4.exe.
            let build = unsafe { VerifiedBuild::assert_verified(Platform::Windows, pins::STEAM_BUILD) };
            let (api, report) = Api::bind(&source, &build);
            match runtime::install(&core_config, api, &report, Platform::Windows) {
                Ok(()) => {
                    epoch();
                    RUNNING.store(true, Ordering::SeqCst);
                    let fields = runtime::features().unwrap_or_default();
                    log(format!("FEATURES process=x4mp-core-win {fields}"));
                    code::OK
                }
                Err(reason) => {
                    log(format!("FEATURE x4mp_core DISABLED reason={reason}"));
                    code::INSTALL_FAILED
                }
            }
        }
    }
}

#[no_mangle]
pub extern "C" fn x4mp_core_win_start() -> i32 {
    // Idempotent: the first result is remembered.
    let previous = STARTED.load(Ordering::SeqCst);
    if previous != code::NOT_STARTED {
        return previous;
    }
    let result = catch_unwind(start).unwrap_or(code::INSTALL_FAILED);
    STARTED.store(result, Ordering::SeqCst);
    result
}

#[no_mangle]
pub extern "C" fn x4mp_core_win_frame(frame: u64, game_time: f64, dt: f64, paused: i32) -> i32 {
    if !RUNNING.load(Ordering::Acquire) {
        return code::NOT_STARTED;
    }
    let input =
        FrameInput { frame, game_time, dt, paused: paused != 0, monotonic_ns: epoch().elapsed().as_nanos() as u64 };
    let result = catch_unwind(|| runtime::frame(&input)).unwrap_or(Err("panic"));
    match result {
        Ok(()) => code::OK,
        Err(reason) => {
            RUNNING.store(false, Ordering::SeqCst);
            runtime::drain_log(log);
            runtime::uninstall();
            log(format!("FEATURE x4mp_core DISABLED reason={reason}"));
            code::REFUSED
        }
    }
}

#[no_mangle]
pub extern "C" fn x4mp_core_md_take(buf: *mut u8, cap: usize) -> isize {
    if buf.is_null() || cap == 0 || cap > 1 << 20 {
        return runtime::BRIDGE_BAD_ARGUMENT;
    }
    catch_unwind(|| {
        // SAFETY: the Lua caller passes `cap` writable bytes it owns.
        runtime::md_take(unsafe { std::slice::from_raw_parts_mut(buf, cap) })
    })
    .unwrap_or(runtime::BRIDGE_BAD_ARGUMENT)
}

#[no_mangle]
pub extern "C" fn x4mp_core_md_result(id: u64, status: i32) -> isize {
    catch_unwind(|| runtime::md_result(id, status)).unwrap_or(runtime::BRIDGE_BAD_ARGUMENT)
}

/// Pop one log line (core lines are moved over first). Returns its length or 0 when
/// none; a line longer than `cap` is truncated (lines are at most 1024 bytes).
#[no_mangle]
pub extern "C" fn x4mp_core_log_take(buf: *mut u8, cap: usize) -> isize {
    if buf.is_null() || cap == 0 || cap > 1 << 20 {
        return runtime::BRIDGE_BAD_ARGUMENT;
    }
    catch_unwind(|| {
        let mut moved = Vec::new();
        runtime::drain_log(|line| moved.push(line));
        let mut guard = LOG.lock().unwrap_or_else(|p| p.into_inner());
        let log = guard.get_or_insert_with(LogBuf::default);
        for line in moved {
            log.push(line);
        }
        let Some(line) = log.pop() else { return 0 };
        if line.len() > cap {
            let mut cut = cap;
            while !line.is_char_boundary(cut) {
                cut -= 1;
            }
            // SAFETY: `cap` writable bytes.
            unsafe { std::ptr::copy_nonoverlapping(line.as_ptr(), buf, cut) };
            return cut as isize;
        }
        unsafe { std::ptr::copy_nonoverlapping(line.as_ptr(), buf, line.len()) };
        line.len() as isize
    })
    .unwrap_or(runtime::BRIDGE_BAD_ARGUMENT)
}
