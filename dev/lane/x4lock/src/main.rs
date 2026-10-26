//! x4lock: find and release the single-instance lock of a running X4.exe.
//!
//! X4 9.00 (build 23660954) WinMain calls
//!     CreateMutexW(NULL, TRUE, L"EGOSOFT_X4_INSTANCE")
//! and if the call fails or GetLastError() == ERROR_ALREADY_EXISTS it shows a
//! system-modal "Game is already running!" box and returns 50. The name has no
//! Global\ prefix, so it lives in the session namespace:
//!     \Sessions\<n>\BaseNamedObjects\EGOSOFT_X4_INSTANCE
//!
//! This tool closes the first instance's handle to that mutex from outside
//! (DuplicateHandle with DUPLICATE_CLOSE_SOURCE), so the named object goes away
//! and a second X4 can create it again. Nothing on disk is changed and no code
//! in X4 is patched.
//!
//! Safety rules (fail closed):
//!   * the target process image file name must be X4.exe (and match
//!     --expect-path exactly if given);
//!   * only a handle whose object type is "Mutant" and whose full object name is
//!     exactly \Sessions\<target session>\BaseNamedObjects\EGOSOFT_X4_INSTANCE
//!     (or \BaseNamedObjects\EGOSOFT_X4_INSTANCE in session 0) is closed;
//!   * anything else is refused.
//!
//! After closing, an unnamed, unowned mutex is duplicated into the target. If it
//! lands on the same handle value (the usual case, handle values are reused), the
//! ReleaseMutex/CloseHandle pair X4 runs on that value at shutdown hits a harmless
//! placeholder instead of whatever object might otherwise reuse the value.
//!
//! Exit codes: 0 ok, 1 usage, 2 refused (not X4 / path mismatch), 3 lock handle
//! not found, 4 OS error (open/duplicate failed), 5 name still present after close.

use std::ffi::c_void;
use std::time::{Duration, Instant};

type HANDLE = *mut c_void;
type BOOL = i32;
type NTSTATUS = i32;

const LOCK_NAME: &str = "EGOSOFT_X4_INSTANCE";

const PROCESS_DUP_HANDLE: u32 = 0x0040;
const PROCESS_QUERY_INFORMATION: u32 = 0x0400;
const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
const SYNCHRONIZE: u32 = 0x0010_0000;
const DUPLICATE_CLOSE_SOURCE: u32 = 0x1;
const DUPLICATE_SAME_ACCESS: u32 = 0x2;
const PROCESS_HANDLE_INFORMATION_CLASS: u32 = 51;
const OBJECT_NAME_INFORMATION: u32 = 1;
const OBJECT_TYPE_INFORMATION: u32 = 2;
const STATUS_INFO_LENGTH_MISMATCH: NTSTATUS = 0xC000_0004u32 as i32;
const STATUS_BUFFER_OVERFLOW: NTSTATUS = 0x8000_0005u32 as i32;
const STATUS_BUFFER_TOO_SMALL: NTSTATUS = 0xC000_0023u32 as i32;
const ERROR_FILE_NOT_FOUND: u32 = 2;

#[repr(C)]
struct UnicodeString {
    length: u16,
    maximum_length: u16,
    buffer: *const u16,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct HandleEntry {
    handle_value: usize,
    handle_count: usize,
    pointer_count: usize,
    granted_access: u32,
    object_type_index: u32,
    handle_attributes: u32,
    reserved: u32,
}

#[link(name = "kernel32")]
extern "system" {
    fn OpenProcess(access: u32, inherit: BOOL, pid: u32) -> HANDLE;
    fn CloseHandle(h: HANDLE) -> BOOL;
    fn GetCurrentProcess() -> HANDLE;
    fn GetLastError() -> u32;
    fn DuplicateHandle(
        src_proc: HANDLE,
        src: HANDLE,
        dst_proc: HANDLE,
        dst: *mut HANDLE,
        access: u32,
        inherit: BOOL,
        options: u32,
    ) -> BOOL;
    fn QueryFullProcessImageNameW(h: HANDLE, flags: u32, buf: *mut u16, size: *mut u32) -> BOOL;
    fn ProcessIdToSessionId(pid: u32, session: *mut u32) -> BOOL;
    fn GetCurrentProcessId() -> u32;
    fn CreateMutexW(attrs: *const c_void, owner: BOOL, name: *const u16) -> HANDLE;
    fn OpenMutexW(access: u32, inherit: BOOL, name: *const u16) -> HANDLE;
    fn K32EnumProcesses(pids: *mut u32, cb: u32, needed: *mut u32) -> BOOL;
}

#[link(name = "ntdll")]
extern "system" {
    fn NtQueryInformationProcess(
        h: HANDLE,
        class: u32,
        buf: *mut c_void,
        len: u32,
        ret: *mut u32,
    ) -> NTSTATUS;
    fn NtQueryObject(h: HANDLE, class: u32, buf: *mut c_void, len: u32, ret: *mut u32) -> NTSTATUS;
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

struct OwnedHandle(HANDLE);
impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { CloseHandle(self.0) };
        }
    }
}

