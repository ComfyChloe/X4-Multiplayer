//! The universe: all sector worlds in tree order, the id map, the component registry, and
//! the frame step (parallel phase per system, then the ordered barrier).

use std::any::TypeId;
use std::cmp::Reverse;
use std::collections::BTreeMap;
use std::time::Instant;

use crate::bus::{Command, Envelope, Item, Message, Outbox, Target};
use crate::column::Bundle;
use crate::comp::{self, Component, RngState};
use crate::hash::{Fnv64, StableHash};
use crate::id::{EntityId, IdMap, Loc, OrderKey, SectorIx, UniverseId};
use crate::rng::entity_seed;
use crate::sched::{BarrierCtx, FrameInfo, FrameReport, PhaseReport, Pool, SectorCtx, System};
use crate::shadow::Comparator;
use crate::snapshot::Snapshot;
use crate::world::{ColInfo, StructError, World, WorldKind, STAGED_ROW};

/// Sender index of messages posted serially (between frames or from a barrier hook).
pub const SERIAL_SENDER: SectorIx = SectorIx(u32::MAX);

/// All sector worlds and the shared registries. See the crate docs for the frame model.
pub struct Universe {
    worlds: Vec<World>,
    ids: IdMap,
    registry: Vec<ColInfo>,
    published: Vec<TypeId>,
    snapshot: Snapshot,
    session_seed: u64,
    phase: u64,
    factions: BTreeMap<u16, String>,
    log_messages: bool,
    message_log: Vec<String>,
    serial_seq: u32,
    barrier_inboxes: Vec<(&'static str, Vec<Envelope>)>,
    hashed_resources: Vec<(TypeId, ResourceHasher)>,
}

type ResourceHasher = fn(&(dyn std::any::Any + Send + Sync), &mut Fnv64);

fn hash_resource_as<R: std::any::Any + StableHash>(r: &(dyn std::any::Any + Send + Sync), h: &mut Fnv64) {
    if let Some(v) = r.downcast_ref::<R>() {
        v.stable_hash(h);
    }
}

impl Universe {
    /// A universe with the core components registered (see [`comp`]).
    pub fn new(session_seed: u64) -> Universe {
        let mut u = Universe::bare(session_seed);
        u.register::<comp::ClassId>()
            .register::<comp::ClassFlags>()
            .register::<comp::Attention>()
            .register::<comp::Tree>()
            .register::<comp::EngineFlags>()
            .register::<comp::Controller>()
            .register::<comp::Transform>()
            .register::<comp::Velocity>()
            .register::<comp::PoseRef>()
            .register::<comp::Hull>()
            .register::<comp::Owner>()
            .register::<comp::OrderState>()
            .register::<comp::RngState>();
        u
    }

    /// A universe without any registered component.
    pub fn bare(session_seed: u64) -> Universe {
        Universe {
            worlds: Vec::new(),
            ids: IdMap::new(),
            registry: Vec::new(),
            published: Vec::new(),
            snapshot: Snapshot::default(),
            session_seed,
            phase: 0,
            factions: BTreeMap::new(),
            log_messages: false,
            message_log: Vec::new(),
            serial_seq: 0,
            barrier_inboxes: Vec::new(),
            hashed_resources: Vec::new(),
        }
    }

    /// Registers a component type (a dense column in every world; existing rows get
    /// `Default`). Registering twice is a no-op.
    pub fn register<T: Component>(&mut self) -> &mut Universe {
        if self.registry.iter().any(|c| c.type_id == TypeId::of::<T>()) {
            return self;
        }
        let info = ColInfo::of::<T>();
        for w in &mut self.worlds {
            w.add_column(&info);
        }
        self.registry.push(info);
        self
    }

    /// Makes `T` readable across sectors through the phase snapshot (registers it if needed).
    pub fn publish<T: Component>(&mut self) -> &mut Universe {
        self.register::<T>();
        let id = TypeId::of::<T>();
        if let Some(info) = self.registry.iter_mut().find(|c| c.type_id == id) {
            info.published = true;
        }
        self.published = self.registry.iter().filter(|c| c.published).map(|c| c.type_id).collect();
        self
    }

