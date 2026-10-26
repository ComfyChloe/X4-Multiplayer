//! In-game recorder and shadow of `IKValue_RotateToward` (RVA 0xe1f840).
//!
//! The detour runs the original through the trampoline on every call. On the owner thread
//! (the simulation thread that armed it) it also copies the joint before and after, the
//! arguments and the two game clocks from executable TLS slot 0, and
//! * records the call into a buffer reserved at start (never grows; full means dropped),
//! * shadows it: runs [`crate::ikjoint::rotate_toward`] on a copy and compares bitwise.
//!
//! The original's result is always the one returned. Calls on other threads go straight
//! to the trampoline. Bookkeeping panics are caught and switch recording and shadowing off.

use std::arch::asm;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicPtr, AtomicU32, AtomicU64, AtomicUsize, Ordering};

use crate::capture::{diff, Record};
use crate::ikjoint::{rotate_toward, FrameClock, JointCommand, JointState};

pub type StepFn = unsafe extern "C" fn(*mut u8, f32, f32, f32, f32, f32, f32, u8) -> u8;

/// Trampoline to the original; set before the patch is installed, never cleared.
pub static TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
/// Thread allowed to touch the recorder.
pub static OWNER: AtomicU32 = AtomicU32::new(0);
pub static RECORDER: AtomicPtr<Recorder> = AtomicPtr::new(std::ptr::null_mut());
/// Calls from other threads (passed through).
pub static FOREIGN: AtomicU64 = AtomicU64::new(0);

#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThreadId() -> u32;
}

pub const MISMATCH_KEEP: usize = 16;

#[derive(Debug, Default)]
pub struct Recorder {
    pub record: bool,
    pub shadow: bool,
    pub records: Vec<Record>,
    pub calls: u64,
    pub dropped: u64,
    pub uninitialised_tls: u64,
    pub shadowed: u64,
    pub mismatched: u64,
    pub skipped_mode: u64,
    /// (original record, our result) of the first mismatches.
    pub mismatches: Vec<(Record, JointState, bool)>,
    pub mxcsr: u32,
    pub mxcsr_changes: u64,
    pub mode_byte: usize,
    pub failed: Option<&'static str>,
}

impl Recorder {
    pub fn new(capacity: usize, record: bool, shadow: bool, mode_byte: usize) -> Recorder {
        Recorder {
            record,
            shadow,
            records: Vec::with_capacity(if record { capacity } else { 0 }),
            mismatches: Vec::with_capacity(MISMATCH_KEEP),
            mxcsr: crate::mxcsr::get(),
            mode_byte,
            ..Default::default()
        }
    }
}

/// # Safety
/// `joint` points at a `U::IKValue`.
unsafe fn read_joint(joint: *const u8) -> JointState {
    JointState {
        value: (joint.add(0x08) as *const f32).read(),
        time: (joint.add(0x10) as *const f64).read(),
        velocity: (joint.add(0x18) as *const f32).read(),
        target: (joint.add(0x1c) as *const f32).read(),
        wrap: joint.add(0x21).read() != 0,
    }
}

/// The game clocks of this thread (executable TLS slot 0), or None before its TLS block is
/// initialised.
fn read_clock() -> Option<FrameClock> {
    unsafe {
        let slots: *const *const u8;
        asm!("mov {}, gs:[0x58]", out(reg) slots, options(nostack, preserves_flags, readonly));
        if slots.is_null() {
            return None;
        }
        let block = slots.read();
        if block.is_null() || block.add(0x10).read() == 0 {
            return None;
        }
        Some(FrameClock {
            now: (block.add(0x300) as *const f64).read(),
            previous: (block.add(0x328) as *const f64).read(),
        })
    }
}

/// The detour installed at the site (same ABI as the original).
///
/// # Safety
/// Only reachable through the installed patch with the original's arguments.
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn detour(
    joint: *mut u8,
    target: f32,
    min: f32,
    max: f32,
    max_speed: f32,
    accel: f32,
    feed_forward: f32,
    snap: u8,
) -> u8 {
    let original: StepFn = std::mem::transmute(TRAMPOLINE.load(Ordering::Relaxed));
    if GetCurrentThreadId() != OWNER.load(Ordering::Relaxed) {
        FOREIGN.fetch_add(1, Ordering::Relaxed);
        return original(joint, target, min, max, max_speed, accel, feed_forward, snap);
    }
    let recorder = RECORDER.load(Ordering::Acquire);
    if recorder.is_null() {
        return original(joint, target, min, max, max_speed, accel, feed_forward, snap);
    }
    // SAFETY: only the owner thread dereferences the recorder.
    let recorder = &mut *recorder;
    recorder.calls += 1;
    if recorder.failed.is_some() || (!recorder.record && !recorder.shadow) {
        return original(joint, target, min, max, max_speed, accel, feed_forward, snap);
    }
    let before = read_joint(joint);
    let clock = read_clock();
    let result = original(joint, target, min, max, max_speed, accel, feed_forward, snap);
    let Some(clock) = clock else {
        recorder.uninitialised_tls += 1;
        return result;
    };
    let after = read_joint(joint);
    let command = JointCommand { target, min, max, max_speed, accel, feed_forward, snap: snap != 0 };
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        let record = Record {
            component: joint as u64,
            before,
            command,
            clock,
            after,
            result: result != 0,
            ..Default::default()
        };
        let mxcsr = crate::mxcsr::get();
        if mxcsr != recorder.mxcsr {
            recorder.mxcsr_changes += 1;
            recorder.mxcsr = mxcsr;
        }
        if recorder.shadow {
            if (recorder.mode_byte as *const u8).read() != 0 {
                recorder.skipped_mode += 1;
            } else {
                let mut ours = before;
                let our_result = rotate_toward(&mut ours, &command, clock);
                recorder.shadowed += 1;
                if diff(&record, &ours, our_result).any() {
                    recorder.mismatched += 1;
                    if recorder.mismatches.len() < MISMATCH_KEEP {
                        recorder.mismatches.push((record, ours, our_result));
                    }
                }
            }
        }
        if recorder.record {
            if recorder.records.len() < recorder.records.capacity() {
                recorder.records.push(record);
            } else {
                recorder.dropped += 1;
            }
        }
    }));
    if outcome.is_err() {
        recorder.failed = Some("bookkeeping panicked");
    }
    result
}