fn image_path(proc: HANDLE) -> Option<String> {
    let mut buf = vec![0u16; 32768];
    let mut size = buf.len() as u32;
    if unsafe { QueryFullProcessImageNameW(proc, 0, buf.as_mut_ptr(), &mut size) } == 0 {
        return None;
    }
    Some(String::from_utf16_lossy(&buf[..size as usize]))
}

fn session_of(pid: u32) -> Option<u32> {
    let mut s = 0u32;
    if unsafe { ProcessIdToSessionId(pid, &mut s) } == 0 {
        None
    } else {
        Some(s)
    }
}

fn handle_snapshot(proc: HANDLE) -> Result<Vec<HandleEntry>, NTSTATUS> {
    let mut len: u32 = 64 * 1024;
    loop {
        let mut buf = vec![0u8; len as usize];
        let mut ret = 0u32;
        let st = unsafe {
            NtQueryInformationProcess(
                proc,
                PROCESS_HANDLE_INFORMATION_CLASS,
                buf.as_mut_ptr() as *mut c_void,
                len,
                &mut ret,
            )
        };
        if st == STATUS_INFO_LENGTH_MISMATCH || st == STATUS_BUFFER_TOO_SMALL {
            len = ret.max(len * 2) + 4096;
            continue;
        }
        if st < 0 {
            return Err(st);
        }
        let count = unsafe { *(buf.as_ptr() as *const usize) };
        let base = unsafe { buf.as_ptr().add(16) as *const HandleEntry };
        let max = (buf.len() - 16) / std::mem::size_of::<HandleEntry>();
        let n = count.min(max);
        let mut out = Vec::with_capacity(n);
        for i in 0..n {
            out.push(unsafe { std::ptr::read_unaligned(base.add(i)) });
        }
        return Ok(out);
    }
}

fn query_unicode(h: HANDLE, class: u32) -> Option<String> {
    let mut len: u32 = 1024;
    for _ in 0..4 {
        // u64 storage keeps the UNICODE_STRING aligned
        let mut buf = vec![0u64; (len as usize + 7) / 8];
        let mut ret = 0u32;
        let st = unsafe { NtQueryObject(h, class, buf.as_mut_ptr() as *mut c_void, len, &mut ret) };
        if st == STATUS_INFO_LENGTH_MISMATCH || st == STATUS_BUFFER_OVERFLOW || st == STATUS_BUFFER_TOO_SMALL {
            len = ret.max(len * 2);
            continue;
        }
        if st < 0 {
            return None;
        }
        let us = unsafe { &*(buf.as_ptr() as *const UnicodeString) };
        if us.buffer.is_null() || us.length == 0 {
            return Some(String::new());
        }
        let slice = unsafe { std::slice::from_raw_parts(us.buffer, us.length as usize / 2) };
        return Some(String::from_utf16_lossy(slice));
    }
    None
}

struct Found {
    value: usize,
    name: String,
    access: u32,
}

/// Look through all handles of the target; return mutant handles whose name ends
/// with the lock name (exact-name check is done by the caller).
fn find_lock_handles(proc: HANDLE) -> Result<Vec<Found>, String> {
    let entries = handle_snapshot(proc).map_err(|st| format!("NtQueryInformationProcess(ProcessHandleInformation) status=0x{:08x}", st as u32))?;
    let me = unsafe { GetCurrentProcess() };
    let mut found = Vec::new();
    // Resolve the type index of "Mutant" once by checking types lazily.
    let mut mutant_index: Option<u32> = None;
    for e in entries {
        if let Some(idx) = mutant_index {
            if e.object_type_index != idx {
                continue;
            }
        }
        let mut dup: HANDLE = std::ptr::null_mut();
        let ok = unsafe {
            DuplicateHandle(proc, e.handle_value as HANDLE, me, &mut dup, 0, 0, DUPLICATE_SAME_ACCESS)
        };
        if ok == 0 {
            continue;
        }
        let dup = OwnedHandle(dup);
        if mutant_index.is_none() {
            match query_unicode(dup.0, OBJECT_TYPE_INFORMATION) {
                Some(t) if t == "Mutant" => mutant_index = Some(e.object_type_index),
                _ => continue,
            }
        }
        // Type is Mutant here; querying a mutant's name cannot block.
        if let Some(name) = query_unicode(dup.0, OBJECT_NAME_INFORMATION) {
            if name.ends_with(LOCK_NAME) {
                found.push(Found { value: e.handle_value, name, access: e.granted_access });
            }
        }
    }
    Ok(found)
}

