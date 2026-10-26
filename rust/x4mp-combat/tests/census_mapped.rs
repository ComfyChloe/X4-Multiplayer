//! The combat census on the mapped pinned X4.exe: the pinned vtable entries hold the pinned
//! originals, the census swaps exactly those entries and restores them; the thread-RNG step
//! the census counts equals the original `Random::U64State`. No game runs and the hooked
//! originals are not called here (they need a live world); the wrappers themselves are
//! tested on a synthetic engine in `game::census`. Not a native result.
#![cfg(all(target_os = "windows", target_arch = "x86_64"))]

use std::sync::OnceLock;

use x4mp_combat::game::census::{pinned_sites, rng_step, rva, shot_thunks, CensusSession, Kind};
use x4mp_combat::oracle::{default_exe, Image};
use x4mp_combat::testrng::Rng;

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

#[test]
fn pinned_entries_hold_the_pinned_originals_and_are_restored() {
    let Some(image) = image() else { return };
    let base = image.at(0);
    let (sites, classes) = unsafe { pinned_sites(base) };
    assert_eq!(sites.len(), 9);
    for site in &sites {
        assert_eq!(unsafe { site.entry.read() }, site.original, "entry {:?} at {:p}", site.kind, site.entry);
    }
    let rel = |address: usize| address - base as usize;
    assert_eq!(rel(sites[0].original), rva::WEAPON_MOVE as usize);
    assert_eq!(rel(sites[4].original), rva::BULLET_MOVE as usize);
    assert_eq!(rel(sites[5].entry as usize), 0x02b6_a1a8);
    assert_eq!(rel(sites[8].original), rva::MISSILE_TURRET_FIRE_SHOT as usize);
    assert_eq!(classes.map(rel), [0x02b6_8278, 0x02b6_e910, 0x02b0_2988, 0x02b0_0620]);

    let census = unsafe { CensusSession::start(&sites, classes, 16) }.expect("census starts on the mapped image");
    let thunks = shot_thunks();
    for site in &sites {
        let now = unsafe { site.entry.read() };
        assert_ne!(now, site.original);
        if let Kind::Shot(i) = site.kind {
            assert_eq!(now, thunks[i]);
        }
    }
    let stats = census.stats();
    assert_eq!((stats.frames, stats.weapon_calls, stats.failed), (0, 0, None));
    census.stop().expect("entries restored");
    for site in &sites {
        assert_eq!(unsafe { site.entry.read() }, site.original);
    }
}

#[test]
fn primary_target_reference_matches_original_getter() {
    let Some(image) = image() else { return };
    // Getter17 is a leaf copying controller+0x10 to the caller's result slot.
    type GetTarget = unsafe extern "system" fn(*const u8, *mut u64) -> *mut u64;
    let original: GetTarget = unsafe { std::mem::transmute(image.at(0x007e_5d30)) };
    for target in [0u64, 77, 0x0123_4567_89ab_cdef] {
        let mut controller = [0u64; 4];
        controller[2] = target;
        controller[3] = !target; // wrong +0x18 offset must not influence the result
        let mut out = 0;
        let returned = unsafe { original(controller.as_ptr().cast(), &mut out) };
        assert_eq!(returned, &mut out as *mut u64);
        assert_eq!(out, target);
    }
}

#[test]
fn rng_step_equals_the_original_u64_state() {
    let Some(image) = image() else { return };
    type U64State = unsafe extern "C" fn(*mut u64, u64) -> u64;
    // SAFETY: Random::U64State 0x14ddb30 is a leaf for n > 0 (the n == 0 path logs).
    let original: U64State = unsafe { std::mem::transmute(image.at(0x014d_db30)) };
    let mut rng = Rng::new(0x5eed_c0de);
    for _ in 0..10_000 {
        let start = rng.next_u64();
        let n = 1 + (rng.next_u64() % 1_000_000);
        let mut state = start;
        let value = unsafe { original(&mut state, n) };
        assert_eq!(state, rng_step(start));
        assert_eq!(value, rng_step(start) % n);
    }
}
