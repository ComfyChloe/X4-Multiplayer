//! The in-game recorder/shadow, exercised offline on the mapped pinned X4.exe: the real
//! patch at 0xe1f840 is armed on the mapped copy, the original is called through it, and
//! the recorded calls, the shadow comparison and the restored bytes are checked. No game
//! runs; this proves the patch, trampoline and detour mechanics, not native behaviour.
#![cfg(all(target_os = "windows", target_arch = "x86_64"))]

use std::sync::{Mutex, OnceLock};

use x4mp_combat::capture::{read_capture, replay};
use x4mp_combat::game::recorder::FAULT_INJECT;
use x4mp_combat::game::session::{Image as Target, Options, Session, SITE_BYTES};
use x4mp_combat::ikjoint::{FrameClock, JointCommand, JointState};
use x4mp_combat::oracle::{default_exe, rva, Image, IK_ROTATE_TOWARD_HEAD};
use x4mp_combat::testrng::Rng;

static SERIAL: Mutex<()> = Mutex::new(());

fn image() -> Option<&'static Image> {
    static IMAGE: OnceLock<Option<Image>> = OnceLock::new();
    IMAGE
        .get_or_init(|| match Image::load(&default_exe()) {
            Ok(image) => Some(image),
            Err(reason) => {
                if std::env::var_os("X4MP_REQUIRE_ORACLE").is_some_and(|v| v == "1") {
                    panic!("oracle required but unavailable: {reason}");
                }
                eprintln!("SKIP oracle: {reason}");
                None
            }
        })
        .as_ref()
}

fn case(rng: &mut Rng) -> (JointState, JointCommand, FrameClock) {
    let now = 100.0 + rng.unit() * 1000.0;
    let clock = FrameClock { now, previous: now - 1.0 / 60.0 };
    let before = JointState {
        value: rng.range(-3.0, 3.0),
        time: clock.previous,
        velocity: rng.range(-2.0, 2.0),
        target: 0.0,
        wrap: rng.chance(0.5),
    };
    let cmd = JointCommand {
        target: rng.range(-6.0, 6.0),
        min: if rng.chance(0.5) { -1.2 } else { 0.0 },
        max: if rng.chance(0.5) { 1.2 } else { 0.0 },
        max_speed: rng.range(0.1, 3.0),
        accel: rng.range(0.1, 8.0),
        feed_forward: rng.range(-0.3, 0.3),
        snap: rng.chance(0.02),
    };
    (before, cmd, clock)
}

#[test]
fn records_and_shadows_through_the_real_patch() {
    let _serial = SERIAL.lock().unwrap();
    let Some(image) = image() else { return };
    let base = image.at(0);
    let site = image.at(rva::IK_ROTATE_TOWARD);
    assert_eq!(image.bytes(rva::IK_ROTATE_TOWARD, 16), &SITE_BYTES);
    let mut session =
        Session::start(Target::Mapped(base), Options::capture(10_000, true)).expect("arm on the mapped image");
    assert_ne!(image.bytes(rva::IK_ROTATE_TOWARD, 16), &SITE_BYTES, "patched");
    assert_eq!(unsafe { *site }, 0xff);

    let mut rng = Rng::new(31);
    let mut expected = Vec::new();
    for _ in 0..5000 {
        let (before, cmd, clock) = case(&mut rng);
        expected.push(image.call_ik_rotate_toward(&before, &cmd, clock));
    }
    // A call from another thread passes through without being recorded.
    std::thread::scope(|scope| {
        scope.spawn(|| {
            let (before, cmd, clock) = case(&mut Rng::new(99));
            image.call_ik_rotate_toward(&before, &cmd, clock);
        });
    });
    let stats = session.stats();
    assert_eq!((stats.calls, stats.recorded, stats.foreign), (5000, 5000, 1));
    assert_eq!((stats.shadowed, stats.mismatched, stats.dropped), (5000, 0, 0), "{:?}", stats.first_mismatch);
    assert_eq!(stats.mode, 0);
    assert!(stats.failed.is_none(), "{:?}", stats.failed);
    for (record, (after, result)) in session.records().iter().zip(&expected) {
        assert_eq!(record.after.value.to_bits(), after.value.to_bits());
        assert_eq!(record.after.velocity.to_bits(), after.velocity.to_bits());
        assert_eq!(record.after.time.to_bits(), after.time.to_bits());
        assert_eq!(record.result, *result);
    }

    let path = std::env::temp_dir().join(format!("x4mp-combat-session-{}.txt", std::process::id()));
    assert_eq!(session.write(&path).unwrap(), 5000);
    let (header, records) = read_capture(std::fs::File::open(&path).unwrap()).unwrap();
    std::fs::remove_file(&path).ok();
    assert_eq!((header.steam_build, header.joint_mode, records.len()), (23_660_954, 0, 5000));
    assert!(replay(&records).exact());

    session.stop().expect("restore");
    assert_eq!(image.bytes(rva::IK_ROTATE_TOWARD, 16), &SITE_BYTES, "restored");
    assert_eq!(image.head_sha256(rva::IK_ROTATE_TOWARD, 32), IK_ROTATE_TOWARD_HEAD);
    // After stop the original runs directly again.
    let (before, cmd, clock) = case(&mut rng);
    let direct = image.call_ik_rotate_toward(&before, &cmd, clock);
    let mut ours = before;
    assert_eq!(x4mp_combat::ikjoint::rotate_toward(&mut ours, &cmd, clock), direct.1);
    assert_eq!(ours.value.to_bits(), direct.0.value.to_bits());
}