fn expected_names(session: u32) -> Vec<String> {
    if session == 0 {
        vec![format!("\\BaseNamedObjects\\{}", LOCK_NAME)]
    } else {
        vec![format!("\\Sessions\\{}\\BaseNamedObjects\\{}", session, LOCK_NAME)]
    }
}

fn lock_name_exists() -> bool {
    let n = wide(LOCK_NAME);
    let h = unsafe { OpenMutexW(SYNCHRONIZE, 0, n.as_ptr()) };
    if h.is_null() {
        let e = unsafe { GetLastError() };
        return e != ERROR_FILE_NOT_FOUND;
    }
    unsafe { CloseHandle(h) };
    true
}

fn list_x4_pids() -> Vec<(u32, String)> {
    let mut pids = vec![0u32; 8192];
    let mut needed = 0u32;
    if unsafe { K32EnumProcesses(pids.as_mut_ptr(), (pids.len() * 4) as u32, &mut needed) } == 0 {
        return vec![];
    }
    let n = needed as usize / 4;
    let mut out = vec![];
    for &pid in &pids[..n] {
        if pid == 0 {
            continue;
        }
        let h = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if h.is_null() {
            continue;
        }
        let h = OwnedHandle(h);
        if let Some(p) = image_path(h.0) {
            if is_x4_image(&p) {
                out.push((pid, p));
            }
        }
    }
    out
}

fn is_x4_image(path: &str) -> bool {
    path.rsplit('\\').next().map(|f| f.eq_ignore_ascii_case("X4.exe")).unwrap_or(false)
}

