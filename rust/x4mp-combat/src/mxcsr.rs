//! The SSE control register (MXCSR) of the calling thread.
//!
//! Bit-identical float results need the same MXCSR as the X4 thread whose work is
//! reproduced: rounding mode, flush-to-zero (FTZ, bit 15) and denormals-are-zero (DAZ,
//! bit 6) change results with subnormal operands. X4 sets FTZ on its main thread
//! (0x14dd0e0, called from `X4::GameClient::Init`); threads start with the Windows
//! default 0x1f80. A capture records the simulation thread's value and a per-sector
//! worker must run with it.
//!
//! Changing MXCSR is outside Rust's floating-point model (the compiler assumes the
//! default environment when it folds constants), so [`set`] is only for a thread's setup
//! before it computes, as the engine's own threads do.

use std::arch::asm;

/// Windows' initial MXCSR for a new thread: all exceptions masked, round to nearest.
pub const DEFAULT: u32 = 0x1f80;
/// Flush-to-zero.
pub const FTZ: u32 = 0x8000;
/// Denormals-are-zero.
pub const DAZ: u32 = 0x0040;

/// MXCSR of the calling thread.
pub fn get() -> u32 {
    let mut value: u32 = 0;
    // SAFETY: stmxcsr only stores the register into the given 4 bytes.
    unsafe { asm!("stmxcsr [{}]", in(reg) &mut value, options(nostack, preserves_flags)) };
    value
}

/// Set the calling thread's MXCSR.
///
/// # Safety
/// The caller decides the floating-point environment of everything the thread computes
/// afterwards; use it at thread setup only, with exceptions masked (bits 7..12 set).
pub unsafe fn set(value: u32) {
    asm!("ldmxcsr [{}]", in(reg) &value, options(nostack, preserves_flags, readonly));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ftz_flushes_a_subnormal_product_only_when_set() {
        std::thread::spawn(|| {
            let start = get();
            let tiny = std::hint::black_box(f32::MIN_POSITIVE);
            let half = std::hint::black_box(0.5f32);
            assert_ne!(tiny * half, 0.0, "default MXCSR keeps subnormals");
            unsafe { set(start | FTZ) };
            // Fresh black_box operands: the compiler assumes a fixed environment and would
            // otherwise reuse the product computed above (the hazard documented on `set`).
            let flushed = std::hint::black_box(tiny) * std::hint::black_box(half);
            unsafe { set(start) };
            assert_eq!(flushed, 0.0);
            assert_eq!(get(), start);
        })
        .join()
        .unwrap();
    }
}
