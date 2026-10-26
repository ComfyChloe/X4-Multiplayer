//! Relevance scheduler: tier classification, rate bands and send counts over time.

use x4mp_proto::sched::*;
use x4mp_proto::*;

const TICK_US: u64 = 16_667; // 60 Hz relay tick

fn p(n: u16) -> PlayerId {
    PlayerId::human(n).unwrap()
}

fn id(n: u64) -> ObjectId {
    ObjectId::new(n).unwrap()
}

fn sec(n: u32) -> SectorId {
    SectorId::new(n).unwrap()
}

fn npc(n: u64, sector: u32, x: f64) -> ObjectInfo {
    ObjectInfo {
        id: id(n),
        sector: sec(sector),
        publisher: PlayerId::AUTHORITY,
        position_m: [x, 0.0, 0.0],
        speed_mps: 0.0,
        combat_target: None,
        ambient: false,
        update_seq: 0,
        map_revision: 0,
    }
}

fn sectors() -> [SectorInterest; 2] {
    [
        SectorInterest { sector: sec(2), interest: Interest::Adjacent },
        SectorInterest { sector: sec(3), interest: Interest::Map },
    ]
}

fn subscriber(sectors: &[SectorInterest]) -> Subscriber<'_> {
    Subscriber {
        player: p(1),
        current_sector: sec(1),
        own_ship: Some(id(100)),
        own_position_m: Some([0.0, 0.0, 0.0]),
        target: Some(id(7)),
        sectors,
        all_map: false,
    }
}

#[test]
fn classification_follows_the_tier_table() {
    let config = SchedulerConfig::default();
    let sectors = sectors();
    let sub = subscriber(&sectors);
    let classify = |o: &ObjectInfo| classify(&config, &sub, o);
    // Own ship (by ID or because this subscriber published it): never sent back.
    assert_eq!(classify(&npc(100, 1, 0.0)), None);
    assert_eq!(classify(&ObjectInfo { publisher: p(1), ..npc(55, 1, 0.0) }), None);
    // Another player's client-published ship in my sector: tier 1, even far away.
    assert_eq!(classify(&ObjectInfo { publisher: p(2), ..npc(200, 1, 90_000.0) }), Some(Tier::Player));
    // ... but in an adjacent sector it is just preload: tier 3.
    assert_eq!(classify(&ObjectInfo { publisher: p(2), ..npc(200, 2, 0.0) }), Some(Tier::Sector));
    // My target, my attacker, an NPC within weapons range: tier 2.
    assert_eq!(classify(&npc(7, 1, 50_000.0)), Some(Tier::Combat));
    assert_eq!(classify(&ObjectInfo { combat_target: Some(id(100)), ..npc(8, 1, 50_000.0) }), Some(Tier::Combat));
    assert_eq!(classify(&npc(9, 1, 5_999.0)), Some(Tier::Combat));
    // Other NPCs in my sector and anything in an adjacent sector: tier 3.
    assert_eq!(classify(&npc(10, 1, 6_001.0)), Some(Tier::Sector));
    assert_eq!(classify(&npc(11, 2, 0.0)), Some(Tier::Sector));
    // Weapons range only applies in my own sector (coordinates are per sector).
    assert_eq!(classify(&npc(12, 2, 10.0)), Some(Tier::Sector));
    // Map-only sector: tier 4; unsubscribed: not sent; ambient: tier 5 everywhere.
    assert_eq!(classify(&npc(13, 3, 0.0)), Some(Tier::Map));
    assert_eq!(classify(&npc(14, 4, 0.0)), None);
    assert_eq!(classify(&ObjectInfo { ambient: true, ..npc(15, 1, 0.0) }), Some(Tier::Ambient));
    let all_map = Subscriber { all_map: true, ..sub };
    assert_eq!(x4mp_proto::sched::classify(&config, &all_map, &npc(14, 4, 0.0)), Some(Tier::Map));
    // No own ship: attackers cannot be identified, and nothing is "within range".
    let walking = Subscriber { own_ship: None, own_position_m: None, ..sub };
    assert_eq!(x4mp_proto::sched::classify(&config, &walking, &npc(9, 1, 1.0)), Some(Tier::Sector));
}

