//! In-game recorder, shadow and switch of `IKValue_RotateToward` (RVA 0xe1f840).
//!
//! On every call from another thread the detour runs the original. On the owner thread (the
//! simulation thread that armed it) it can
//! * record the call (joint before/after, arguments, both game clocks) into a buffer
//!   reserved at start (never grows; full means dropped),
//! * shadow it: run [`crate::ikjoint::rotate_toward`] on a copy and compare bitwise,
//! * switch: make the reimplementation authoritative for the kinematic branch. The result is
//!   applied with the original's writes (target, velocity, then the joint's value source
//!   SetValue through its vftable, or nothing but the target when no game time passed). With
//!   `verify_every` = N, every N-th switched call also runs the original on a scratch copy of
//!   the joint first; any difference turns the switch off for good and that call (and all
//!   later ones) is served by the original on the real joint.
//!
//! Bookkeeping panics are caught and turn everything but the plain pass-through off.

use std::arch::asm;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU32, AtomicU64, AtomicUsize, Ordering};

use crate::capture::{diff, Record};
use crate::ikjoint::{self, FrameClock, JointCommand, JointState, Outcome};

pub type StepFn = unsafe extern "C" fn(*mut u8, f32, f32, f32, f32, f32, f32, u8) -> u8;
type SetValueFn = unsafe extern "C" fn(*mut u8, *const f32, f64);

/// Trampoline to the original; set before the patch is installed, never cleared.
pub static TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
/// Thread allowed to touch the recorder.
pub static OWNER: AtomicU32 = AtomicU32::new(0);
pub static RECORDER: AtomicPtr<Recorder> = AtomicPtr::new(std::ptr::null_mut());
/// Calls from other threads (passed through).
pub static FOREIGN: AtomicU64 = AtomicU64::new(0);
/// Test hook: when set, switched results are corrupted by one bit before verification
/// (proves that a mismatch turns the switch off). Never set outside tests.
#[doc(hidden)]
pub static FAULT_INJECT: AtomicBool = AtomicBool::new(false);

#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThreadId() -> u32;
}

pub const MISMATCH_KEEP: usize = 16;
const IKVALUE_VBASE: usize = 0x678;

#[derive(Debug, Default)]
pub struct Recorder {
    pub record: bool,
    pub shadow: bool,
    pub switch: bool,
    pub verify_every: u32,
    pub records: Vec<Record>,
    pub calls: u64,
    pub dropped: u64,
    pub uninitialised_tls: u64,
    pub shadowed: u64,
    pub mismatched: u64,
    pub skipped_mode: u64,
    pub served: u64,
    pub verified: u64,
    pub switch_mismatched: u64,
    pub foreign_class: u64,
    pub switch_off: Option<&'static str>,
    /// (original record, our result) of the first mismatches (shadow or switch).
    pub mismatches: Vec<(Record, JointState, bool)>,
    pub mxcsr: u32,
    pub mxcsr_changes: u64,
    pub mode_byte: usize,
    /// Address of `U::IKValue::vftable` (the joint's value-source vtable) in this image.
    pub ikvalue_vftable: usize,
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

    /// Enable switch mode (reimplementation authoritative).
    pub fn with_switch(mut self, verify_every: u32, ikvalue_vftable: usize) -> Recorder {
        self.switch = true;
        self.verify_every = verify_every;
        self.ikvalue_vftable = ikvalue_vftable;
        self
    }

    fn keep_mismatch(&mut self, record: Record, ours: JointState, our_result: bool) {
        if self.mismatches.len() < MISMATCH_KEEP {
            self.mismatches.push((record, ours, our_result));
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

/// The joint's value-source subobject (virtual base) through its vbtable.
unsafe fn value_source(joint: *mut u8) -> *mut u8 {
    let vbtable = (joint as *const *const u8).read();
    let offset = (vbtable.add(4) as *const i32).read();
    joint.offset(offset as isize)
}

/// Write a step result into the joint exactly as the original does for that outcome.
unsafe fn apply(joint: *mut u8, ours: &JointState, outcome: Outcome) {
    (joint.add(0x1c) as *mut f32).write(ours.target);
    if outcome == Outcome::Waiting {
        return;
    }
    (joint.add(0x18) as *mut f32).write(ours.velocity);
    let source = value_source(joint);
    let vtable = (source as *const *const usize).read();
    let set_value: SetValueFn = std::mem::transmute(vtable.add(3).read());
    let value = ours.value;
    set_value(source, &value, ours.time);
}

#[repr(C, align(16))]
struct Scratch([u8; IKVALUE_SIZE]);
const IKVALUE_SIZE: usize = 0x680;
static mut SCRATCH: Scratch = Scratch([0; IKVALUE_SIZE]);

/// A copy of the joint's header and value-source vptr in the owner-thread scratch IKValue.
unsafe fn scratch_copy(joint: *const u8) -> *mut u8 {
    let scratch = std::ptr::addr_of_mut!(SCRATCH) as *mut u8;
    std::ptr::copy_nonoverlapping(joint, scratch, 0x40);
    std::ptr::copy_nonoverlapping(joint.add(IKVALUE_VBASE - 8), scratch.add(IKVALUE_VBASE - 8), 16);
    scratch
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
    if recorder.failed.is_some() {
        return original(joint, target, min, max, max_speed, accel, feed_forward, snap);
    }
    let command = JointCommand { target, min, max, max_speed, accel, feed_forward, snap: snap != 0 };

    if recorder.switch && recorder.switch_off.is_none() {
        if (recorder.mode_byte as *const u8).read() != 0 {
            recorder.switch_off = Some("sub-stepped joint mode active");
        } else if (value_source(joint) as *const usize).read() != recorder.ikvalue_vftable {
            recorder.foreign_class += 1;
        } else if let Some(clock) = read_clock() {
            let before = read_joint(joint);
            let served = catch_unwind(AssertUnwindSafe(|| {
                let mut ours = before;
                let (mut our_result, outcome) = ikjoint::step(&mut ours, &command, clock);
                if FAULT_INJECT.load(Ordering::Relaxed) {
                    ours.velocity = f32::from_bits(ours.velocity.to_bits() ^ 1);
                    our_result = !our_result;
                }
                let verify = recorder.verify_every != 0 && recorder.served % recorder.verify_every as u64 == 0;
                if verify {
                    let scratch = scratch_copy(joint);
                    let result = original(scratch, target, min, max, max_speed, accel, feed_forward, snap);
                    let record = Record {
                        component: joint as u64,
                        before,
                        command,
                        clock,
                        after: read_joint(scratch),
                        result: result != 0,
                        ..Default::default()
                    };
                    recorder.verified += 1;
                    if diff(&record, &ours, our_result).any() {
                        recorder.switch_mismatched += 1;
                        recorder.keep_mismatch(record, ours, our_result);
                        recorder.switch_off = Some("verification mismatch");
                        return None;
                    }
                }
                apply(joint, &ours, outcome);
                recorder.served += 1;
                Some(our_result as u8)
            }));
            match served {
                Ok(Some(result)) => return result,
                Ok(None) => {}
                Err(_) => recorder.failed = Some("switch panicked"),
            }
            return original(joint, target, min, max, max_speed, accel, feed_forward, snap);
        }
    }

    if !recorder.record && !recorder.shadow {
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
                let our_result = ikjoint::rotate_toward(&mut ours, &command, clock);
                recorder.shadowed += 1;
                if diff(&record, &ours, our_result).any() {
                    recorder.mismatched += 1;
                    recorder.keep_mismatch(record, ours, our_result);
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