    /// Includes the per-sector resource `R` (see [`World::resource_mut`]) in
    /// [`Universe::state_hash`], so thread-invariance checks cover system state kept there.
    pub fn hash_resource<R: std::any::Any + Send + Sync + StableHash>(&mut self) -> &mut Universe {
        let id = TypeId::of::<R>();
        if !self.hashed_resources.iter().any(|(t, _)| *t == id) {
            self.hashed_resources.push((id, hash_resource_as::<R>));
        }
        self
    }

    pub fn is_registered<T: Component>(&self) -> bool {
        self.registry.iter().any(|c| c.type_id == TypeId::of::<T>())
    }

    /// Registered component names in column order.
    pub fn component_names(&self) -> Vec<&'static str> {
        self.registry.iter().map(|c| c.name).collect()
    }

    /// Appends a world (tree order = call order).
    pub fn add_sector(&mut self, uid: UniverseId, kind: WorldKind) -> SectorIx {
        let ix = SectorIx(u32::try_from(self.worlds.len()).expect("too many sectors"));
        self.worlds.push(World::new(ix, uid, kind, &self.registry));
        ix
    }

    /// Pinned worlds always run on the thread that calls `step` (the engine thread).
    pub fn set_pinned(&mut self, s: SectorIx, pinned: bool) {
        self.worlds[s.index()].pinned = pinned;
    }

    pub fn sector_count(&self) -> usize {
        self.worlds.len()
    }

    pub fn worlds(&self) -> &[World] {
        &self.worlds
    }

    pub fn world(&self, s: SectorIx) -> &World {
        &self.worlds[s.index()]
    }

    pub fn world_mut(&mut self, s: SectorIx) -> &mut World {
        &mut self.worlds[s.index()]
    }

    pub fn sector_by_uid(&self, uid: UniverseId) -> Option<SectorIx> {
        self.worlds.iter().find(|w| w.root_uid() == uid).map(|w| w.sector())
    }

    pub fn ids(&self) -> &IdMap {
        &self.ids
    }

    pub fn session_seed(&self) -> u64 {
        self.session_seed
    }

    pub fn entity_count(&self) -> usize {
        self.worlds.iter().map(|w| w.len()).sum()
    }

    /// Faction dictionary (index -> faction id string), e.g. from a capture.
    pub fn factions(&self) -> &BTreeMap<u16, String> {
        &self.factions
    }

    pub fn set_faction(&mut self, ix: u16, name: &str) {
        self.factions.insert(ix, name.to_string());
    }

    // --- structure (staged until commit) -------------------------------------------------

    fn world_checked(&mut self, s: SectorIx) -> Result<&mut World, StructError> {
        self.worlds.get_mut(s.index()).ok_or(StructError::NoSuchSector(s))
    }

    /// Spawns at the end of the sector's tree order. Visible after [`Universe::commit`]
    /// (`step` commits first).
    pub fn spawn(&mut self, s: SectorIx, uid: UniverseId, bundle: Bundle) -> Result<EntityId, StructError> {
        let key = self.world_checked(s)?.last_key().map_or(OrderKey(0), |k| OrderKey(k.0 + 1));
        self.spawn_at(s, uid, key, bundle)
    }

    /// Spawns at tree-order position `key`. Entities without an explicit `RngState` get the
    /// stream `entity_seed(session_seed, uid, 0)` when `RngState` is registered.
    pub fn spawn_at(
        &mut self,
        s: SectorIx,
        uid: UniverseId,
        key: OrderKey,
        mut bundle: Bundle,
    ) -> Result<EntityId, StructError> {
        self.world_checked(s)?;
        let e = self.ids.alloc(uid, Loc { sector: s, row: STAGED_ROW }).map_err(StructError::Id)?;
        if self.is_registered::<RngState>() && bundle.get::<RngState>().is_none() {
            let stream_uid = if uid.is_none() { UniverseId(e.to_bits() | (1 << 63)) } else { uid };
            bundle.set(RngState(entity_seed(self.session_seed, stream_uid, 0)));
        }
        if let Err(err) = self.worlds[s.index()].stage(e, uid, key, bundle) {
            let _ = self.ids.free(e);
            return Err(err);
        }
        Ok(e)
    }

