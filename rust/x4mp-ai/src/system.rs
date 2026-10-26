//! Per-sector AI driver (authoritative mode) and its thread-invariance test bed.
//!
//! A frame runs in passes like `AIDirector::Move` (calls, returns and wakes made in a pass run in the
//! next pass of the same frame; at most 0x40 passes). In a pass each sector job runs the due agents of
//! its sector in engine tree order; an agent reads only immutable world data ([`Services`]) and its own
//! state, draws from its own stream (`rng::stream_seed(session, uid, 'AISC')`) and writes effects into
//! its sector's [`Outbox`]. The barrier after every pass applies all outboxes in sector order, then
//! emission order, which is the order a sequential run produces; so 1 and N threads give identical
//! results by construction (tested below).
//!
//! The ECS binding (x4mp-sim `System::run_sector` / `barrier`, rewrite-coord/interfaces.md v0) is a thin
//! adapter over [`run_pass`] and the per-pass barrier of [`run_frame`]; it is not wired yet because
//! x4mp-sim is not on the base branch.

use crate::replay::FactStore;
use crate::rng::{stream_seed, Stream, PURPOSE_AI_SCRIPT};
use crate::runtime::{Agent, Frame, RetVals, Waiting, Yield};
use crate::val::{OrderId, Pos, Time, Uid, Val};
use crate::world::{Answer, Effect, Facts, Io, Prop, Query, Stop, R};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};

/// Engine services a behaviour can query, implemented natively (read-only during the parallel phase).
pub trait Services: Facts + Sync {
    /// Answer an engine query for `ent`. Draws the engine makes inside the service are made on the
    /// entity's stream (`rng`), so they stay per entity.
    fn answer(&self, ent: Uid, q: &Query, rng: &mut Stream) -> Answer;

    /// A called script that is not ported yet (`lib.target.selection`, `move.generic`): in the engine
    /// binding the original interpreter runs it (hybrid mode); here the service returns its result.
    /// `None` = refuse (the agent stops and the original must take over the context).
    fn opaque_call(&self, ent: Uid, script: &str, rng: &mut Stream) -> Option<RetVals>;
}

/// One sector job's exclusive borrows during a parallel pass.
type Job<'a> = (&'a mut SectorAi, &'a mut Outbox, &'a mut Vec<Uid>, &'a mut bool);

/// Effect log entries: entity, XML line, effect.
pub type EffectLog = Vec<(Uid, u32, Effect)>;

/// Effects of one sector job, in emission order.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Outbox {
    pub effects: Vec<(Uid, u32, Effect)>,
    /// Behaviours that stopped (unsupported branch or internal error); the original must take over.
    pub stopped: Vec<(Uid, String)>,
}

struct NativeIo<'a> {
    services: &'a dyn Services,
    out: &'a mut Outbox,
    rolls: u64,
}

impl Io for NativeIo<'_> {
    fn roll(&mut self, _ent: Uid, _line: u32, _chance: f32, _pass: bool) -> R<()> {
        self.rolls += 1;
        Ok(())
    }
    fn rnd(&mut self, _ent: Uid, _line: u32, _v: f64) -> R<()> {
        Ok(())
    }
    fn query(&mut self, ent: Uid, _line: u32, q: &Query, rng: &mut Stream) -> R<Answer> {
        Ok(self.services.answer(ent, q, rng))
    }
    fn effect(&mut self, ent: Uid, line: u32, e: &Effect, _rng: &mut Stream) -> R<()> {
        self.out.effects.push((ent, line, e.clone()));
        Ok(())
    }
}

/// The AI state of one sector.
#[derive(Clone, Debug, Default)]
pub struct SectorAi {
    pub sector: Uid,
    /// Agents in engine tree order.
    pub agents: Vec<Agent>,
}

/// Maximum passes per frame (`AIDirector::Move` caps at 0x40 and warns about a wake loop at pass 0x20;
/// see aidriver.h AD_PASS_LIMIT / AD_LOOP_PASS).
pub const MAX_PASSES: usize = 0x40;

