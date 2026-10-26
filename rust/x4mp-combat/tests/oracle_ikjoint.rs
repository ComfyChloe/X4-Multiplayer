//! Differential test of `ikjoint::rotate_toward` against the original machine code.
//!
//! Needs Windows x64 and the pinned X4.exe (default `../x4-re/bin/X4.exe` next to the
//! checkout, or `X4MP_X4_EXE`). Without it each test prints `SKIP oracle` and passes,
//! unless `X4MP_REQUIRE_ORACLE=1`, which turns the missing file into a failure. This is
//! an offline comparison with the engine's own code, not a native game result.
#![cfg(all(target_os = "windows", target_arch = "x86_64"))]

use std::sync::OnceLock;

use x4mp_combat::capture::{diff, replay, Record};
use x4mp_combat::ikjoint::{self, rotate_toward, FrameClock, JointCommand, JointState};
use x4mp_combat::mxcsr;
use x4mp_combat::oracle::{default_exe, rva, Image, IK_ROTATE_TOWARD_HEAD};
use x4mp_combat::testrng::Rng;

fn image() -> Option<&'static Image> {
    static IMAGE: OnceLock<Option<Image>> = OnceLock::new();
    IMAGE
        .get_or_init(|| {
            let path = default_exe();
            match Image::load(&path) {
                Ok(image) => Some(image),
                Err(reason) => {
                    if std::env::var_os("X4MP_REQUIRE_ORACLE").is_some_and(|v| v == "1") {
                        panic!("oracle required but unavailable: {reason}");
                    }
                    eprintln!("SKIP oracle: {reason}");
                    None
                }
            }
        })
        .as_ref()
}

struct Tally {
    cases: usize,
    reached: usize,
    snapped_or_settled: usize,
}

/// Compare one case; panics with the full input on the first difference.
fn check(image: &Image, before: &JointState, cmd: &JointCommand, clock: FrameClock, tally: &mut Tally) -> JointState {
    let (expected, expected_result) = image.call_ik_rotate_toward(before, cmd, clock);
    let mut ours = *before;
    let result = rotate_toward(&mut ours, cmd, clock);
    let record = Record {
        before: *before,
        command: *cmd,
        clock,
        after: expected,
        result: expected_result,
        ..Default::default()
    };
    let d = diff(&record, &ours, result);
    assert!(
        !d.any(),
        "mismatch {d:?}\n before={before:?}\n cmd={cmd:?}\n clock={clock:?}\n original={expected:?} -> {expected_result}\n rust={ours:?} -> {result}"
    );
    tally.cases += 1;
    tally.reached += result as usize;
    tally.snapped_or_settled += (expected.velocity == 0.0) as usize;
    expected
}

fn realistic(rng: &mut Rng) -> (JointState, JointCommand, FrameClock) {
    let now = 10.0 + rng.unit() * 20_000.0;
    let dt = if rng.chance(0.1) { rng.unit() * 0.25 } else { 1.0 / (20.0 + rng.unit() * 220.0) };
    let clock = FrameClock { now, previous: now - dt };
    let before = JointState {
        value: rng.range(-3.2, 3.2),
        time: if rng.chance(0.03) { now + rng.unit() * 0.01 } else { clock.previous },
        velocity: rng.range(-2.5, 2.5),
        target: rng.range(-3.2, 3.2),
        wrap: rng.chance(0.5),
    };
    let limited = rng.chance(0.6);
    let cmd = JointCommand {
        target: if rng.chance(0.05) { before.value + rng.range(-2e-4, 2e-4) } else { rng.range(-9.5, 9.5) },
        min: if limited {
            rng.range(-1.6, 0.05)
        } else if rng.chance(0.1) {
            rng.range(-1e-4, 1e-4)
        } else {
            0.0
        },
        max: if limited { rng.range(-0.05, 1.6) } else { 0.0 },
        max_speed: if rng.chance(0.02) { 0.0 } else { rng.range(0.01, 3.5) },
        accel: if rng.chance(0.02) { 0.0 } else { rng.range(0.01, 12.0) },
        feed_forward: if rng.chance(0.4) { rng.range(-1.5, 1.5) } else { 0.0 },
        snap: rng.chance(0.02),
    };
    (before, cmd, clock)
}