#[test]
fn rates_scale_with_speed_inside_each_band() {
    let c = SchedulerConfig::default();
    assert_eq!(rate_hz(&c, Tier::Player, 0.0), Some(20.0));
    assert_eq!(rate_hz(&c, Tier::Player, 150.0), Some(25.0));
    assert_eq!(rate_hz(&c, Tier::Player, 300.0), Some(30.0));
    assert_eq!(rate_hz(&c, Tier::Combat, 30_000.0), Some(30.0)); // highway speed
    assert_eq!(rate_hz(&c, Tier::Sector, 0.0), Some(1.0));
    assert_eq!(rate_hz(&c, Tier::Sector, 1e9), Some(2.0));
    assert_eq!(rate_hz(&c, Tier::Sector, f64::NAN), Some(2.0));
    assert_eq!(rate_hz(&c, Tier::Sector, -5.0), Some(1.0));
    assert_eq!(rate_hz(&c, Tier::Map, 100.0), None);
    assert_eq!(rate_hz(&c, Tier::Ambient, 100.0), None);
    assert_eq!(interval_us(30.0), 33_333);
    let flat = SchedulerConfig { speed_scaling: false, ..c };
    assert_eq!(rate_hz(&flat, Tier::Player, 0.0), Some(30.0));
    assert_eq!(rate_hz(&flat, Tier::Sector, 0.0), Some(2.0));
}

/// Run `ticks` plans at 60 Hz; `update` mutates objects before each tick.
fn run(
    config: SchedulerConfig,
    sub: &Subscriber<'_>,
    objects: &mut [ObjectInfo],
    ticks: u64,
    mut update: impl FnMut(u64, &mut [ObjectInfo]),
) -> std::collections::BTreeMap<u64, usize> {
    let mut scheduler = Scheduler::new(config).unwrap();
    let mut counts = std::collections::BTreeMap::new();
    for k in 0..ticks {
        update(k, objects);
        for due in scheduler.plan(k * TICK_US, sub, objects.iter()) {
            *counts.entry(due.object.get()).or_insert(0) += 1;
        }
    }
    counts
}

#[test]
fn send_counts_over_ten_seconds() {
    let sectors = sectors();
    let sub = subscriber(&sectors);
    let mut objects = vec![
        ObjectInfo { publisher: p(2), speed_mps: 400.0, ..npc(1, 1, 0.0) }, // tier 1 fast: 30 Hz
        ObjectInfo { publisher: p(3), speed_mps: 0.0, ..npc(2, 1, 0.0) },   // tier 1 at rest: 20 Hz
        ObjectInfo { combat_target: Some(id(100)), speed_mps: 600.0, ..npc(3, 1, 20_000.0) }, // attacker
        npc(4, 1, 20_000.0),                                                // tier 3 at rest: 1 Hz
        ObjectInfo { speed_mps: 300.0, ..npc(5, 1, 20_000.0) },             // tier 3 moving: 2 Hz
        npc(6, 3, 0.0),                                                     // map: on change
        ObjectInfo { ambient: true, ..npc(16, 1, 0.0) },                    // never
        ObjectInfo { speed_mps: 400.0, ..npc(17, 4, 0.0) },                 // unsubscribed: never
        ObjectInfo { publisher: p(4), speed_mps: 400.0, ..npc(18, 1, 0.0) }, // tier 1, never updated
    ];
    let counts = run(SchedulerConfig::default(), &sub, &mut objects, 600, |k, objects| {
        for o in objects.iter_mut() {
            if o.id.get() != 18 {
                o.update_seq = k + 1; // new state every tick
            }
        }
        if k == 200 || k == 400 {
            objects[5].map_revision += 1; // e.g. ownership changed twice
        }
    });
    assert_eq!(counts.get(&1), Some(&300));
    assert_eq!(counts.get(&2), Some(&200));
    assert_eq!(counts.get(&3), Some(&300));
    assert_eq!(counts.get(&4), Some(&10));
    assert_eq!(counts.get(&5), Some(&20));
    assert_eq!(counts.get(&6), Some(&3));
    assert_eq!(counts.get(&16), None);
    assert_eq!(counts.get(&17), None);
    // Unchanged state is only refreshed at refresh_hz (1 Hz) to repair loss.
    assert_eq!(counts.get(&18), Some(&10));
}

#[test]
fn promotion_is_immediate_and_demotion_waits_for_the_slower_interval() {
    let sectors = sectors();
    let mut scheduler = Scheduler::new(SchedulerConfig::default()).unwrap();
    let far = npc(20, 1, 20_000.0);
    let sub = Subscriber { target: None, ..subscriber(&sectors) };
    assert_eq!(scheduler.plan(0, &sub, [&far]).len(), 1);
    assert!(scheduler.plan(TICK_US, &sub, [&ObjectInfo { update_seq: 1, ..far }]).is_empty());
    // The player targets it: tier 2 now, sent at once.
    let targeting = Subscriber { target: Some(id(20)), ..sub };
    let due = scheduler.plan(2 * TICK_US, &targeting, [&ObjectInfo { update_seq: 2, ..far }]);
    assert_eq!(due, vec![Due { object: id(20), tier: Tier::Combat }]);
    // Target dropped: back to tier 3, next send only after the 1 Hz interval.
    let t0 = 2 * TICK_US;
    assert!(scheduler.plan(t0 + 100_000, &sub, [&ObjectInfo { update_seq: 3, ..far }]).is_empty());
    assert_eq!(scheduler.plan(t0 + 1_000_000, &sub, [&ObjectInfo { update_seq: 4, ..far }]).len(), 1);
}