fn due(a: &Agent, now: Time) -> bool {
    match a.waiting.last() {
        Some(Waiting::Start | Waiting::Return | Waiting::Restart | Waiting::BlockDone) => true,
        Some(Waiting::Timer) => a.wake_at.is_some_and(|t| t <= now),
        _ => false,
    }
}

/// Run one pass over a sector's due agents at game time `now`; returns whether any agent ran. Blocked
/// `move_to`s are completed by the movement system, which reports them through [`complete_moves`].
pub fn run_pass(s: &mut SectorAi, services: &dyn Services, now: Time, moving: &mut Vec<Uid>, out: &mut Outbox) -> bool {
    let mut io = NativeIo { services, out, rolls: 0 };
    let mut ran = false;
    for a in s.agents.iter_mut() {
        if a.stack.is_empty() || moving.contains(&a.ent) || !due(a, now) {
            continue;
        }
        ran = true;
        if let Some(Frame::Opaque { script }) = a.stack.last() {
            let script = script.clone();
            let _ = a.next_wake();
            match services.opaque_call(a.ent, &script, &mut a.rng) {
                Some(vals) => a.after_yield(&Yield::Return(vals)),
                None => {
                    io.out.stopped.push((a.ent, format!("opaque callee {script} refused")));
                    a.stack.clear();
                    a.waiting.clear();
                }
            }
            continue;
        }
        match a.run(services, &mut io) {
            Ok(Yield::MoveTo) => moving.push(a.ent),
            Ok(_) => {}
            Err(Stop::Unsupported { script, line, what }) => {
                let why = format!("{script} L{line}: {what}");
                io.out.stopped.push((a.ent, why.clone()));
                a.stopped = Some(why);
                a.stack.clear();
                a.waiting.clear();
            }
            Err(Stop::Mismatch(m)) => {
                io.out.stopped.push((a.ent, m));
                a.stack.clear();
                a.waiting.clear();
            }
        }
    }
    ran
}

/// Barrier: the movement system finished these `move_to`s (arrival); their agents wake next frame.
pub fn complete_moves(sectors: &mut [SectorAi], arrived: &[Uid]) {
    for s in sectors.iter_mut() {
        for a in s.agents.iter_mut() {
            if arrived.contains(&a.ent) {
                if let Some(w) = a.waiting.last_mut() {
                    if *w == Waiting::Block {
                        *w = Waiting::BlockDone;
                    }
                }
            }
        }
    }
}