    /// Removes an entity and returns its component values (row disappears at commit).
    pub fn despawn(&mut self, e: EntityId) -> Result<Bundle, StructError> {
        let loc = self.ids.loc(e).ok_or(StructError::Id(crate::id::IdError::Stale(e)))?;
        let w = &mut self.worlds[loc.sector.index()];
        let bundle = if loc.row == STAGED_ROW {
            w.unstage(e).ok_or(StructError::Id(crate::id::IdError::Stale(e)))?
        } else {
            w.remove_row(loc.row as usize)
        };
        self.ids.free(e).map_err(StructError::Id)?;
        Ok(bundle)
    }

    /// Moves an entity with all its components to the end of `to`'s tree order.
    /// The key is taken after the entity left its old row, so migrating within one sector
    /// moves it to the end.
    pub fn migrate(&mut self, e: EntityId, to: SectorIx) -> Result<(), StructError> {
        self.migrate_with(e, to, None)
    }

    pub fn migrate_at(&mut self, e: EntityId, to: SectorIx, key: OrderKey) -> Result<(), StructError> {
        self.migrate_with(e, to, Some(key))
    }

    fn migrate_with(&mut self, e: EntityId, to: SectorIx, key: Option<OrderKey>) -> Result<(), StructError> {
        self.world_checked(to)?;
        let loc = self.ids.loc(e).ok_or(StructError::Id(crate::id::IdError::Stale(e)))?;
        let uid = self.ids.uid(e).unwrap_or_default();
        let from = &mut self.worlds[loc.sector.index()];
        let bundle = if loc.row == STAGED_ROW {
            from.unstage(e).ok_or(StructError::Id(crate::id::IdError::Stale(e)))?
        } else {
            from.remove_row(loc.row as usize)
        };
        let target = &mut self.worlds[to.index()];
        let key = key.unwrap_or_else(|| target.last_key().map_or(OrderKey(0), |k| OrderKey(k.0 + 1)));
        target.stage(e, uid, key, bundle)?;
        self.ids.set_loc(e, Loc { sector: to, row: STAGED_ROW });
        Ok(())
    }

    /// Applies all staged structure. Returns true if any world changed.
    pub fn commit(&mut self) -> bool {
        let mut changed = false;
        for w in &mut self.worlds {
            changed |= w.commit(&mut self.ids);
        }
        changed
    }

    pub(crate) fn reorder_by_keys(&mut self, s: SectorIx) {
        self.worlds[s.index()].reorder_by_keys(&mut self.ids);
    }

    /// A component value of a committed entity.
    pub fn get<T: Component>(&self, e: EntityId) -> Option<&T> {
        let loc = self.ids.loc(e).filter(|l| l.row != STAGED_ROW)?;
        self.worlds[loc.sector.index()].try_col::<T>()?.get(loc.row as usize)
    }

    pub fn get_mut<T: Component>(&mut self, e: EntityId) -> Option<&mut T> {
        let loc = self.ids.loc(e).filter(|l| l.row != STAGED_ROW)?;
        self.worlds[loc.sector.index()].try_col_mut::<T>()?.get_mut(loc.row as usize)
    }

    /// Posts a message serially (between frames, or from a barrier hook) to `system`'s next
    /// run, after everything already queued for it. Returns false when the target is gone.
    pub fn post<M: Message>(&mut self, system: &'static str, to: Target, msg: M) -> bool {
        let mut out = Outbox::new(SERIAL_SENDER, system);
        out.send(to, msg);
        let Some(Item::Msg(mut env)) = out.items.pop() else { unreachable!("one message sent") };
        env.seq = self.serial_seq;
        self.serial_seq = self.serial_seq.wrapping_add(1);
        self.route(env)
    }

    fn barrier_inbox(&mut self, system: &'static str) -> &mut Vec<Envelope> {
        let pos = match self.barrier_inboxes.iter().position(|(n, _)| *n == system) {
            Some(p) => p,
            None => {
                self.barrier_inboxes.push((system, Vec::new()));
                self.barrier_inboxes.len() - 1
            }
        };
        &mut self.barrier_inboxes[pos].1
    }

