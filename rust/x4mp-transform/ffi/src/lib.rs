//! C ABI over `x4mp-transform` for the in-game view-cache shadow.
//!
//! `x4lan_xform_64.dll` (native/engine/xform_probe.c) loads this library with `LoadLibraryW`
//! and, on shadow frames, hands it a complete in-memory capture (`X4XFCAP1`: everything the
//! original `BuildCache` could read, then what it changed). The model replays the frame and
//! compares every captured component. It has no hooks of its own; the check only reads the
//! buffer it is given, on the calling thread. The switch functions (called by the probe in place
//! of `BuildCache`) read engine memory and call the engine functions in `EngineApi`.
//!
//! ```c
//! uint32_t x4mp_xform_abi(void);   // 3
//! int32_t  x4mp_xform_check(const uint8_t *capture, size_t len, XformCheck *out);
//!          // 0 checked (out filled), -1 null argument, -2 unreadable capture, -3 internal panic
//! int32_t  x4mp_xform_build(void *galaxy, uint32_t idx, double time, uint32_t threads,
//!                           uint32_t drop_pose_blocks, const EngineApi *api, XformBuild *out);
//!          // the switch: 0 executed; <0 nothing executed, run the original BuildCache
//! int32_t  x4mp_xform_build_fast(void *galaxy, uint32_t idx, double time, uint32_t threads,
//!                                const EngineApi *api, XformBuild *out);
//!          // the fast switch (exact walk on engine memory, fast.rs); same results
//! ```

use std::panic::{catch_unwind, AssertUnwindSafe};
use x4mp_transform::capture::{replay, Capture, Field};

pub mod engine;
pub mod fast;
mod pool;

/// Result of [`x4mp_xform_build`] (C layout).
#[repr(C)]
pub struct XformBuild {
    /// Components copied.
    pub components: u64,
    /// Sector tasks used while loading.
    pub tasks: u64,
    /// Refresh calls executed.
    pub refreshes: u64,
    /// Lists whose timers were written.
    pub timers: u64,
    /// Entries removed.
    pub removed: u64,
    /// Load and plan time.
    pub plan_us: f64,
    /// Execution time.
    pub exec_us: f64,
    /// Reason when refused, NUL-terminated.
    pub message: [u8; 200],
}

/// `BuildCache(galaxy, idx, time)` done by us (the switch; see [`engine`]): loads a copy of what
/// the build reads (`threads` > 1: sectors on worker threads), plans with the model, executes the
/// plan through `api`. Returns 0 when executed; -1 null argument, -2 refused, -3 internal panic,
/// in which cases nothing was executed and the caller must run the original `BuildCache`.
///
/// # Safety
/// Only from the probe's hook on the MoveWorker, in place of the original call, with `api`
/// pointing at the engine's verified `PropagateCacheFlags` and `RemoveCacheSpecification`.
#[no_mangle]
pub unsafe extern "C" fn x4mp_xform_build(
    galaxy: *mut u8,
    idx: u32,
    time: f64,
    threads: u32,
    drop_pose_blocks: u32,
    api: *const engine::EngineApi,
    out: *mut XformBuild,
) -> i32 {
    if galaxy.is_null() || api.is_null() || out.is_null() || idx > 1 {
        return -1;
    }
    let out = unsafe { &mut *out };
    let api = unsafe { *api };
    let t0 = std::time::Instant::now();
    let planned = catch_unwind(AssertUnwindSafe(|| unsafe {
        engine::plan(galaxy as usize, idx as usize, time, threads.max(1) as usize, drop_pose_blocks != 0)
    }));
    let plan = match planned {
        Ok(Ok(p)) => p,
        Ok(Err(why)) => {
            put_text(&mut out.message, why);
            return -2;
        }
        Err(_) => {
            put_text(&mut out.message, "internal panic while planning");
            return -3;
        }
    };
    let t1 = std::time::Instant::now();
    let stats = unsafe { engine::execute(&plan, idx as usize, &api) };
    out.components = stats.components as u64;
    out.tasks = stats.tasks as u64;
    out.refreshes = stats.refreshes as u64;
    out.timers = stats.timers as u64;
    out.removed = stats.removed as u64;
    out.plan_us = (t1 - t0).as_secs_f64() * 1e6;
    out.exec_us = t1.elapsed().as_secs_f64() * 1e6;
    out.message[0] = 0;
    0
}