/// Run all sectors for one frame on `threads` worker threads, pass by pass like `AIDirector::Move`:
/// every pass runs the sectors in parallel, then a barrier merges the pass's outboxes in sector order
/// (pass-major order, which is the order of a sequential engine frame). The barrier is where a wake
/// that one sector's pass makes for another sector's entity belongs, so that it runs in the next pass as
/// in the engine (the census measured a third of the wakes during runs crossing sectors; the synthetic
/// world makes none yet). Returns the merged outbox and the entities that issued a `move_to` this frame
/// (sector order).
pub fn run_frame(sectors: &mut [SectorAi], services: &dyn Services, now: Time, threads: usize) -> (Outbox, Vec<Uid>) {
    let n = sectors.len();
    let mut moves: Vec<Vec<Uid>> = vec![Vec::new(); n];
    let mut merged = Outbox::default();
    for _pass in 0..MAX_PASSES {
        // Nothing due anywhere: the frame is done (saves starting the workers for an empty pass).
        let any_due = sectors
            .iter()
            .zip(&moves)
            .any(|(s, m)| s.agents.iter().any(|a| !a.stack.is_empty() && !m.contains(&a.ent) && due(a, now)));
        if !any_due {
            break;
        }
        let mut outs: Vec<Outbox> = vec![Outbox::default(); n];
        let mut ran: Vec<bool> = vec![false; n];
        if threads <= 1 {
            for (i, s) in sectors.iter_mut().enumerate() {
                ran[i] = run_pass(s, services, now, &mut moves[i], &mut outs[i]);
            }
        } else {
            // Work stealing over sector indices; each job owns its sector, outbox and move list.
            let next = AtomicUsize::new(0);
            let slots: Vec<std::sync::Mutex<Option<Job<'_>>>> = sectors
                .iter_mut()
                .zip(outs.iter_mut())
                .zip(moves.iter_mut())
                .zip(ran.iter_mut())
                .map(|(((s, o), m), r)| std::sync::Mutex::new(Some((s, o, m, r))))
                .collect();
            std::thread::scope(|sc| {
                for _ in 0..threads.min(n.max(1)) {
                    sc.spawn(|| loop {
                        let i = next.fetch_add(1, Ordering::Relaxed);
                        if i >= n {
                            break;
                        }
                        let job = slots[i].lock().expect("slot").take();
                        if let Some((s, o, m, r)) = job {
                            *r = run_pass(s, services, now, m, o);
                        }
                    });
                }
            });
        }
        // Barrier: sector order, then emission order. Cross-entity wakes would be applied here; the
        // synthetic world has none (its signals target the sender's own ship).
        for o in outs {
            merged.effects.extend(o.effects);
            merged.stopped.extend(o.stopped);
        }
        if !ran.iter().any(|r| *r) {
            break;
        }
    }
    let moved = moves.into_iter().flatten().collect();
    (merged, moved)
}