    fn route(&mut self, env: Envelope) -> bool {
        if self.log_messages {
            self.message_log.push(format!("p{} {}", self.phase, env.describe()));
        }
        match env.to {
            Target::Sector(t) => match self.worlds.get_mut(t.index()) {
                Some(w) => w.inbox_mut(env.system).push(env),
                None => return false,
            },
            Target::Entity(e) => match self.ids.loc(e) {
                Some(loc) => self.worlds[loc.sector.index()].inbox_mut(env.system).push(env),
                None => return false,
            },
            Target::Barrier => self.barrier_inbox(env.system).push(env),
        }
        true
    }

    /// Messages routed but not yet consumed (all worlds, all systems, barrier queues).
    pub fn pending_messages(&self) -> usize {
        self.worlds.iter().map(|w| w.pending_messages()).sum::<usize>()
            + self.barrier_inboxes.iter().map(|(_, v)| v.len()).sum::<usize>()
    }

    /// Records every routed message (`p<phase> from#seq -> target type payload`).
    pub fn set_message_log(&mut self, on: bool) {
        self.log_messages = on;
    }

    pub fn take_message_log(&mut self) -> Vec<String> {
        std::mem::take(&mut self.message_log)
    }

    // --- the frame -----------------------------------------------------------------------

    /// Runs one frame: commits staged structure, then for each system a parallel phase over
    /// all worlds followed by the ordered barrier.
    pub fn step(&mut self, pool: &Pool, systems: &[&dyn System], frame: &FrameInfo) -> FrameReport {
        self.commit();
        let mut report = FrameReport { frame: frame.frame, phases: Vec::new(), errors: Vec::new() };
        for sys in systems {
            let phase = self.run_phase(pool, *sys, frame, &mut report.errors);
            report.phases.push(phase);
        }
        report
    }

    fn run_phase(&mut self, pool: &Pool, sys: &dyn System, frame: &FrameInfo, errors: &mut Vec<String>) -> PhaseReport {
        let phase = self.phase;
        self.snapshot.refresh(&self.worlds, &self.published);
        let mut pinned = Vec::new();
        let mut order = Vec::new();
        for (i, w) in self.worlds.iter().enumerate() {
            if w.pinned {
                pinned.push(i);
            } else {
                order.push(i);
            }
        }
        // Largest first by last measured cost; only affects which thread runs what.
        order.sort_by_key(|&i| (Reverse(self.worlds[i].cost_ns), i));
        let t0 = Instant::now();
        {
            let ids = &self.ids;
            let snapshot = &self.snapshot;
            let session_seed = self.session_seed;
            let name = sys.name();
            let job = |w: &mut World| {
                let t = Instant::now();
                let inbox = w.take_inbox(name);
                let mut out = std::mem::take(&mut w.outbox);
                out.reset(w.ix, name);
                let sector = w.ix;
                {
                    let mut ctx = SectorCtx {
                        frame,
                        sector,
                        world: &mut *w,
                        ids,
                        snapshot,
                        inbox: &inbox,
                        out: &mut out,
                        session_seed,
                    };
                    sys.run_sector(&mut ctx);
                }
                w.outbox = out;
                w.return_inbox(name, inbox);
                w.cost_ns = u64::try_from(t.elapsed().as_nanos()).unwrap_or(u64::MAX);
            };
            pool.for_each_mut(&mut self.worlds, &pinned, &order, &job);
        }
        let parallel_ns = u64::try_from(t0.elapsed().as_nanos()).unwrap_or(u64::MAX);
        let tb = Instant::now();
        let mut rep = PhaseReport {
            system: sys.name(),
            phase,
            sectors: self.worlds.len(),
            parallel_ns,
            sector_sum_ns: self.worlds.iter().map(|w| w.cost_ns).sum(),
            sector_max_ns: self.worlds.iter().map(|w| w.cost_ns).max().unwrap_or(0),
            ..PhaseReport::default()
        };
        // Barrier 1: structural commands in total order: (order key, sector, emission) with
        // all keys 0 unless a job set one, i.e. sector order then emission order.
        let keyed = self.worlds.iter().any(|w| w.outbox.keyed);
        let mut ordered: Vec<(u32, Item)> = Vec::new();
        for s in 0..self.worlds.len() {
            let mut items = std::mem::take(&mut self.worlds[s].outbox.items);
            ordered.extend(items.drain(..).map(|it| (s as u32, it)));
            self.worlds[s].outbox.items = items;
        }
        if keyed {
            ordered.sort_by_key(|(s, it)| it.sort_key(*s));
        }
        let mut msgs = Vec::new();
        {
            for (s, item) in ordered {
                match item {
                    Item::Msg(env) => msgs.push(env),
                    Item::Cmd { seq, cmd, .. } => {
                        rep.commands += 1;
                        match &cmd {
                            Command::Spawn { .. } => rep.spawned += 1,
                            Command::Despawn { .. } => rep.despawned += 1,
                            Command::Migrate { .. } => rep.migrated += 1,
                        }
                        if let Err(e) = self.apply(cmd) {
                            rep.command_errors += 1;
                            if errors.len() < 16 {
                                errors.push(format!("phase {phase} sector {s} #{seq}: {e}"));
                            }
                        }
                    }
                }
            }
        }
        self.commit();
        // Barrier 2: route messages in the same total order.
        for env in msgs {
            rep.messages += 1;
            if !self.route(env) {
                rep.dropped_messages += 1;
            }
        }
        // Barrier 3: the system's serial hook gets everything queued for it, then commit.
        let barrier = std::mem::take(self.barrier_inbox(sys.name()));
        sys.barrier(&mut BarrierCtx { frame, universe: self, messages: &barrier });
        self.commit();
        self.phase += 1;
        self.serial_seq = 0;
        rep.barrier_ns = u64::try_from(tb.elapsed().as_nanos()).unwrap_or(u64::MAX);
        rep
    }

