//! Structure: spawn/despawn/migrate/commit against a reference model (property test), tree
//! order, staged cancellation, migration semantics, message routing rules.

#![allow(clippy::needless_range_loop)]

mod common;

use std::collections::BTreeMap;

use common::TestRng;
use x4mp_sim::comp::{Hull, RngState};
use x4mp_sim::{
    BarrierCtx, Bundle, Component, EntityId, FrameInfo, OrderKey, Pool, SectorCtx, SectorIx, System, Target, Universe,
    UniverseId, WorldKind,
};

#[derive(Clone, Copy, Default, Debug, PartialEq)]
struct Val(u64);
x4mp_sim::impl_fields!(newtype Val);
impl Component for Val {
    const NAME: &'static str = "val";
}

/// Reference: per sector, rows sorted by (key, epoch, seq); values travel with rows.
#[derive(Clone, Debug, PartialEq)]
struct Row {
    key: u64,
    epoch: u64,
    seq: u64,
    uid: u64,
    val: u64,
}

#[test]
fn random_structure_ops_match_reference_model() {
    for seed in 0..12u64 {
        let mut rng = TestRng(seed * 7919 + 1);
        let mut u = Universe::bare(seed);
        u.register::<Val>();
        let sectors = 1 + rng.below(6) as usize;
        for s in 0..sectors {
            u.add_sector(UniverseId(1000 + s as u64), WorldKind::Sector);
        }
        let mut model: Vec<Vec<Row>> = vec![Vec::new(); sectors];
        let mut staged: Vec<Vec<Row>> = vec![Vec::new(); sectors];
        let mut live: BTreeMap<u64, EntityId> = BTreeMap::new(); // uid -> entity
        let mut epoch = 0u64;
        let mut seq = 0u64;
        let mut next_uid = 1u64;
        for _op in 0..400 {
            match rng.below(10) {
                0..=3 => {
                    let s = rng.below(sectors as u64) as usize;
                    let uid = next_uid;
                    next_uid += 1;
                    let val = rng.next();
                    let explicit = rng.below(3) == 0;
                    let key = if explicit {
                        rng.below(50)
                    } else {
                        let committed = model[s].iter().map(|r| r.key).max();
                        let pending = staged[s].iter().map(|r| r.key).max();
                        committed.max(pending).map_or(0, |k| k + 1)
                    };
                    let b = Bundle::new().with(Val(val));
                    let e = if explicit {
                        u.spawn_at(SectorIx(s as u32), UniverseId(uid), OrderKey(key), b)
                    } else {
                        u.spawn(SectorIx(s as u32), UniverseId(uid), b)
                    }
                    .unwrap();
                    live.insert(uid, e);
                    seq += 1;
                    staged[s].push(Row { key, epoch, seq, uid, val });
                }
                4..=5 => {
                    if let Some((&uid, &e)) = live.iter().nth(rng.below(live.len().max(1) as u64) as usize) {
                        u.despawn(e).unwrap();
                        live.remove(&uid);
                        for list in model.iter_mut().chain(staged.iter_mut()) {
                            list.retain(|r| r.uid != uid);
                        }
                    }
                }
                6..=7 => {
                    if let Some((&uid, &e)) = live.iter().nth(rng.below(live.len().max(1) as u64) as usize) {
                        let to = rng.below(sectors as u64) as usize;
                        let mut row = None;
                        for list in model.iter_mut().chain(staged.iter_mut()) {
                            if let Some(p) = list.iter().position(|r| r.uid == uid) {
                                row = Some(list.remove(p));
                            }
                        }
                        let mut row = row.unwrap();
                        let key = {
                            let committed = model[to].iter().map(|r| r.key).max();
                            let pending = staged[to].iter().map(|r| r.key).max();
                            committed.max(pending).map_or(0, |k| k + 1)
                        };
                        u.migrate(e, SectorIx(to as u32)).unwrap();
                        seq += 1;
                        row.key = key;
                        row.epoch = epoch;
                        row.seq = seq;
                        staged[to].push(row);
                    }
                }
                _ => {
                    u.commit();
                    for s in 0..sectors {
                        let mut rows = std::mem::take(&mut staged[s]);
                        model[s].append(&mut rows);
                        model[s].sort_by_key(|r| (r.key, r.epoch, r.seq));
                    }
                    epoch += 1;
                    u.check().unwrap();
                    for s in 0..sectors {
                        let w = u.world(SectorIx(s as u32));
                        let got: Vec<(u64, u64, u64)> =
                            (0..w.len()).map(|r| (w.keys()[r].0, w.uids()[r].0, w.col::<Val>()[r].0)).collect();
                        let want: Vec<(u64, u64, u64)> = model[s].iter().map(|r| (r.key, r.uid, r.val)).collect();
                        assert_eq!(got, want, "seed {seed} sector {s}");
                    }
                }
            }
        }
    }
}