/// FNV-1a 64 over everything that must not depend on the thread count.
pub fn state_hash(sectors: &[SectorAi], log: &[(Uid, u32, Effect)]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    let mut eat = |b: &[u8]| {
        for x in b {
            h ^= *x as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    for s in sectors {
        eat(&s.sector.0.to_le_bytes());
        for a in &s.agents {
            eat(&a.ent.0.to_le_bytes());
            eat(&a.rng.state().to_le_bytes());
            eat(&a.rng.draws().to_le_bytes());
            eat(&(a.stack.len() as u64).to_le_bytes());
            eat(&a.wake_at.unwrap_or(-1.0).to_bits().to_le_bytes());
            for f in &a.stack {
                eat(f.name().as_bytes());
            }
        }
    }
    for (e, line, fx) in log {
        eat(&e.0.to_le_bytes());
        eat(&line.to_le_bytes());
        eat(fx.to_record(*e).to_string().as_bytes());
    }
    h
}

/// A synthetic world for the test bed: sectors with normal zones, patrol job ships. Its engine
/// services are deterministic stand-ins (NOT the engine's algorithms: positions are drawn from the
/// entity stream with a formula of our own). Facts come from a [`FactStore`].
#[derive(Clone, Debug, Default)]
pub struct SyntheticWorld {
    pub facts: FactStore,
    /// Zones per sector, in tree order, with their sector-space centre.
    pub zones: BTreeMap<u64, Vec<(Uid, Pos)>>,
    /// Ship positions (sector space).
    pub ship_pos: BTreeMap<u64, Pos>,
}

impl Facts for SyntheticWorld {
    fn now(&self) -> Time {
        self.facts.now
    }
    fn get(&self, s: Uid, p: Prop) -> Val {
        Facts::get(&self.facts, s, p)
    }
    fn order_get(&self, o: OrderId, p: Prop) -> Val {
        Facts::order_get(&self.facts, o, p)
    }
    fn get_args(&self, s: Uid, p: Prop, args: &[Val]) -> Val {
        Facts::get_args(&self.facts, s, p, args)
    }
}

fn dist(a: Pos, b: Pos) -> f32 {
    let (dx, dy, dz) = (a.x - b.x, a.y - b.y, a.z - b.z);
    (dx * dx + dy * dy + dz * dz).sqrt()
}

impl Services for SyntheticWorld {
    fn answer(&self, ent: Uid, q: &Query, rng: &mut Stream) -> Answer {
        let _ = ent;
        match q {
            Query::FindZone { space, normalzone: _, knownto: _, min_distance, not_highway: _ } => {
                let zs = self.zones.get(&space.0).cloned().unwrap_or_default();
                let pick = zs.iter().find(|(_, c)| match min_distance {
                    Some((o, d)) => self.ship_pos.get(&o.0).is_none_or(|p| dist(*p, *c) >= *d),
                    None => true,
                });
                Answer::Zone(pick.map(|(z, _)| *z).unwrap_or(Uid::NULL))
            }
            Query::GlobalPath { end, .. } => Answer::Path(vec![*end]),
            Query::CreatePosition { space: _, max } => {
                let x = rng.float(2.0 * max) - max;
                let z = rng.float(2.0 * max) - max;
                Answer::Pos(Pos::new(x, 0.0, z))
            }
            Query::CreatePositionIn { space, value, object } => {
                let c = self
                    .zones
                    .get(&space.0)
                    .and_then(|zs| zs.iter().find(|(z, _)| z == object).map(|(_, c)| *c))
                    .unwrap_or_default();
                Answer::Pos(Pos::new(c.x + value.x, c.y + value.y, c.z + value.z))
            }
            // The synthetic world has no hostiles.
            Query::GravidarContacts { .. } => Answer::Path(Vec::new()),
            Query::SafePos { value, .. } => Answer::Pos(*value),
            Query::SafePosReserve { value, space, component, .. } => {
                Answer::Reserved { pos: *value, index: Val::Int((component.0 & 0xff) as i64), zone: *space }
            }
            // Stand-ins for move.generic (the synthetic patrols stay inside their sector).
            Query::JumpPath { start, end, .. } => Answer::Path(vec![*start, *end]),
            Query::ZoneAtPosition { sector, .. } => {
                Answer::Zone(self.zones.get(&sector.0).and_then(|zs| zs.first()).map(|(z, _)| *z).unwrap_or(Uid::NULL))
            }
            Query::FindSector { space } => Answer::Zone(*space),
            Query::SafePosGeneral { value, space, reserve, .. } => {
                let pos = value.unwrap_or_default();
                match reserve {
                    Some((c, _)) => Answer::Reserved { pos, index: Val::Int((c.0 & 0xff) as i64), zone: *space },
                    None => Answer::Pos(pos),
                }
            }
            Query::CountGravidarContacts { .. } => Answer::Component(Uid::NULL),
            Query::MoveTo { destination, .. } => Answer::Component(*destination),
        }
    }

    fn opaque_call(&self, _ent: Uid, script: &str, rng: &mut Stream) -> Option<RetVals> {
        match script {
            // Stand-in: one draw (as if a debug roll ran), no target found.
            "lib.target.selection" => {
                rng.skip(1);
                Some(vec![("target".into(), Val::Null)])
            }
            "move.generic" => Some(vec![("v".into(), Val::Bool(true))]),
            _ => None,
        }
    }
}

/// Build a synthetic universe: `sectors` sectors with `zones` normal zones each, and `ships` patrol
/// job ships per sector (alternating `range` zone and sector). Returns the world and the sector AIs
/// with agents seeded from `session`.
pub fn synthetic_universe(session: u64, sectors: usize, zones: usize, ships: usize) -> (SyntheticWorld, Vec<SectorAi>) {
    use crate::orders::patrol::{Patrol, PatrolParams};
    let mut w = SyntheticWorld::default();
    let mut ais = Vec::new();
    // Stable ids: an entity's uid depends only on its (sector, index), not on how many exist.
    let cluster = Uid(0x10);
    w.facts.set(cluster, Prop::Class, Val::key("cluster"));
    for si in 0..sectors {
        let sbase = 0x10000 + 0x1000 * si as u64;
        let sector = Uid(sbase);
        w.facts.set(sector, Prop::Class, Val::key("sector"));
        w.facts.set(sector, Prop::Cluster, Val::Uid(cluster));
        let mut zl = Vec::new();
        for zi in 0..zones {
            let z = Uid(sbase + 0x100 + zi as u64);
            w.facts.set(z, Prop::Class, Val::key("zone"));
            w.facts.set(z, Prop::Sector, Val::Uid(sector));
            w.facts.set(z, Prop::Cluster, Val::Uid(cluster));
            w.facts.set(z, Prop::IsNormalZone, Val::Bool(true));
            w.facts.set(z, Prop::Size, Val::F32(20000.0 + 1000.0 * zi as f32));
            zl.push((z, Pos::new(50000.0 * zi as f32, 0.0, 10000.0 * si as f32)));
        }
        w.zones.insert(sector.0, zl.clone());
        let mut s = SectorAi { sector, agents: Vec::new() };
        for k in 0..ships {
            let pilot = Uid(sbase + 0x800 + 2 * k as u64);
            let ship = Uid(pilot.0 + 1);
            let home = zl[k % zl.len()].0;
            w.facts.set(pilot, Prop::Ship, Val::Uid(ship));
            w.facts.set(pilot, Prop::Zone, Val::Uid(home));
            w.facts.set(pilot, Prop::Sector, Val::Uid(sector));
            w.facts.set(pilot, Prop::Cluster, Val::Uid(cluster));
            w.facts.set(ship, Prop::Class, Val::key(if k % 3 == 0 { "ship_l" } else { "ship_m" }));
            w.facts.set(ship, Prop::Zone, Val::Uid(home));
            w.facts.set(ship, Prop::Sector, Val::Uid(sector));
            w.facts.set(ship, Prop::JobMainZone, Val::Uid(home));
            w.facts.set(ship, Prop::IsJobShip, Val::Bool(true));
            w.facts.set(ship, Prop::Size, Val::F32(60.0));
            w.facts.set(ship, Prop::SafeSize, Val::F32(120.0));
            w.facts.set(ship, Prop::IsOperational, Val::Bool(true));
            w.ship_pos.insert(ship.0, zl[k % zl.len()].1);
            let range = if k % 2 == 0 { "class.zone" } else { "class.sector" };
            let params = vec![("range".to_string(), Val::key(range))];
            let p = PatrolParams::from_params(&params, 8000.0).expect("params");
            let mut a = Agent::new(pilot, stream_seed(session, pilot.0, PURPOSE_AI_SCRIPT));
            let mut sink = Outbox::default();
            let mut io = NativeIo { services: &w, out: &mut sink, rolls: 0 };
            a.start_order(Frame::Patrol(Box::new(Patrol::new(p, ship))), &w, &mut io).expect("init");
            s.agents.push(a);
        }
        ais.push(s);
    }
    (w, ais)
}

/// Run the synthetic universe for `frames` frames of `dt` seconds; every `move_to` arrives after
/// `travel_frames` frames. Returns the effect log and the final state hash.
pub fn run_synthetic(
    w: &mut SyntheticWorld,
    ais: &mut [SectorAi],
    frames: u64,
    dt: f64,
    travel_frames: u64,
    threads: usize,
) -> (EffectLog, u64, Vec<(Uid, String)>) {
    let mut log = Vec::new();
    let mut stopped = Vec::new();
    let mut in_flight: Vec<(u64, Uid)> = Vec::new();
    for f in 0..frames {
        w.facts.now = f as f64 * dt;
        let (out, moved) = run_frame(ais, w, w.facts.now, threads);
        log.extend(out.effects);
        stopped.extend(out.stopped);
        for m in moved {
            in_flight.push((f + travel_frames, m));
        }
        let arrived: Vec<Uid> = in_flight.iter().filter(|(t, _)| *t <= f).map(|(_, e)| *e).collect();
        in_flight.retain(|(t, _)| *t > f);
        complete_moves(ais, &arrived);
    }
    let h = state_hash(ais, &log);
    (log, h, stopped)
}