/// `BuildCache(galaxy, idx, time)` done by the fast switch ([`fast`]): the exact walk on engine
/// memory (sector subtrees on `threads` threads), then the planned calls in walk order. Returns
/// as [`x4mp_xform_build`]: 0 executed; -1 null argument, -2 refused, -3 internal panic, in which
/// cases nothing was executed and the caller must run the original `BuildCache`.
///
/// # Safety
/// As [`x4mp_xform_build`]; `api.base_flags` must point at the engine's base flags 0x3d72b8c.
#[no_mangle]
pub unsafe extern "C" fn x4mp_xform_build_fast(
    galaxy: *mut u8,
    idx: u32,
    time: f64,
    threads: u32,
    api: *const engine::EngineApi,
    out: *mut XformBuild,
) -> i32 {
    if galaxy.is_null() || api.is_null() || out.is_null() || idx > 1 {
        return -1;
    }
    let out = unsafe { &mut *out };
    let api = unsafe { *api };
    if api.base_flags.is_null() {
        return -1;
    }
    let base = unsafe { std::ptr::read_volatile(api.base_flags) };
    let t0 = std::time::Instant::now();
    let planned = catch_unwind(AssertUnwindSafe(|| unsafe {
        fast::plan(galaxy as usize, idx as usize, time, threads.max(1) as usize, base)
    }));
    let plan = match planned {
        Ok(Ok(p)) => p,
        Ok(Err(why)) => {
            put_text(&mut out.message, why);
            return -2;
        }
        Err(_) => {
            put_text(&mut out.message, "internal panic while planning");
            return -3;
        }
    };
    let t1 = std::time::Instant::now();
    let stats = unsafe { fast::execute(&plan, idx as usize, &api) };
    out.components = stats.components as u64;
    out.tasks = stats.tasks as u64;
    out.refreshes = stats.refreshes as u64;
    out.timers = stats.timers as u64;
    out.removed = stats.removed as u64;
    out.plan_us = (t1 - t0).as_secs_f64() * 1e6;
    out.exec_us = t1.elapsed().as_secs_f64() * 1e6;
    out.message[0] = 0;
    0
}

fn put_text(buf: &mut [u8; 200], text: &str) {
    let b = text.as_bytes();
    let n = b.len().min(buf.len() - 1);
    buf[..n].copy_from_slice(&b[..n]);
    buf[n] = 0;
}

/// Result of one shadow check (C layout).
#[repr(C)]
pub struct XformCheck {
    /// Captured components.
    pub components: u64,
    /// BuildCache entries the model made.
    pub visits: u64,
    /// Gates open.
    pub open: u64,
    /// Refresh calls.
    pub refreshes: u64,
    /// Refresh calls with work.
    pub with_work: u64,
    /// Pose getter calls.
    pub poses: u64,
    /// Velocity getter calls.
    pub velocities: u64,
    /// Attachment getter calls.
    pub connections: u64,
    /// Expired specification entries.
    pub expired: u64,
    /// Differences other than reference counts.
    pub mismatches: u64,
    /// Reference-count differences (other threads may change them).
    pub refs_only: u64,
    /// Field of the first difference (`Field` as u32 + 1; 0 = none).
    pub first_field: u32,
    /// Captured node of the first difference.
    pub first_node: u32,
    /// UniverseID of the first difference.
    pub first_id: u64,
    /// First difference as text, NUL-terminated.
    pub message: [u8; 200],
}

/// ABI version (2: adds `x4mp_xform_build`; 3: `EngineApi.base_flags`, `x4mp_xform_build_fast`).
#[no_mangle]
pub extern "C" fn x4mp_xform_abi() -> u32 {
    3
}

fn field_code(f: Field) -> u32 {
    1 + match f {
        Field::Flags => 0,
        Field::Count => 1,
        Field::Dirty => 2,
        Field::OtherDirty => 3,
        Field::Attention => 4,
        Field::Parent => 5,
        Field::Spec => 6,
        Field::Refs => 7,
        Field::Gate => 8,
        Field::TraceRefresh => 9,
        Field::TraceVisit => 10,
    }
}

fn put_message(out: &mut XformCheck, text: &str) {
    let b = text.as_bytes();
    let n = b.len().min(out.message.len() - 1);
    out.message[..n].copy_from_slice(&b[..n]);
    out.message[n] = 0;
}

