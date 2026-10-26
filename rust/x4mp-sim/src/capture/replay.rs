//! Capture replay (method step 2 for every system agent): load engine frame k with the
//! mirror, run the replacement systems for one frame, compare what they produced with the
//! engine's frame k+1 under the exactness policy.
//!
//! ```ignore
//! let setup = ReplaySetup {
//!     make: &|| { let mut u = Universe::new(seed); u.register::<MyState>(); u },
//!     prepare: None,                       // fill system state that is not in the capture
//!     systems: &[&my_system],
//!     columns: &["transform", "velocity"], // what the systems produce
//!     policy: Policy::exact(),             // bit-exact unless a documented tolerance applies
//!     structure: false,                    // the engine spawns/destroys; compare common entities
//! };
//! let rep = replay_frames(&frames, &factions, &setup, &Pool::new(0));
//! println!("{}", rep.line("my-system"));    // REPLAYCAP my-system pairs=.. mismatched_pairs=.. ...
//! ```
//! Only consecutive frame pairs (frame k, k+1) are replayed, with `FrameInfo { frame: k+1,
//! game_time: t(k+1), dt: t(k+1) - t(k) }`, which is the engine's TLS+0x300 - TLS+0x328 for frame
//! k+1. Other pairs, and any pair containing a subset or truncated frame, are counted as
//! skipped. Replaying an isolated subset would omit cross-sector dependencies.

use super::{mirror, FrameRecord};
use crate::sched::{FrameInfo, Pool, System};
use crate::shadow::{Comparator, Policy};
use crate::universe::Universe;
use crate::world::CompareOpts;

/// Fills system-owned state after the mirror loaded a frame.
pub type Prepare<'a> = &'a dyn Fn(&FrameRecord, &mut Universe);

/// What to replay and how to compare.
pub struct ReplaySetup<'a> {
    /// An empty universe with every component the systems need (core components + their own).
    pub make: &'a dyn Fn() -> Universe,
    /// Fills system-owned state that the capture does not carry (called after the mirror).
    pub prepare: Option<Prepare<'a>>,
    pub systems: &'a [&'a dyn System],
    /// Columns the systems produce (compared); empty = all registered columns.
    pub columns: &'a [&'a str],
    pub policy: Policy,
    /// Also compare entity sets and row order (false: entities present in both frames only).
    pub structure: bool,
}

/// Result of [`replay_frames`].
#[derive(Debug, Clone)]
pub struct CaptureReplayReport {
    pub pairs: u64,
    pub pairs_mismatched: u64,
    pub first_mismatched_frame: Option<u64>,
    /// Non-consecutive pairs and frames the mirror refused.
    pub skipped: u64,
    pub comparator: Comparator,
}

impl CaptureReplayReport {
    pub fn is_clean(&self) -> bool {
        self.pairs > 0 && self.pairs_mismatched == 0
    }

    /// `REPLAYCAP <name> pairs=.. mismatched_pairs=.. skipped=.. compared=.. mismatched=.. [first=[..]]`
    pub fn line(&self, name: &str) -> String {
        let mut s = format!(
            "REPLAYCAP {name} pairs={} mismatched_pairs={} skipped={} compared={} mismatched={}",
            self.pairs,
            self.pairs_mismatched,
            self.skipped,
            self.comparator.compared(),
            self.comparator.mismatched()
        );
        if let Some(d) = self.comparator.first() {
            s.push_str(&format!(" first=[{d}]"));
        }
        s
    }
}

/// Replays every consecutive pair of `frames` (see the module docs).
pub fn replay_frames(
    frames: &[FrameRecord],
    factions: &[(u32, &str)],
    setup: &ReplaySetup<'_>,
    pool: &Pool,
) -> CaptureReplayReport {
    let mut rep = CaptureReplayReport {
        pairs: 0,
        pairs_mismatched: 0,
        first_mismatched_frame: None,
        skipped: 0,
        comparator: Comparator::new(setup.policy.clone()),
    };
    let opts = CompareOpts { columns: setup.columns, structure: setup.structure };
    for pair in frames.windows(2) {
        let (a, b) = (&pair[0], &pair[1]);
        if b.frame != a.frame + 1 {
            rep.skipped += 1;
            continue;
        }
        let mut ours = (setup.make)();
        if mirror(a, factions, &mut ours).is_err() {
            rep.skipped += 1;
            continue;
        }
        if let Some(prepare) = setup.prepare {
            prepare(a, &mut ours);
        }
        let fi = FrameInfo { frame: b.frame, game_time: b.game_time, dt: b.game_time - a.game_time };
        ours.step(pool, setup.systems, &fi);
        let mut engine = (setup.make)();
        if mirror(b, factions, &mut engine).is_err() {
            rep.skipped += 1;
            continue;
        }
        let before = rep.comparator.mismatched();
        engine.compare_with(&ours, &opts, b.frame, &mut rep.comparator);
        rep.pairs += 1;
        if rep.comparator.mismatched() != before {
            rep.pairs_mismatched += 1;
            rep.first_mismatched_frame.get_or_insert(b.frame);
        }
    }
    rep
}
