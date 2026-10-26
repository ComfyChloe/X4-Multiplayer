//! Thread invariance of the per-sector parallel view-cache build: for random engine-like
//! universes and several frames, the parallel run with 1, 2, 4 and 8 threads must produce
//! exactly the sequential event log and exactly the sequential final state, including
//! frames that fall back because of stale child vectors.

use x4mp_transform::cache::{frame, NoGetters};
use x4mp_transform::parallel::{frame_parallel, Hazard};
use x4mp_transform::scenario::{generate, Scenario, Shape};

fn state(s: &Scenario, tree: &x4mp_transform::cache::CacheTree) -> Vec<u8> {
    let mut c = s.clone();
    c.tree = tree.clone();
    c.to_bytes()
}

fn check(seed: u64, shape: &Shape) -> (usize, usize, usize) {
    let s = generate(seed, shape);
    let mut seq = s.tree.clone();
    let mut par: Vec<_> = [1usize, 2, 4, 8].iter().map(|_| s.tree.clone()).collect();
    let (mut tasks, mut fallbacks, mut events) = (0, 0, 0);
    for k in 0..s.frames {
        let idx = (s.first_idx ^ (k & 1)) as usize;
        let time = s.time0 + k as f64 * s.dt;
        let expect = match frame(&mut seq, s.root, idx, time, &mut NoGetters) {
            Ok(l) => l,
            Err(_) => return (tasks, fallbacks, events),
        };
        events += expect.len();
        for (t, tree) in [1usize, 2, 4, 8].iter().zip(par.iter_mut()) {
            let r = frame_parallel(tree, s.root, idx, time, *t, 0x58);
            assert_eq!(r.log, expect, "seed {seed} frame {k} threads {t}: log differs");
            assert_eq!(state(&s, tree), state(&s, &seq), "seed {seed} frame {k} threads {t}: state differs");
            if *t == 4 {
                tasks += r.tasks;
                if let Some(h) = r.fallback {
                    assert!(!matches!(h, Hazard::Overrun(_)));
                    fallbacks += 1;
                }
            }
        }
    }
    (tasks, fallbacks, events)
}

#[test]
fn parallel_build_matches_sequential_on_clean_universes() {
    let shape = Shape { oracle_safe: false, stale: false, frames: 5, ..Shape::default() };
    let (mut tasks, mut fallbacks, mut events) = (0, 0, 0);
    for seed in 0..120 {
        let (t, f, e) = check(seed, &shape);
        tasks += t;
        fallbacks += f;
        events += e;
    }
    eprintln!("clean: {tasks} tasks, {fallbacks} fallback frames, {events} events");
    assert!(tasks > 1000, "the split must actually happen");
    assert_eq!(fallbacks, 0, "clean universes never need the fallback");
}

#[test]
fn parallel_build_detects_stale_child_vectors() {
    let shape = Shape { oracle_safe: false, stale: true, frames: 4, ..Shape::default() };
    let (mut tasks, mut fallbacks) = (0, 0);
    for seed in 1000..1120 {
        let (t, f, _) = check(seed, &shape);
        tasks += t;
        fallbacks += f;
    }
    eprintln!("stale: {tasks} tasks, {fallbacks} fallback frames");
    assert!(fallbacks > 0, "stale vectors that cross sectors must trigger the fallback");
}

#[test]
fn large_universe_many_threads() {
    let shape =
        Shape { clusters: 8, sectors: 4, zones: 4, objects: 10, depth: 3, frames: 3, oracle_safe: false, stale: false };
    for seed in [5u64, 77, 901] {
        let (t, f, e) = check(seed, &shape);
        assert!(t > 0 && e > 0);
        assert_eq!(f, 0);
    }
}
