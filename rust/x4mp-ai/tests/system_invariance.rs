#![allow(clippy::type_complexity)]
//! Thread-invariance and stream-isolation tests of the per-sector AI driver (SYNTHETIC world:
//! stand-in engine services, see `system::SyntheticWorld`; no native X4 involved).

use x4mp_ai::system::{run_synthetic, synthetic_universe};
use x4mp_ai::val::Uid;
use x4mp_ai::world::Effect;

const SESSION: u64 = 276944566;

fn run(threads: usize, sectors: usize, ships: usize) -> (Vec<(Uid, u32, Effect)>, u64, Vec<(Uid, String)>) {
    // 20 s of game time at 30 fps; every move_to arrives after 3 s.
    run_frames(threads, sectors, ships, 600)
}

fn run_frames(
    threads: usize,
    sectors: usize,
    ships: usize,
    frames: u64,
) -> (Vec<(Uid, u32, Effect)>, u64, Vec<(Uid, String)>) {
    let (mut w, mut ais) = synthetic_universe(SESSION, sectors, 4, ships);
    run_synthetic(&mut w, &mut ais, frames, 1.0 / 30.0, 90, threads)
}

#[test]
fn one_and_n_threads_give_identical_results() {
    let (log1, h1, st1) = run(1, 8, 6);
    assert!(st1.is_empty(), "behaviours stopped: {st1:?}");
    // The run must exercise the order: legs, waits, reservations, run_script calls.
    let moves = log1.iter().filter(|(_, _, e)| matches!(e, Effect::MoveTo { .. })).count();
    assert!(moves >= 8 * 6 * 3, "only {moves} move_to effects");
    for threads in [2, 3, 8, 16] {
        let (log, h, st) = run(threads, 8, 6);
        assert_eq!(st, st1);
        assert_eq!(log.len(), log1.len(), "threads={threads}");
        assert_eq!(log, log1, "threads={threads}");
        assert_eq!(h, h1, "threads={threads}");
    }
}

#[test]
fn an_entity_does_not_depend_on_its_neighbours() {
    // Per-entity streams: the first ship of every sector behaves the same whether 5 others exist.
    let (alone, _, _) = run(4, 3, 1);
    let (crowded, _, _) = run(4, 3, 6);
    for sector in 0..3u64 {
        let pilot = Uid(0x10000 + 0x1000 * sector + 0x800);
        let a: Vec<_> = alone.iter().filter(|(e, _, _)| *e == pilot).collect();
        let b: Vec<_> = crowded.iter().filter(|(e, _, _)| *e == pilot).collect();
        assert!(!a.is_empty());
        assert_eq!(a, b, "sector {sector}");
    }
}