fn wild(rng: &mut Rng) -> (JointState, JointCommand, FrameClock) {
    // Any finite bit patterns (including subnormals, signed zeros, huge magnitudes).
    let now = rng.unit() * 1e6 - 1e3;
    let clock = FrameClock { now, previous: now - (rng.unit() * 2.0 - 0.5) };
    let before = JointState {
        value: rng.finite_bits(),
        time: if rng.chance(0.5) { clock.previous } else { now + rng.unit() - 0.5 },
        velocity: rng.finite_bits(),
        target: rng.finite_bits(),
        wrap: rng.chance(0.5),
    };
    let cmd = JointCommand {
        target: rng.finite_bits(),
        min: rng.finite_bits(),
        max: rng.finite_bits(),
        max_speed: rng.finite_bits(),
        accel: rng.finite_bits(),
        feed_forward: rng.finite_bits(),
        snap: rng.chance(0.05),
    };
    (before, cmd, clock)
}

#[test]
fn pinned_code_and_constants() {
    let Some(image) = image() else { return };
    assert_eq!(image.head_sha256(rva::IK_ROTATE_TOWARD, 32), IK_ROTATE_TOWARD_HEAD);
    assert_eq!(image.joint_mode(), 0, "kinematic branch expected in an unstarted image");
    let vbase = i32::from_le_bytes(image.bytes(rva::IKVALUE_VBTABLE + 4, 4).try_into().unwrap());
    assert_eq!(vbase, 0x678);
    for (at, ours) in [
        (rva::TWO_PI, ikjoint::TWO_PI),
        (rva::PI, ikjoint::PI),
        (rva::EPSILON, ikjoint::EPSILON),
        (rva::BRAKE_MARGIN, ikjoint::BRAKE_MARGIN),
    ] {
        assert_eq!(image.f32_at(at).to_bits(), ours.to_bits(), "constant at {at:#x}");
    }
}

#[test]
fn realistic_single_steps_are_bit_exact() {
    let Some(image) = image() else { return };
    let mut rng = Rng::new(0xc0ffee);
    let mut tally = Tally { cases: 0, reached: 0, snapped_or_settled: 0 };
    for _ in 0..400_000 {
        let (before, cmd, clock) = realistic(&mut rng);
        check(image, &before, &cmd, clock, &mut tally);
    }
    eprintln!(
        "realistic: {} cases, {} reached, {} with zero velocity after",
        tally.cases, tally.reached, tally.snapped_or_settled
    );
    assert!(tally.reached > 10_000 && tally.reached < tally.cases - 10_000);
}

#[test]
fn wild_bit_patterns_are_bit_exact() {
    let Some(image) = image() else { return };
    let mut rng = Rng::new(42);
    let mut tally = Tally { cases: 0, reached: 0, snapped_or_settled: 0 };
    for _ in 0..400_000 {
        let (before, cmd, clock) = wild(&mut rng);
        check(image, &before, &cmd, clock, &mut tally);
    }
    eprintln!("wild: {} cases, {} reached", tally.cases, tally.reached);
}

#[test]
fn edge_cases_are_bit_exact() {
    let Some(image) = image() else { return };
    let mut tally = Tally { cases: 0, reached: 0, snapped_or_settled: 0 };
    let specials = [
        0.0f32,
        -0.0,
        1e-4,
        -1e-4,
        9.99e-5,
        1.0001e-4,
        f32::MIN_POSITIVE,
        -f32::MIN_POSITIVE,
        1e-40,
        -1e-40,
        0.5,
        -0.5,
        std::f32::consts::PI,
        -std::f32::consts::PI,
        std::f32::consts::TAU,
        1.0,
        -1.0,
        3.0e38,
        -3.0e38,
    ];
    let clocks = [
        FrameClock { now: 100.0, previous: 100.0 - 1.0 / 60.0 },
        FrameClock { now: 100.0, previous: 100.0 },
        FrameClock { now: 1e-300, previous: 0.0 },
        FrameClock { now: 5e5, previous: 5e5 - 0.25 },
    ];
    for &value in &specials {
        for &target in &specials {
            for &limit in &[0.0f32, 1e-4, 1.0, -1e-40] {
                for clock in clocks {
                    for wrap in [false, true] {
                        for snap in [false, true] {
                            let before = JointState { value, time: clock.previous, velocity: 0.25, target: 0.0, wrap };
                            let cmd = JointCommand {
                                target,
                                min: -limit,
                                max: limit,
                                max_speed: 2.0,
                                accel: 4.0,
                                feed_forward: 0.125,
                                snap,
                            };
                            check(image, &before, &cmd, clock, &mut tally);
                        }
                    }
                }
            }
        }
    }
    eprintln!("edge: {} cases", tally.cases);
}

