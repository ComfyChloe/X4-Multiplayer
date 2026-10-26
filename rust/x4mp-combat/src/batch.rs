//! Batched joint steps for one sector's turrets, optionally on several threads.
//!
//! Every joint step reads and writes only its own joint, so a batch is order-independent
//! by construction: any split over threads gives bit-identical results to the serial
//! loop, provided every worker uses the simulation thread's MXCSR. The engine-side
//! effects of a step (StartedAiming/StoppedAiming events, the "at rest" flag of 0x756a80)
//! are not part of the step and stay with the caller, which applies them in the
//! original order at the frame barrier. Until the shared scheduler of `x4mp-sim` is
//! published (rewrite-coord/interfaces.md), this uses scoped std threads.

use crate::ikjoint::{rotate_toward, FrameClock, JointCommand, JointState};

/// One joint and its command for this frame.
#[derive(Debug, Clone, Copy, Default)]
pub struct JointJob {
    pub joint: JointState,
    pub command: JointCommand,
    pub reached: bool,
}

fn run(jobs: &mut [JointJob], clock: FrameClock) {
    for job in jobs {
        job.reached = rotate_toward(&mut job.joint, &job.command, clock);
    }
}

/// Step all jobs with `threads` workers (1 = on the calling thread). `mxcsr` is applied to
/// every worker thread before it computes; the calling thread keeps its own.
pub fn step_all(jobs: &mut [JointJob], clock: FrameClock, threads: usize, mxcsr: u32) {
    let threads = threads.max(1).min(jobs.len().max(1));
    if threads == 1 {
        run(jobs, clock);
        return;
    }
    let chunk = jobs.len().div_ceil(threads);
    std::thread::scope(|scope| {
        for part in jobs.chunks_mut(chunk) {
            scope.spawn(move || {
                // SAFETY: fresh worker thread, set before any float work.
                #[cfg(target_arch = "x86_64")]
                unsafe {
                    crate::mxcsr::set(mxcsr);
                }
                #[cfg(not(target_arch = "x86_64"))]
                let _ = mxcsr;
                run(part, clock);
            });
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testrng::Rng;

    fn jobs(seed: u64, count: usize) -> Vec<JointJob> {
        let mut rng = Rng::new(seed);
        (0..count)
            .map(|_| JointJob {
                joint: JointState {
                    value: rng.range(-3.5, 3.5),
                    time: 100.0,
                    velocity: rng.range(-2.0, 2.0),
                    target: 0.0,
                    wrap: rng.chance(0.5),
                },
                command: JointCommand {
                    target: rng.range(-7.0, 7.0),
                    min: if rng.chance(0.5) { rng.range(-1.6, 0.0) } else { 0.0 },
                    max: if rng.chance(0.5) { rng.range(0.0, 1.6) } else { 0.0 },
                    max_speed: rng.range(0.05, 3.0),
                    accel: rng.range(0.05, 8.0),
                    feed_forward: if rng.chance(0.3) { rng.range(-0.5, 0.5) } else { 0.0 },
                    snap: rng.chance(0.02),
                },
                reached: false,
            })
            .collect()
    }

    fn bits(jobs: &[JointJob]) -> Vec<(u32, u32, u32, u64, bool)> {
        jobs.iter()
            .map(|j| {
                (
                    j.joint.value.to_bits(),
                    j.joint.velocity.to_bits(),
                    j.joint.target.to_bits(),
                    j.joint.time.to_bits(),
                    j.reached,
                )
            })
            .collect()
    }

    #[test]
    fn thread_count_does_not_change_results() {
        let mxcsr = crate::mxcsr::get();
        let mut reference = jobs(7, 5000);
        let mut clock = FrameClock { now: 100.0, previous: 99.983 };
        let mut variants: Vec<Vec<JointJob>> = (0..4).map(|_| reference.clone()).collect();
        for frame in 0..60 {
            clock.previous = clock.now;
            clock.now += 1.0 / 60.0 + (frame % 3) as f64 * 0.001;
            step_all(&mut reference, clock, 1, mxcsr);
            for (variant, threads) in variants.iter_mut().zip([2, 3, 8, 32]) {
                step_all(variant, clock, threads, mxcsr);
            }
        }
        let expected = bits(&reference);
        for variant in &variants {
            assert_eq!(bits(variant), expected);
        }
        assert!(reference.iter().any(|j| j.reached) && reference.iter().any(|j| !j.reached));
    }
}
