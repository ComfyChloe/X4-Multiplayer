//! Capture replay harness: frames produced by a reference "engine" system are replayed through
//! the system under test; an identical system is exact, a reassociated one is caught.

use x4mp_sim::capture::{capture_universe, replay_frames, ReplaySetup};
use x4mp_sim::comp::{Mat4f, Transform, Vel, Velocity};
use x4mp_sim::shadow::{Policy, Tol};
use x4mp_sim::{Bundle, FrameInfo, Pool, SectorCtx, System, Universe, UniverseId, WorldKind};

/// Reference kinematics: x += v * dt per lane, f32 multiply then add.
struct Kinematics;
impl System for Kinematics {
    fn name(&self) -> &'static str {
        "kinematics"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let dt = ctx.frame.dt as f32;
        let (t, v) = ctx.world.cols_mut::<Transform, Velocity>();
        for (t, v) in t.iter_mut().zip(v.iter()) {
            for k in 0..3 {
                t.0 .0[0][k] += v.0.linear[k] * dt;
            }
        }
    }
}

/// Same physics, different rounding: the product is formed in f64.
struct Kinematics64;
impl System for Kinematics64 {
    fn name(&self) -> &'static str {
        "kinematics64"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let dt = ctx.frame.dt;
        let (t, v) = ctx.world.cols_mut::<Transform, Velocity>();
        for (t, v) in t.iter_mut().zip(v.iter()) {
            for k in 0..3 {
                t.0 .0[0][k] = (f64::from(t.0 .0[0][k]) + f64::from(v.0.linear[k]) * dt) as f32;
            }
        }
    }
}

fn engine_frames(n: u64) -> Vec<x4mp_sim::capture::FrameRecord> {
    let pool = Pool::new(4);
    let mut u = Universe::new(3);
    for s in 0..6u64 {
        let ix = u.add_sector(UniverseId(0x500 + s), WorldKind::Sector);
        for i in 0..40u64 {
            let mut m = Mat4f::IDENTITY;
            m.0[0] = [i as f32 * 1234.567, -(s as f32) * 99.125, 0.1 * i as f32, 0.0];
            let v = Vel { linear: [37.3 + i as f32, -12.9, 0.333, 0.0], angular: [0.0; 4] };
            u.spawn(ix, UniverseId((s << 16) | (i + 1)), Bundle::new().with(Transform(m)).with(Velocity(v))).unwrap();
        }
    }
    let mut t = 100.0f64;
    u.commit();
    let mut frames = vec![capture_universe(&u, 0, t)];
    for f in 1..=n {
        let dt = if f % 3 == 0 { 1.0 / 30.0 } else { 1.0 / 60.0 }; // variable dt like the engine
        t += dt;
        u.step(&pool, &[&Kinematics], &FrameInfo { frame: f, game_time: t, dt });
        frames.push(capture_universe(&u, f, t));
    }
    frames
}

fn setup<'a>(systems: &'a [&'a dyn System], policy: Policy) -> ReplaySetup<'a> {
    ReplaySetup { make: &|| Universe::new(3), prepare: None, systems, columns: &["transform"], policy, structure: true }
}

#[test]
fn identical_system_replays_exactly() {
    let frames = engine_frames(12);
    let rep = replay_frames(&frames, &[], &setup(&[&Kinematics], Policy::exact()), &Pool::new(8));
    assert!(rep.is_clean(), "{}", rep.line("kinematics"));
    assert_eq!((rep.pairs, rep.skipped), (12, 0));
    assert_eq!(rep.comparator.compared(), 12 * 240 * 16);
}

#[test]
fn reassociated_system_is_caught_with_the_first_difference() {
    let frames = engine_frames(12);
    let rep = replay_frames(&frames, &[], &setup(&[&Kinematics64], Policy::exact()), &Pool::new(2));
    assert!(!rep.is_clean());
    assert_eq!(rep.first_mismatched_frame, Some(1));
    let d = rep.comparator.first().unwrap();
    assert!(d.field.starts_with("transform[0]"), "{d}");
    let line = rep.line("kinematics64");
    assert!(line.starts_with("REPLAYCAP kinematics64 pairs=12 mismatched_pairs="), "{line}");
    // The documented bound of this alternative: one rounding difference per step.
    let tolerant = replay_frames(
        &frames,
        &[],
        &setup(&[&Kinematics64], Policy::exact().with("transform", Tol::Ulps(1))),
        &Pool::new(2),
    );
    assert!(tolerant.is_clean(), "{}", tolerant.line("kinematics64"));
    assert!(tolerant.comparator.stats()["transform[][]"].within_tolerance > 0);
}

#[test]
fn non_consecutive_pairs_are_skipped() {
    let mut frames = engine_frames(6);
    frames.remove(3);
    let rep = replay_frames(&frames, &[], &setup(&[&Kinematics], Policy::exact()), &Pool::new(1));
    assert_eq!((rep.pairs, rep.skipped), (4, 1));
    assert!(rep.is_clean());
}

#[test]
fn subset_and_truncated_frames_never_count_as_full_world_replay() {
    use x4mp_sim::capture::{FRAME_SUBSET, FRAME_TRUNCATED};
    for flag in [FRAME_SUBSET, FRAME_TRUNCATED] {
        let mut frames = engine_frames(2);
        frames[1].flags = flag;
        frames[1].sectors.truncate(1);
        let rep = replay_frames(&frames, &[], &setup(&[&Kinematics], Policy::exact()), &Pool::new(1));
        assert_eq!((rep.pairs, rep.skipped), (0, 2));
        assert!(!rep.is_clean());
    }
}
