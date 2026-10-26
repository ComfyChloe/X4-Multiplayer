//! The x4mp-sim binding of the AI driver (SYNTHETIC world, see `system::SyntheticWorld`). A separate test
//! binary: x4mp-sim's worker pool spins, so it must not share the machine with the other driver tests.

use x4mp_ai::ecs::run_synthetic_ecs;
use x4mp_ai::system::{run_synthetic, synthetic_universe};

const SESSION: u64 = 276944566;

#[test]
fn the_x4mp_sim_binding_reproduces_the_driver() {
    // Same universe on x4mp-sim's scheduler (multi-pass system, one Universe::step per frame): identical effect log,
    // stops and state hash for 1, 3 and 8 threads.
    // 8 s of game time: first legs, waits and reservations of every ship.
    let (mut w0, mut ais0) = synthetic_universe(SESSION, 8, 4, 6);
    let (log1, h1, st1) = run_synthetic(&mut w0, &mut ais0, 240, 1.0 / 30.0, 90, 1);
    assert!(log1.len() > 8 * 6 * 4, "only {} effects", log1.len());
    for threads in [1, 3, 8] {
        let (mut w, ais) = synthetic_universe(SESSION, 8, 4, 6);
        let (log, h, st) = run_synthetic_ecs(&mut w, ais, SESSION, 240, 1.0 / 30.0, 90, threads);
        assert_eq!(st, st1, "threads={threads}");
        assert_eq!(log.len(), log1.len(), "threads={threads}");
        assert_eq!(log, log1, "threads={threads}");
        assert_eq!(h, h1, "threads={threads}");
    }
}
