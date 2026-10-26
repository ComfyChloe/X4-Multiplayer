//! ===================== EXTENSION POINT: per-frame work =====================
//!
//! [`on_frame`] runs on the X4 `Movement worker` thread inside VBI slot 13, once per
//! simulation frame that is not a loading frame, after every engine guard has passed.
//! It drives the Rust core (`x4mp-core`): the frame driver, its subsystems (which read
//! engine state through build-pinned exports) and the MD action queue. When no core is
//! installed (`X4MP_CORE=0`, refused binding, or after disarm) this is a no-op.
//!
//! Rules for code behind this point:
//!
//! - Never block, allocate unboundedly, take engine locks or do I/O on this thread.
//!   The core logs into a bounded buffer that the agent worker thread drains.
//! - Only call engine code through `x4mp_core::ffi` (exports pinned to this build), and
//!   only on this thread (the universe is mutated by the MoveWorker without locks).
//! - On any inconsistency return `Err`; the caller disarms the agent (fail closed).
//! - Keep this separate from the hook-free transport library (`x4lan-net`), per
//!   ENGINE-INSTRUMENTATION.txt rule 2; the core may link and drive it later.

pub use x4mp_core::frame::FrameInput;

/// What the frame callback observed this frame.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct FrameSample {
    /// Callbacks since registration (not save-persistent; includes paused frames).
    pub frame: u64,
    /// TimerBase game time (s).
    pub game_time: f64,
    /// TimerBase real time (s), advances while paused.
    pub real_time: f64,
    /// TimerBase speed factor (0 paused, 1 normal, up to 6 SETA).
    pub speed: f64,
    /// Game-time step this frame from the MoveWorker TLS clock (CurTime - previous).
    pub tls_dt: f64,
    pub paused: bool,
    /// CLOCK_MONOTONIC at callback entry (ns).
    pub monotonic_ns: u64,
}

impl FrameSample {
    pub fn input(&self) -> FrameInput {
        FrameInput {
            frame: self.frame,
            game_time: self.game_time,
            dt: self.tls_dt,
            paused: self.paused,
            monotonic_ns: self.monotonic_ns,
        }
    }
}

/// Called once per simulation frame on the MoveWorker thread.
#[inline]
pub fn on_frame(sample: &FrameSample) -> Result<(), &'static str> {
    x4mp_core::runtime::frame(&sample.input())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sample_maps_to_core_input_and_is_a_no_op_without_core() {
        let s = FrameSample {
            frame: 3,
            game_time: 1.5,
            tls_dt: 0.016,
            paused: true,
            monotonic_ns: 9,
            ..Default::default()
        };
        assert_eq!(s.input(), FrameInput { frame: 3, game_time: 1.5, dt: 0.016, paused: true, monotonic_ns: 9 });
        assert_eq!(on_frame(&s), Ok(()));
    }
}