#[test]
fn nan_inputs_are_bit_exact() {
    // NaN in any non-wrapping input (wrapping would send NaN into the CRT fmodf error path,
    // which cannot run offline). NaN payloads and signs must match too.
    let Some(image) = image() else { return };
    let mut rng = Rng::new(1234);
    let mut tally = Tally { cases: 0, reached: 0, snapped_or_settled: 0 };
    let nans = [f32::NAN, -f32::NAN, f32::from_bits(0x7fc0_0001), f32::from_bits(0xffa0_0000)];
    for i in 0..60_000 {
        let (mut before, mut cmd, clock) = realistic(&mut rng);
        before.wrap = false;
        let nan = nans[i % nans.len()];
        match i % 9 {
            0 => cmd.max_speed = nan,
            1 => cmd.accel = nan,
            2 => cmd.feed_forward = nan,
            3 => cmd.min = nan,
            4 => cmd.max = nan,
            5 => cmd.target = nan,
            6 => before.velocity = nan,
            7 => before.value = nan,
            _ => before.time = f64::NAN,
        }
        check(image, &before, &cmd, clock, &mut tally);
    }
    eprintln!("nan: {} cases, {} reached", tally.cases, tally.reached);
}

#[test]
fn tracking_sequences_are_bit_exact() {
    // Feed the original's output back as the next frame's input: a turret following a
    // moving target for 600 frames, 400 turrets.
    let Some(image) = image() else { return };
    let mut rng = Rng::new(9);
    let mut tally = Tally { cases: 0, reached: 0, snapped_or_settled: 0 };
    for _ in 0..400 {
        let mut joint =
            JointState { value: rng.range(-1.0, 1.0), time: 0.0, velocity: 0.0, target: 0.0, wrap: rng.chance(0.5) };
        let (speed, accel) = (rng.range(0.2, 2.5), rng.range(0.5, 6.0));
        let (min, max) = if joint.wrap { (0.0, 0.0) } else { (rng.range(-1.5, -0.1), rng.range(0.1, 1.5)) };
        let (phase, rate) = (rng.range(0.0, 6.0), rng.range(-1.2, 1.2));
        let mut clock = FrameClock { now: 0.0, previous: 0.0 };
        for frame in 0..600 {
            clock.previous = clock.now;
            clock.now += 1.0 / 60.0 + if frame % 7 == 0 { 0.004 } else { 0.0 };
            let target = phase + rate * clock.now as f32;
            let cmd = JointCommand {
                target,
                min,
                max,
                max_speed: speed,
                accel,
                feed_forward: rate,
                snap: frame == 0 && rng.chance(0.1),
            };
            joint = check(image, &joint, &cmd, clock, &mut tally);
        }
    }
    eprintln!("tracking: {} steps, {} reached", tally.cases, tally.reached);
}

#[test]
fn bit_exact_under_flush_to_zero() {
    // Both sides run on one thread with FTZ set, as on X4's main thread.
    let Some(image) = image() else { return };
    std::thread::spawn(move || {
        let start = mxcsr::get();
        unsafe { mxcsr::set(start | mxcsr::FTZ) };
        let mut rng = Rng::new(77);
        let mut tally = Tally { cases: 0, reached: 0, snapped_or_settled: 0 };
        for i in 0..200_000 {
            let (before, cmd, clock) = if i % 2 == 0 { realistic(&mut rng) } else { wild(&mut rng) };
            check(image, &before, &cmd, clock, &mut tally);
        }
        unsafe { mxcsr::set(start) };
        eprintln!("ftz: {} cases", tally.cases);
    })
    .join()
    .unwrap();
}

#[test]
fn oracle_capture_replays_exactly() {
    let Some(image) = image() else { return };
    let mut rng = Rng::new(5);
    let records: Vec<Record> = (0..20_000)
        .map(|i| {
            let (before, command, clock) = realistic(&mut rng);
            let (after, result) = image.call_ik_rotate_toward(&before, &command, clock);
            Record { component: i, chain: 0, joint: 0, before, command, clock, after, result }
        })
        .collect();
    let report = replay(&records);
    assert!(report.exact(), "{report:?}");
}