fn usage() -> ! {
    eprintln!(
        "usage:\n  x4lock list\n  x4lock query   --pid <pid>\n  x4lock release --pid <pid> [--expect-path <full X4.exe path>] [--wait-ms <ms>]"
    );
    std::process::exit(1);
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        usage();
    }
    let cmd = args[0].as_str();
    let mut pid: Option<u32> = None;
    let mut expect_path: Option<String> = None;
    let mut wait_ms: u64 = 0;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--pid" => {
                pid = args.get(i + 1).and_then(|v| v.parse().ok());
                i += 2;
            }
            "--expect-path" => {
                expect_path = args.get(i + 1).cloned();
                i += 2;
            }
            "--wait-ms" => {
                wait_ms = args.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or_else(|| usage());
                i += 2;
            }
            _ => usage(),
        }
    }

    if cmd == "list" {
        for (p, path) in list_x4_pids() {
            println!("x4 pid={} session={} path={}", p, session_of(p).map(|s| s.to_string()).unwrap_or("?".into()), path);
        }
        println!("lock_name_present_in_my_session={}", lock_name_exists());
        std::process::exit(0);
    }
    if cmd != "query" && cmd != "release" {
        usage();
    }
    let pid = pid.unwrap_or_else(|| usage());

    let proc = unsafe {
        OpenProcess(PROCESS_DUP_HANDLE | PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    };
    if proc.is_null() {
        eprintln!("error: OpenProcess({}) failed, GetLastError={}", pid, unsafe { GetLastError() });
        std::process::exit(4);
    }
    let proc = OwnedHandle(proc);

    let path = match image_path(proc.0) {
        Some(p) => p,
        None => {
            eprintln!("refused: cannot read image path of pid {}", pid);
            std::process::exit(2);
        }
    };
    println!("target pid={} path={}", pid, path);
    if !is_x4_image(&path) {
        eprintln!("refused: pid {} is not an X4.exe", pid);
        std::process::exit(2);
    }
    if let Some(exp) = &expect_path {
        if !exp.eq_ignore_ascii_case(&path) {
            eprintln!("refused: image path does not match --expect-path {}", exp);
            std::process::exit(2);
        }
    }
    let session = match session_of(pid) {
        Some(s) => s,
        None => {
            eprintln!("error: cannot read session of pid {}", pid);
            std::process::exit(4);
        }
    };
    let expected = expected_names(session);
    println!("target session={} expected_name={}", session, expected[0]);

    let deadline = Instant::now() + Duration::from_millis(wait_ms);
    let lock = loop {
        let found = match find_lock_handles(proc.0) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("error: {}", e);
                std::process::exit(4);
            }
        };
        for f in &found {
            println!(
                "handle value=0x{:x} access=0x{:x} name={} exact_match={}",
                f.value,
                f.access,
                f.name,
                expected.iter().any(|x| x == &f.name)
            );
        }
        let exact: Vec<&Found> = found.iter().filter(|f| expected.iter().any(|x| x == &f.name)).collect();
        if exact.len() == 1 {
            break (exact[0].value, exact[0].name.clone());
        }
        if exact.len() > 1 {
            eprintln!("refused: {} handles match; expected exactly one", exact.len());
            std::process::exit(2);
        }
        if Instant::now() >= deadline {
            eprintln!("not found: pid {} holds no handle named {}", pid, expected[0]);
            std::process::exit(3);
        }
        std::thread::sleep(Duration::from_millis(250));
    };

    if cmd == "query" {
        std::process::exit(0);
    }

    // Re-check the exact handle just before closing it (the value could in
    // theory have been closed and reused between enumeration and now).
    let me = unsafe { GetCurrentProcess() };
    {
        let mut dup: HANDLE = std::ptr::null_mut();
        if unsafe { DuplicateHandle(proc.0, lock.0 as HANDLE, me, &mut dup, 0, 0, DUPLICATE_SAME_ACCESS) } == 0 {
            eprintln!("error: re-check DuplicateHandle failed, GetLastError={}", unsafe { GetLastError() });
            std::process::exit(4);
        }
        let dup = OwnedHandle(dup);
        let t = query_unicode(dup.0, OBJECT_TYPE_INFORMATION).unwrap_or_default();
        let n = query_unicode(dup.0, OBJECT_NAME_INFORMATION).unwrap_or_default();
        if t != "Mutant" || n != lock.1 {
            eprintln!("refused: handle 0x{:x} changed (type={} name={})", lock.0, t, n);
            std::process::exit(2);
        }
        // our duplicate is dropped here so it does not keep the name alive
    }

    let ok = unsafe {
        DuplicateHandle(proc.0, lock.0 as HANDLE, std::ptr::null_mut(), std::ptr::null_mut(), 0, 0, DUPLICATE_CLOSE_SOURCE)
    };
    if ok == 0 {
        eprintln!("error: DuplicateHandle(DUPLICATE_CLOSE_SOURCE) failed, GetLastError={}", unsafe { GetLastError() });
        std::process::exit(4);
    }
    println!("closed handle value=0x{:x} in pid {}", lock.0, pid);

    // Placeholder: unnamed, unowned mutex at (ideally) the same handle value.
    let ph = unsafe { CreateMutexW(std::ptr::null(), 0, std::ptr::null()) };
    if !ph.is_null() {
        let ph = OwnedHandle(ph);
        let mut remote: HANDLE = std::ptr::null_mut();
        if unsafe { DuplicateHandle(me, ph.0, proc.0, &mut remote, 0, 0, DUPLICATE_SAME_ACCESS) } != 0 {
            if remote as usize == lock.0 {
                println!("placeholder unnamed mutex installed at 0x{:x}", lock.0);
            } else {
                unsafe {
                    DuplicateHandle(proc.0, remote, std::ptr::null_mut(), std::ptr::null_mut(), 0, 0, DUPLICATE_CLOSE_SOURCE)
                };
                println!("placeholder landed at 0x{:x} (not 0x{:x}); removed it", remote as usize, lock.0);
            }
        }
    }

    let my_session = session_of(unsafe { GetCurrentProcessId() });
    if my_session == Some(session) {
        if lock_name_exists() {
            eprintln!("error: {} still exists after close (another process holds it)", LOCK_NAME);
            std::process::exit(5);
        }
        println!("verified: {} no longer exists in session {}", LOCK_NAME, session);
    } else {
        println!("warning: target in session {} but x4lock in {:?}; name check skipped", session, my_session);
    }
    std::process::exit(0);
}