#[test]
fn budget_keeps_priority_order_and_defers_the_rest() {
    let sectors = sectors();
    let sub = subscriber(&sectors);
    let config = SchedulerConfig { max_records_per_tick: 2, ..SchedulerConfig::default() };
    let mut scheduler = Scheduler::new(config).unwrap();
    let objects = [
        npc(31, 1, 20_000.0),                              // tier 3
        npc(32, 3, 0.0),                                   // tier 4
        ObjectInfo { publisher: p(2), ..npc(33, 1, 0.0) }, // tier 1
        npc(7, 1, 20_000.0),                               // tier 2 (target)
    ];
    let first: Vec<_> = scheduler.plan(0, &sub, objects.iter()).iter().map(|d| (d.object.get(), d.tier)).collect();
    assert_eq!(first, vec![(33, Tier::Player), (7, Tier::Combat)]);
    let second: Vec<_> = scheduler.plan(1, &sub, objects.iter()).iter().map(|d| (d.object.get(), d.tier)).collect();
    assert_eq!(second, vec![(31, Tier::Sector), (32, Tier::Map)]);
}

#[test]
fn deterministic_regardless_of_input_order_and_forgets_absent_objects() {
    let sectors = sectors();
    let sub = subscriber(&sectors);
    let objects: Vec<ObjectInfo> = (1..=40)
        .map(|n| ObjectInfo { speed_mps: (n * 13 % 400) as f64, ..npc(n, 1 + (n % 3) as u32, (n * 997) as f64) })
        .collect();
    let mut reversed = objects.clone();
    reversed.reverse();
    let mut a = Scheduler::new(SchedulerConfig::default()).unwrap();
    let mut b = Scheduler::new(SchedulerConfig::default()).unwrap();
    for k in 0..300 {
        let now = k * TICK_US;
        let (mut x, mut y) = (objects.clone(), reversed.clone());
        for o in x.iter_mut().chain(y.iter_mut()) {
            o.update_seq = k;
        }
        assert_eq!(a.plan(now, &sub, x.iter()), b.plan(now, &sub, y.iter()));
    }
    // A map object that left the subscriber's view and came back is re-sent at once.
    let map = npc(50, 3, 0.0);
    let mut s = Scheduler::new(SchedulerConfig::default()).unwrap();
    assert_eq!(s.plan(0, &sub, [&map]).len(), 1);
    assert!(s.plan(1, &sub, [&map]).is_empty());
    assert!(s.plan(2, &sub, []).is_empty());
    assert_eq!(s.plan(3, &sub, [&map]).len(), 1);
    s.forget_player(p(1));
    assert_eq!(s.plan(4, &sub, [&map]).len(), 1);
}

#[test]
fn configuration_is_validated() {
    let ok = SchedulerConfig::default();
    assert!(ok.validate().is_ok());
    let bad = [
        SchedulerConfig { tier1: TierRate { min_hz: 0.0, max_hz: 30.0 }, ..ok.clone() },
        SchedulerConfig { tier3: TierRate { min_hz: 3.0, max_hz: 2.0 }, ..ok.clone() },
        SchedulerConfig { tier2: TierRate { min_hz: 1.0, max_hz: f64::INFINITY }, ..ok.clone() },
        SchedulerConfig { speed_for_max_rate: 0.0, ..ok.clone() },
        SchedulerConfig { weapons_range_m: f64::NAN, ..ok.clone() },
        SchedulerConfig { refresh_hz: 0.0, ..ok.clone() },
        SchedulerConfig { map_refresh_s: Some(-1.0), ..ok.clone() },
        SchedulerConfig { max_records_per_tick: 0, ..ok.clone() },
    ];
    for config in bad {
        assert!(Scheduler::new(config).is_err());
    }
    // Configurable rates are honoured.
    let fast = SchedulerConfig { tier3: TierRate { min_hz: 10.0, max_hz: 10.0 }, ..ok };
    let sectors = sectors();
    let sub = subscriber(&sectors);
    let mut objects = [npc(60, 1, 20_000.0)];
    let counts = run(fast, &sub, &mut objects, 600, |k, o| o[0].update_seq = k);
    assert_eq!(counts.get(&60), Some(&100)); // 10 Hz -> every 6 ticks
}