    fn apply(&mut self, cmd: Command) -> Result<(), StructError> {
        match cmd {
            Command::Spawn { sector, uid, key: Some(k), bundle } => self.spawn_at(sector, uid, k, bundle).map(|_| ()),
            Command::Spawn { sector, uid, key: None, bundle } => self.spawn(sector, uid, bundle).map(|_| ()),
            Command::Despawn { entity } => self.despawn(entity).map(|_| ()),
            Command::Migrate { entity, to, key: Some(k) } => self.migrate_at(entity, to, k),
            Command::Migrate { entity, to, key: None } => self.migrate(entity, to),
        }
    }

    // --- inspection ------------------------------------------------------------------------

    /// FNV-1a over every world in sector order (all rows, all columns). Equal hashes mean
    /// bit-identical state with overwhelming probability.
    pub fn state_hash(&self) -> u64 {
        let mut h = Fnv64::new();
        h.write_u64(self.worlds.len() as u64);
        for w in &self.worlds {
            w.hash_into(&mut h);
            for (t, f) in &self.hashed_resources {
                match w.resource_any(*t) {
                    Some(r) => {
                        h.write_u8(1);
                        f(r, &mut h);
                    }
                    None => h.write_u8(0),
                }
            }
        }
        h.finish()
    }

    /// Verifies the world invariants and the id map (tests, debugging).
    pub fn check(&self) -> Result<(), String> {
        let mut total = 0;
        for w in &self.worlds {
            if w.has_staged() {
                return Err(format!("{:?}: uncommitted structure", w.sector()));
            }
            w.check(&self.ids)?;
            total += w.len();
        }
        if total != self.ids.len() {
            return Err(format!("{} rows but {} live ids", total, self.ids.len()));
        }
        Ok(())
    }

    /// Compares this (expected) universe with `actual` sector by sector.
    pub fn compare(&self, actual: &Universe, columns: &[&str], frame: u64, c: &mut Comparator) {
        self.compare_with(actual, &crate::world::CompareOpts { columns, structure: true }, frame, c);
    }

    /// [`Universe::compare`] with options (see [`World::compare_with`]).
    pub fn compare_with(
        &self,
        actual: &Universe,
        opts: &crate::world::CompareOpts<'_>,
        frame: u64,
        c: &mut Comparator,
    ) {
        if self.worlds.len() != actual.worlds.len() {
            c.structural("sectors", self.worlds.len().to_string(), actual.worlds.len().to_string());
        }
        for (e, a) in self.worlds.iter().zip(&actual.worlds) {
            if e.root_uid() != a.root_uid() {
                c.structural("sector_uid", e.root_uid().to_string(), a.root_uid().to_string());
                continue;
            }
            e.compare_with(a, opts, frame, c);
        }
    }
}