#[test]
fn spawn_then_despawn_before_commit_cancels() {
    let mut u = Universe::bare(1);
    u.register::<Val>();
    let s = u.add_sector(UniverseId(5), WorldKind::Sector);
    let a = u.spawn(s, UniverseId(10), Bundle::new().with(Val(1))).unwrap();
    let b = u.spawn(s, UniverseId(11), Bundle::new().with(Val(2))).unwrap();
    assert_eq!(u.despawn(a).unwrap().get::<Val>(), Some(&Val(1)));
    assert!(u.despawn(a).is_err(), "second despawn of the same handle fails");
    u.commit();
    u.check().unwrap();
    assert_eq!(u.world(s).uids(), &[UniverseId(11)]);
    assert_eq!(u.get::<Val>(b), Some(&Val(2)));
    assert!(u.ids().by_uid(UniverseId(10)).is_none());
    // The uid can be reused after release.
    u.spawn(s, UniverseId(10), Bundle::new()).unwrap();
    u.commit();
    assert_eq!(u.world(s).len(), 2);
}

#[test]
fn unregistered_component_in_bundle_is_refused_without_leaking_an_id() {
    let mut u = Universe::bare(1);
    let s = u.add_sector(UniverseId(5), WorldKind::Sector);
    let err = u.spawn(s, UniverseId(10), Bundle::new().with(Val(1))).unwrap_err();
    assert_eq!(err.to_string(), "component val is not registered");
    assert!(u.ids().is_empty());
    assert!(u.spawn(SectorIx(9), UniverseId(11), Bundle::new()).is_err());
}

#[test]
fn rng_streams_are_seeded_from_uid_and_session() {
    let mut u = Universe::new(42);
    let s = u.add_sector(UniverseId(5), WorldKind::Sector);
    let a = u.spawn(s, UniverseId(10), Bundle::new()).unwrap();
    let b = u.spawn(s, UniverseId(11), Bundle::new().with(RngState(7))).unwrap();
    u.commit();
    assert_eq!(u.get::<RngState>(a).unwrap().0, x4mp_sim::rng::entity_seed(42, UniverseId(10), 0));
    assert_eq!(u.get::<RngState>(b).unwrap().0, 7, "explicit state kept");
}

#[test]
fn migration_keeps_handle_and_components_and_appends() {
    let mut u = Universe::new(3);
    u.register::<Val>();
    let s0 = u.add_sector(UniverseId(100), WorldKind::Sector);
    let s1 = u.add_sector(UniverseId(101), WorldKind::Sector);
    let e = u.spawn(s0, UniverseId(1), Bundle::new().with(Val(9)).with(Hull { current: 5.0, max: 6.0 })).unwrap();
    for i in 0..3 {
        u.spawn(s1, UniverseId(10 + i), Bundle::new()).unwrap();
    }
    u.commit();
    let rng_before = *u.get::<RngState>(e).unwrap();
    u.migrate(e, s1).unwrap();
    u.commit();
    u.check().unwrap();
    let loc = u.ids().loc(e).unwrap();
    assert_eq!(loc.sector, s1);
    assert_eq!(loc.row, 3, "appended after the existing rows");
    assert_eq!(u.get::<Val>(e), Some(&Val(9)));
    assert_eq!(u.get::<Hull>(e).map(|h| (h.current, h.max)), Some((5.0, 6.0)));
    assert_eq!(*u.get::<RngState>(e).unwrap(), rng_before, "stream travels with the entity");
    assert!(u.world(s0).is_empty());
    // Explicit key: into the middle.
    let f = u.ids().by_uid(UniverseId(10)).unwrap();
    u.migrate_at(f, s1, OrderKey(1)).unwrap();
    u.commit();
    let order: Vec<u64> = u.world(s1).uids().iter().map(|x| x.0).collect();
    assert_eq!(order, vec![11, 10, 12, 1], "key 1 sorts after the existing key 1 (row 11)");
}

// --- routing -------------------------------------------------------------------------------

#[derive(Debug)]
struct Ping(u64);

struct Mover;
impl System for Mover {
    fn name(&self) -> &'static str {
        "mover"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        // Sector 0 sends a ping to every entity it owns, then migrates them all to sector 1.
        if ctx.sector == SectorIx(0) {
            for (r, e) in ctx.world.entities().iter().enumerate() {
                ctx.out.send(Target::Entity(*e), Ping(r as u64));
                ctx.out.migrate(*e, SectorIx(1));
            }
        }
        // Whoever receives pings records them in a resource.
        let got: Vec<(u32, u32, u64)> =
            ctx.inbox.iter().filter_map(|env| env.get::<Ping>().map(|p| (env.from.0, env.seq, p.0))).collect();
        ctx.world.resource_mut::<Vec<(u32, u32, u64)>>().extend(got);
    }
}