/// Replays one capture and compares (see the module docs).
///
/// # Safety
/// `capture` must point to `len` readable bytes and `out` to a writable `XformCheck`.
#[no_mangle]
pub unsafe extern "C" fn x4mp_xform_check(capture: *const u8, len: usize, out: *mut XformCheck) -> i32 {
    if capture.is_null() || out.is_null() {
        return -1;
    }
    // SAFETY: the caller guarantees both pointers (see above).
    let bytes = unsafe { std::slice::from_raw_parts(capture, len) };
    let out = unsafe { &mut *out };
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<(), String> {
        let c = Capture::parse(bytes).map_err(|e| e.to_string())?;
        let r = replay(&c);
        let s = &r.census;
        out.components = s.components as u64;
        out.visits = s.visits as u64;
        out.open = s.open as u64;
        out.refreshes = s.refreshes as u64;
        out.with_work = s.refreshes_with_work as u64;
        out.poses = s.poses as u64;
        out.velocities = s.velocities as u64;
        out.connections = s.connections as u64;
        out.expired = s.expired as u64;
        let mut real = r.mismatches.iter().filter(|m| m.field != Field::Refs);
        out.refs_only = r.mismatches.iter().filter(|m| m.field == Field::Refs).count() as u64;
        out.mismatches = real.clone().count() as u64;
        out.first_field = 0;
        out.first_node = 0;
        out.first_id = 0;
        out.message[0] = 0;
        if let Some(m) = real.next() {
            out.first_field = field_code(m.field);
            out.first_node = m.node as u32;
            out.first_id = c.ids.get(m.node).copied().unwrap_or(0);
            put_message(out, &format!("{:?} node {} model {} engine {}", m.field, m.node, m.model, m.engine));
        }
        Ok(())
    }));
    match result {
        Ok(Ok(())) => 0,
        Ok(Err(e)) => {
            put_message(out, &e);
            -2
        }
        Err(_) => {
            put_message(out, "internal panic");
            -3
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blank() -> XformCheck {
        XformCheck {
            components: 0,
            visits: 0,
            open: 0,
            refreshes: 0,
            with_work: 0,
            poses: 0,
            velocities: 0,
            connections: 0,
            expired: 0,
            mismatches: 0,
            refs_only: 0,
            first_field: 0,
            first_node: 0,
            first_id: 0,
            message: [0; 200],
        }
    }

    #[test]
    fn rejects_null_and_garbage() {
        let mut out = blank();
        assert_eq!(unsafe { x4mp_xform_check(std::ptr::null(), 0, &mut out) }, -1);
        let junk = [1u8, 2, 3];
        assert_eq!(unsafe { x4mp_xform_check(junk.as_ptr(), junk.len(), std::ptr::null_mut()) }, -1);
        assert_eq!(unsafe { x4mp_xform_check(junk.as_ptr(), junk.len(), &mut out) }, -2);
        assert!(out.message[0] != 0);
    }

    /// With `X4MP_XFORM_CAPTURE_DIR` (oracle or in-game captures): a capture checks clean, and a
    /// changed post-state (an engine-side difference) is reported with its field and node.
    #[test]
    fn checks_captures_and_reports_differences() {
        let Some(dir) = std::env::var_os("X4MP_XFORM_CAPTURE_DIR") else {
            eprintln!("skipped: X4MP_XFORM_CAPTURE_DIR not set");
            return;
        };
        let file = std::fs::read_dir(dir)
            .unwrap()
            .filter_map(|e| e.ok().map(|e| e.path()))
            .find(|p| p.extension().is_some_and(|x| x == "cap"))
            .expect("a capture");
        let mut bytes = std::fs::read(&file).unwrap();
        let mut out = blank();
        assert_eq!(unsafe { x4mp_xform_check(bytes.as_ptr(), bytes.len(), &mut out) }, 0);
        assert_eq!(out.mismatches, 0, "{}", String::from_utf8_lossy(&out.message));
        assert!(out.components > 0);
        let post = Capture::parse(&bytes).unwrap().post_at;
        bytes[post] ^= 0x40; // node 0 own flags (+0x1e0) after the build
        let mut out = blank();
        assert_eq!(unsafe { x4mp_xform_check(bytes.as_ptr(), bytes.len(), &mut out) }, 0);
        assert!(out.mismatches >= 1);
        assert_eq!((out.first_field, out.first_node), (1, 0), "Flags of node 0");
    }
}
