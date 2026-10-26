//! x4mp-sim binding of the per-sector AI driver ([`crate::system`]).
//!
//! Each x4mp-sim world (one sector) holds its AI state as a resource ([`AiSector`]). One AI pass is one
//! x4mp-sim phase of the multi-pass system [`AiPass`] (`max_passes` = [`MAX_PASSES`], the engine's cap):
//! the sector jobs run their due agents in parallel ([`run_pass`]), send every effect and every stop to
//! the barrier as a message, and request another pass when an agent ran; x4mp-sim's barrier orders the
//! messages by (order key, sector, emission sequence), which with key 0 is the sector order of
//! [`crate::system::run_frame`]. `Universe::step` repeats the pass until none is requested, so calls,
//! returns and cross-sector wakes made in a pass run in the next pass of the same frame, as in
//! `AIDirector::Move`.
//!
//! Status: the binding drives the synthetic test world (`tests/system_invariance.rs` checks that it
//! reproduces `system::run_frame` exactly for 1, 2 and 16 threads). The engine-backed [`Services`] (facts
//! from x4mp-sim columns and the snapshot, queries answered by native services) is the next step.

use crate::system::{
    complete_moves, run_pass, state_hash, EffectLog, Outbox, SectorAi, Services, SyntheticWorld, MAX_PASSES,
};
use crate::val::Uid;
use crate::world::Effect;
use std::sync::Mutex;
use x4mp_sim::{BarrierCtx, FrameInfo, Pool, SectorCtx, SectorIx, System, Target, Universe, UniverseId, WorldKind};

/// Name of the AI pass system (messages are addressed by it).
pub const SYSTEM_NAME: &str = "ai";

/// The AI state of one x4mp-sim world.
#[derive(Debug, Default)]
pub struct AiSector {
    pub ai: SectorAi,
    /// Agents that issued a `move_to` this frame (blocked until the movement system completes it).
    pub moving: Vec<Uid>,
}

/// An effect emitted by a sector job, in emission order (routed to the barrier).
#[derive(Debug)]
pub struct AiEffect {
    pub ent: Uid,
    pub line: u32,
    pub effect: Effect,
}

/// A behaviour that stopped (unsupported branch or internal error): the original must take over.
#[derive(Debug)]
pub struct AiStopped {
    pub ent: Uid,
    pub why: String,
}

/// The AI phase as an x4mp-sim multi-pass system.
pub struct AiPass<'s> {
    services: &'s dyn Services,
    log: Mutex<Outbox>,
}

impl<'s> AiPass<'s> {
    pub fn new(services: &'s dyn Services) -> AiPass<'s> {
        AiPass { services, log: Mutex::new(Outbox::default()) }
    }

    fn take_log(&self) -> Outbox {
        std::mem::take(&mut *self.log.lock().expect("ai log"))
    }
}

impl System for AiPass<'_> {
    fn name(&self) -> &'static str {
        SYSTEM_NAME
    }

    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let now = ctx.frame.game_time;
        let res = ctx.world.resource_mut::<AiSector>();
        let mut out = Outbox::default();
        // An agent ran: what it did may make others (or itself) due in the next pass.
        if run_pass(&mut res.ai, self.services, now, &mut res.moving, &mut out) {
            ctx.request_pass();
        }
        for (ent, line, effect) in out.effects {
            ctx.out.send(Target::Barrier, AiEffect { ent, line, effect });
        }
        for (ent, why) in out.stopped {
            ctx.out.send(Target::Barrier, AiStopped { ent, why });
        }
    }

    fn max_passes(&self) -> u32 {
        MAX_PASSES as u32
    }

    fn barrier(&self, ctx: &mut BarrierCtx<'_>) {
        let mut log = self.log.lock().expect("ai log");
        for m in ctx.messages {
            if let Some(e) = m.get::<AiEffect>() {
                log.effects.push((e.ent, e.line, e.effect.clone()));
            } else if let Some(s) = m.get::<AiStopped>() {
                log.stopped.push((s.ent, s.why.clone()));
            }
        }
    }
}

/// Add one x4mp-sim world per sector (in the given order, which must be engine tree order) and move
/// the sector AIs into them.
pub fn install(universe: &mut Universe, sectors: Vec<SectorAi>) -> Vec<SectorIx> {
    sectors
        .into_iter()
        .map(|s| {
            let ix = universe.add_sector(UniverseId(s.sector.0), WorldKind::Sector);
            universe.world_mut(ix).resource_mut::<AiSector>().ai = s;
            ix
        })
        .collect()
}

/// The sector AIs, in world order (for hashing and inspection).
pub fn sector_ais(universe: &Universe) -> Vec<SectorAi> {
    universe.worlds().iter().filter_map(|w| w.resource::<AiSector>()).map(|r| r.ai.clone()).collect()
}

/// One frame: AI passes until quiescent (one `Universe::step`). Returns the merged outbox (pass-major,
/// sector order inside a pass) and the entities that issued a `move_to` this frame (sector order).
pub fn run_frame_ecs(universe: &mut Universe, pool: &Pool, pass: &AiPass<'_>, frame: &FrameInfo) -> (Outbox, Vec<Uid>) {
    for i in 0..universe.sector_count() {
        universe.world_mut(SectorIx(i as u32)).resource_mut::<AiSector>().moving.clear();
    }
    universe.step(pool, &[pass as &dyn System], frame);
    let mut moved = Vec::new();
    for w in universe.worlds() {
        if let Some(r) = w.resource::<AiSector>() {
            moved.extend(r.moving.iter().copied());
        }
    }
    (pass.take_log(), moved)
}

/// [`crate::system::run_synthetic`] on the x4mp-sim scheduler with `threads` threads.
pub fn run_synthetic_ecs(
    w: &mut SyntheticWorld,
    ais: Vec<SectorAi>,
    session: u64,
    frames: u64,
    dt: f64,
    travel_frames: u64,
    threads: usize,
) -> (EffectLog, u64, Vec<(Uid, String)>) {
    let mut universe = Universe::bare(session);
    install(&mut universe, ais);
    let pool = Pool::new(threads);
    let mut log = Vec::new();
    let mut stopped = Vec::new();
    let mut in_flight: Vec<(u64, Uid)> = Vec::new();
    for f in 0..frames {
        w.facts.now = f as f64 * dt;
        let info = FrameInfo { frame: f, game_time: w.facts.now, dt };
        let (out, moved) = {
            let pass = AiPass::new(&*w);
            run_frame_ecs(&mut universe, &pool, &pass, &info)
        };
        log.extend(out.effects);
        stopped.extend(out.stopped);
        for m in moved {
            in_flight.push((f + travel_frames, m));
        }
        let arrived: Vec<Uid> = in_flight.iter().filter(|(t, _)| *t <= f).map(|(_, e)| *e).collect();
        in_flight.retain(|(t, _)| *t > f);
        for i in 0..universe.sector_count() {
            let r = universe.world_mut(SectorIx(i as u32)).resource_mut::<AiSector>();
            complete_moves(std::slice::from_mut(&mut r.ai), &arrived);
        }
    }
    let h = state_hash(&sector_ais(&universe), &log);
    (log, h, stopped)
}