#[test]
fn full_buffer_drops_and_second_session_is_refused() {
    let _serial = SERIAL.lock().unwrap();
    let Some(image) = image() else { return };
    let session = Session::start(Target::Mapped(image.at(0)), Options::capture(10, false)).unwrap();
    assert!(Session::start(Target::Mapped(image.at(0)), Options::capture(10, false)).is_err());
    let mut rng = Rng::new(3);
    for _ in 0..25 {
        let (before, cmd, clock) = case(&mut rng);
        image.call_ik_rotate_toward(&before, &cmd, clock);
    }
    let stats = session.stats();
    assert_eq!((stats.calls, stats.recorded, stats.dropped, stats.shadowed), (25, 10, 15, 0));
    session.stop().unwrap();
    assert_eq!(image.bytes(rva::IK_ROTATE_TOWARD, 16), &SITE_BYTES);
}

#[test]
fn refuses_a_modified_site() {
    let _serial = SERIAL.lock().unwrap();
    let Some(image) = image() else { return };
    // A foreign patch at the site: the session must refuse and leave it alone.
    let site = image.at(rva::IK_ROTATE_TOWARD);
    let original = image.bytes(rva::IK_ROTATE_TOWARD, 16).to_vec();
    unsafe { poke(site, 0x90) };
    assert!(Session::start(Target::Mapped(image.at(0)), Options::capture(10, true)).is_err());
    assert_eq!(unsafe { *site }, 0x90);
    unsafe { poke(site, original[0]) };
    assert_eq!(image.bytes(rva::IK_ROTATE_TOWARD, 16), &original[..]);
}

#[test]
fn switch_serves_rust_results_identical_to_the_original() {
    let _serial = SERIAL.lock().unwrap();
    let Some(image) = image() else { return };
    let mut rng = Rng::new(77);
    let cases: Vec<_> = (0..4000).map(|_| case(&mut rng)).collect();
    // Reference: the original alone.
    let reference: Vec<_> = cases.iter().map(|(b, c, k)| image.call_ik_rotate_toward(b, c, *k)).collect();
    for verify_every in [1u32, 7, 0] {
        let session = Session::start(Target::Mapped(image.at(0)), Options::switched(verify_every)).unwrap();
        let served: Vec<_> = cases.iter().map(|(b, c, k)| image.call_ik_rotate_toward(b, c, *k)).collect();
        let stats = session.stats();
        session.stop().unwrap();
        assert_eq!(stats.served, 4000, "verify_every={verify_every}");
        let expected_verified = if verify_every == 0 { 0 } else { 4000u64.div_ceil(verify_every as u64) };
        assert_eq!(stats.verified, expected_verified);
        assert_eq!((stats.switch_mismatched, stats.switch_off), (0, None));
        for ((after, result), (want, want_result)) in served.iter().zip(&reference) {
            assert_eq!(result, want_result);
            assert_eq!(after.value.to_bits(), want.value.to_bits());
            assert_eq!(after.velocity.to_bits(), want.velocity.to_bits());
            assert_eq!(after.target.to_bits(), want.target.to_bits());
            assert_eq!(after.time.to_bits(), want.time.to_bits());
        }
    }
    assert_eq!(image.bytes(rva::IK_ROTATE_TOWARD, 16), &SITE_BYTES);
}

#[test]
fn switch_mismatch_falls_back_to_the_original_for_good() {
    let _serial = SERIAL.lock().unwrap();
    let Some(image) = image() else { return };
    let mut rng = Rng::new(5);
    let cases: Vec<_> = (0..50).map(|_| case(&mut rng)).collect();
    let reference: Vec<_> = cases.iter().map(|(b, c, k)| image.call_ik_rotate_toward(b, c, *k)).collect();
    let session = Session::start(Target::Mapped(image.at(0)), Options::switched(1)).unwrap();
    FAULT_INJECT.store(true, std::sync::atomic::Ordering::SeqCst);
    let served: Vec<_> = cases.iter().map(|(b, c, k)| image.call_ik_rotate_toward(b, c, *k)).collect();
    FAULT_INJECT.store(false, std::sync::atomic::Ordering::SeqCst);
    let stats = session.stats();
    session.stop().unwrap();
    assert_eq!((stats.served, stats.verified, stats.switch_mismatched), (0, 1, 1));
    assert_eq!(stats.switch_off, Some("verification mismatch"));
    // Every call, including the mismatching one, got the original's result.
    for ((after, result), (want, want_result)) in served.iter().zip(&reference) {
        assert_eq!(result, want_result);
        assert_eq!(after.velocity.to_bits(), want.velocity.to_bits());
        assert_eq!(after.value.to_bits(), want.value.to_bits());
    }
}

unsafe fn poke(at: *mut u8, value: u8) {
    #[link(name = "kernel32")]
    extern "system" {
        fn VirtualProtect(address: *mut std::ffi::c_void, size: usize, protect: u32, old: *mut u32) -> i32;
    }
    let mut old = 0;
    assert_ne!(VirtualProtect(at as *mut _, 1, 0x40, &mut old), 0);
    *at = value;
    VirtualProtect(at as *mut _, 1, old, &mut old);
}