#[test]
fn entity_messages_follow_a_migration_of_the_same_barrier() {
    let pool = Pool::new(4);
    let mut u = Universe::new(1);
    let s0 = u.add_sector(UniverseId(100), WorldKind::Sector);
    let s1 = u.add_sector(UniverseId(101), WorldKind::Sector);
    for i in 0..5 {
        u.spawn(s0, UniverseId(1 + i), Bundle::new()).unwrap();
    }
    let fi = FrameInfo { frame: 0, game_time: 0.0, dt: 0.0 };
    let rep = u.step(&pool, &[&Mover], &fi);
    assert_eq!((rep.phases[0].messages, rep.phases[0].migrated, rep.phases[0].dropped_messages), (5, 5, 0));
    assert_eq!(u.world(s1).len(), 5);
    assert_eq!(u.world(s1).pending_for("mover"), 5, "queued for the next run of the same system");
    u.step(&pool, &[&Mover], &FrameInfo { frame: 1, ..fi });
    let got = u.world(s1).resource::<Vec<(u32, u32, u64)>>().unwrap();
    // from sector 0, seq 0,2,4,6,8 (pings interleaved with migrate commands), in that order.
    assert_eq!(got, &vec![(0, 0, 0), (0, 2, 1), (0, 4, 2), (0, 6, 3), (0, 8, 4)]);
    assert!(u.world(s0).resource::<Vec<(u32, u32, u64)>>().unwrap().is_empty());
    assert_eq!(u.pending_messages(), 0);
}

struct Killer;
impl System for Killer {
    fn name(&self) -> &'static str {
        "killer"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        for e in ctx.world.entities().to_vec() {
            ctx.out.despawn(e);
            ctx.out.send(Target::Entity(e), Ping(1));
            ctx.out.despawn(e); // second despawn: a deterministic command error
        }
    }
}

#[test]
fn messages_to_despawned_entities_are_dropped_and_errors_reported() {
    let pool = Pool::new(2);
    let mut u = Universe::new(1);
    let s = u.add_sector(UniverseId(100), WorldKind::Sector);
    for i in 0..3 {
        u.spawn(s, UniverseId(1 + i), Bundle::new()).unwrap();
    }
    let rep = u.step(&pool, &[&Killer], &FrameInfo::default());
    let p = &rep.phases[0];
    assert_eq!((p.despawned, p.command_errors, p.dropped_messages), (6, 3, 3));
    assert_eq!(rep.errors.len(), 3);
    assert!(rep.errors[0].starts_with("phase 0 sector 0 #2: stale entity e0v0"), "{}", rep.errors[0]);
    assert_eq!(u.entity_count(), 0);
}

#[derive(Debug)]
struct Note(&'static str);

struct First;
impl System for First {
    fn name(&self) -> &'static str {
        "first"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let seen: Vec<&'static str> = ctx.inbox.iter().filter_map(|e| e.get::<Note>().map(|n| n.0)).collect();
        ctx.world.resource_mut::<Vec<String>>().push(format!("first f{} saw {:?}", ctx.frame.frame, seen));
        ctx.out.send_to("second", Target::Sector(ctx.sector), Note("from first"));
        ctx.out.send(Target::Barrier, Note("first barrier"));
    }
    fn barrier(&self, ctx: &mut BarrierCtx<'_>) {
        let n = ctx.messages.len();
        ctx.universe.post("first", Target::Sector(SectorIx(0)), Note("posted by first barrier"));
        ctx.universe.world_mut(SectorIx(0)).resource_mut::<Vec<String>>().push(format!("first barrier got {n}"));
    }
}

struct Second;
impl System for Second {
    fn name(&self) -> &'static str {
        "second"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let seen: Vec<&'static str> = ctx.inbox.iter().filter_map(|e| e.get::<Note>().map(|n| n.0)).collect();
        ctx.world.resource_mut::<Vec<String>>().push(format!("second f{} saw {:?}", ctx.frame.frame, seen));
        ctx.out.send_to("first", Target::Sector(ctx.sector), Note("from second"));
    }
}

#[test]
fn cross_system_delivery_timing() {
    let pool = Pool::new(3);
    let mut u = Universe::new(1);
    u.add_sector(UniverseId(100), WorldKind::Sector);
    assert!(u.post("second", Target::Sector(SectorIx(0)), Note("posted before frame 0")));
    for f in 0..2 {
        u.step(&pool, &[&First, &Second], &FrameInfo { frame: f, ..FrameInfo::default() });
    }
    let log = u.world(SectorIx(0)).resource::<Vec<String>>().unwrap();
    assert_eq!(
        log,
        &vec![
            "first f0 saw []".to_string(),
            "first barrier got 1".to_string(),
            // second runs after first in the same frame: sees the serial post and first's note.
            "second f0 saw [\"posted before frame 0\", \"from first\"]".to_string(),
            // first sees second's note (previous frame) after its own barrier's post.
            "first f1 saw [\"posted by first barrier\", \"from second\"]".to_string(),
            "first barrier got 1".to_string(),
            "second f1 saw [\"from first\"]".to_string(),
        ]
    );
    assert!(!u.post("first", Target::Sector(SectorIx(7)), Note("nowhere")), "no such sector");
}

#[test]
fn hull_column_survives_register_after_spawn() {
    let mut u = Universe::bare(1);
    let s = u.add_sector(UniverseId(1), WorldKind::Sector);
    let e = u.spawn(s, UniverseId(2), Bundle::new()).unwrap();
    u.commit();
    u.register::<Hull>();
    assert!(u.get::<Hull>(e).unwrap().max.is_nan(), "default hull: max unknown");
    u.check().unwrap();
}
