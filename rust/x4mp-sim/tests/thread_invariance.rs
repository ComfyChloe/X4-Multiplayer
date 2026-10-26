//! 1, 2 and 16 threads must give byte-identical results: state hash after every frame, the
//! routed message log, the barrier log and the frame reports' deterministic counters.

mod common;

use common::{build_universe, frame, Age, Traffic};
use x4mp_sim::comp::{Hull, Transform};
use x4mp_sim::{FrameInfo, Pool, SectorCtx, System, Universe};

struct Outcome {
    hashes: Vec<u64>,
    messages: Vec<String>,
    barrier: Vec<String>,
    counters: Vec<(u64, u64, u64, u64, u64, u64, u64)>,
    errors: Vec<String>,
    entities: usize,
}

/// A second system in the same frame: reads the traffic phase's results in its own phase.
struct Wear;
impl System for Wear {
    fn name(&self) -> &'static str {
        "wear"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let (hull, age) = ctx.world.cols_mut::<Hull, Age>();
        for (h, a) in hull.iter_mut().zip(age.iter()) {
            if a.0 % 13 == 0 {
                h.current -= 0.125;
            }
        }
    }
}

fn run(threads: usize, frames: u64, sectors: u32, per_sector: u32) -> Outcome {
    let pool = Pool::new(threads);
    let mut u: Universe = build_universe(0x5eed, sectors, per_sector);
    u.set_message_log(true);
    // The sector holding the "player" is pinned to the calling thread.
    u.set_pinned(x4mp_sim::SectorIx(0), true);
    let traffic = Traffic::new(sectors);
    let wear = Wear;
    let mut out = Outcome {
        hashes: Vec::new(),
        messages: Vec::new(),
        barrier: Vec::new(),
        counters: Vec::new(),
        errors: Vec::new(),
        entities: 0,
    };
    for f in 0..frames {
        let fi: FrameInfo = frame(f);
        let rep = u.step(&pool, &[&traffic, &wear], &fi);
        u.check().expect("invariants");
        out.hashes.push(u.state_hash());
        for p in &rep.phases {
            out.counters.push((
                p.messages,
                p.commands,
                p.spawned,
                p.despawned,
                p.migrated,
                p.command_errors,
                p.dropped_messages,
            ));
        }
        out.errors.extend(rep.errors);
    }
    out.messages = u.take_message_log();
    out.barrier = std::mem::take(&mut *traffic.log.lock().unwrap());
    out.entities = u.entity_count();
    out
}

#[test]
fn one_two_sixteen_threads_identical() {
    let (frames, sectors, per) = (90, 24, 120);
    let base = run(1, frames, sectors, per);
    // The scenario must actually exercise every path.
    assert!(base.counters.iter().any(|c| c.0 > 0), "no messages");
    assert!(base.counters.iter().any(|c| c.1 > 0), "no structural commands");
    assert!(base.counters.iter().any(|c| c.2 > 0), "no spawns");
    assert!(base.counters.iter().any(|c| c.3 > 0), "no despawns");
    assert!(base.counters.iter().any(|c| c.4 > 0), "no migrations");
    assert!(base.messages.iter().any(|m| m.contains("Damage")), "no cross-sector damage");
    assert!(base.barrier.iter().any(|m| m.contains("Report")), "no barrier reports");
    for threads in [2, 16] {
        let other = run(threads, frames, sectors, per);
        assert_eq!(other.hashes.len(), base.hashes.len());
        for (f, (a, b)) in base.hashes.iter().zip(&other.hashes).enumerate() {
            assert_eq!(a, b, "state differs at frame {f} with {threads} threads");
        }
        assert_eq!(other.counters, base.counters, "counters differ with {threads} threads");
        assert_eq!(other.messages, base.messages, "message log differs with {threads} threads");
        assert_eq!(other.barrier, base.barrier, "barrier log differs with {threads} threads");
        assert_eq!(other.errors, base.errors);
        assert_eq!(other.entities, base.entities);
    }
}

#[test]
fn repeated_runs_identical_and_seed_sensitive() {
    let a = run(4, 30, 8, 60);
    let b = run(4, 30, 8, 60);
    assert_eq!(a.hashes, b.hashes);
    assert_eq!(a.messages, b.messages);
    let pool = Pool::new(4);
    let mut u = build_universe(0x5eee, 8, 60);
    let t = Traffic::new(8);
    for f in 0..30 {
        u.step(&pool, &[&t], &frame(f));
    }
    assert_ne!(u.state_hash(), *a.hashes.last().unwrap(), "a different seed must change the result");
}

#[test]
fn barrier_messages_arrive_in_sector_then_emission_order() {
    let o = run(16, 20, 12, 40);
    // Within one frame the barrier log must be sorted by (sender sector, sequence).
    let mut last: Option<(String, u32, u32)> = None;
    for line in &o.barrier {
        let (frame, rest) = line.split_once(' ').unwrap();
        let (sender, rest) = rest.split_once('#').unwrap();
        let seq: u32 = rest.split_once(' ').unwrap().0.parse().unwrap();
        let sender: u32 = sender.parse().unwrap();
        if let Some((lf, ls, lq)) = &last {
            if lf == frame {
                assert!((sender, seq) > (*ls, *lq), "out of order: {line}");
            }
        }
        last = Some((frame.to_string(), sender, seq));
    }
    // Every sector reports its census every frame.
    assert_eq!(o.barrier.iter().filter(|l| l.contains("Census")).count(), 20 * 12);
}

#[test]
fn transforms_are_integrated_in_f32_without_reassociation() {
    // x' = x + v*dt evaluated as f32 multiply then f32 add, exactly like the system.
    let pool = Pool::new(2);
    let mut u = build_universe(7, 2, 3);
    let before: Vec<[f32; 3]> =
        u.world(x4mp_sim::SectorIx(1)).col::<Transform>().iter().map(|t| t.0.position()).collect();
    let vel: Vec<[f32; 4]> =
        u.world(x4mp_sim::SectorIx(1)).col::<x4mp_sim::comp::Velocity>().iter().map(|v| v.0.linear).collect();
    struct Move;
    impl System for Move {
        fn name(&self) -> &'static str {
            "move"
        }
        fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
            let dt = ctx.frame.dt as f32;
            let (tr, vel) = ctx.world.cols_mut::<Transform, x4mp_sim::comp::Velocity>();
            for (t, v) in tr.iter_mut().zip(vel.iter()) {
                for k in 0..3 {
                    t.0 .0[0][k] += v.0.linear[k] * dt;
                }
            }
        }
    }
    u.step(&pool, &[&Move], &frame(0));
    let after: Vec<[f32; 3]> =
        u.world(x4mp_sim::SectorIx(1)).col::<Transform>().iter().map(|t| t.0.position()).collect();
    let dt = (1.0f64 / 60.0) as f32;
    for i in 0..3 {
        for k in 0..3 {
            assert_eq!(after[i][k].to_bits(), (before[i][k] + vel[i][k] * dt).to_bits());
        }
    }
}
