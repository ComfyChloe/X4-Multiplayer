//! Synthetic test (no X4): the economy as an x4mp-sim system over 48 sectors x 4 stations,
//! stepped with 1, 2, 4 and 16 worker threads, must give the same universe state hash after
//! every frame and the same barrier log (messages in x4mp-sim's total order, outcomes,
//! refreshed owners). Cross-sector reservations go through the owners' reservation lists and
//! expire after their duration; stale decisions become conflicts.

use x4mp_economy::ids::{SectorIx, UniverseId, WareIx};
use x4mp_economy::market::OfferBook;
use x4mp_economy::prices::WarePrices;
use x4mp_economy::production::{ContainerTimes, Factors, Item, Method, Module, State};
use x4mp_economy::reservations::ReservationList;
use x4mp_economy::sector::{count_outcomes, register, Demand, EconOffers, EconStation, EconomySystem, Wares};
use x4mp_economy::time::UNSET;
use x4mp_sim::{Bundle, FrameInfo, Pool, Universe, WorldKind};

const ECELLS: WareIx = WareIx(0);
const ORE: WareIx = WareIx(1);
const METAL: WareIx = WareIx(2);

fn method(product: WareIx, time: f64, amount: i32, primary: Vec<(WareIx, i32)>) -> Method {
    Method { time, product, amount, primary, secondary: vec![], tertiary: vec![], special: false }
}

fn module(addr: u64, m: Method) -> Module {
    Module {
        addr,
        functional: true,
        state: State::Empty,
        start: UNSET,
        next: UNSET,
        paused_since: UNSET,
        paused_total: 0.0,
        item: -1,
        cycle: -1,
        factors: Factors::default(),
        items: vec![Item {
            ware: m.product,
            method: Some(m),
            time_factor: 1.0,
            product_factor: 1.0,
            primary_factor: 1.0,
            secondary_factor: 1.0,
            tertiary_factor: 1.0,
            cycles: 1,
            repeat: true,
        }],
        pending: Vec::new(),
    }
}

fn universe(sectors: u32, stations: u32) -> Universe {
    let mut u = Universe::new(0xC0FFEE);
    register(&mut u);
    for s in 0..sectors {
        let ix = u.add_sector(UniverseId(0x7000_0000 + s as u64), WorldKind::Sector);
        assert_eq!(ix, SectorIx(s));
        for k in 0..stations {
            let uid = UniverseId(0x1_0000 + (s as u64) * 64 + k as u64);
            // Varied cycle times so that event times interleave across stations and sectors.
            let solar = method(ECELLS, 60.0 + (k as f64) * 7.5, 175, vec![]);
            let refinery = method(METAL, 150.0 + (s % 5) as f64, 88, vec![(ECELLS, 90), (ORE, 240)]);
            let st = EconStation {
                times: ContainerTimes::default(),
                modules: vec![
                    module(0x7000_0000 + uid.0 * 0x100, solar),
                    module(0x7000_0000 + uid.0 * 0x100 + 0x40, refinery),
                ],
                cargo: vec![(ECELLS, 500), (ORE, 1000 + (s as i32) * 37 % 900)],
                capacity: 200_000 + (k as i64) * 10_000,
                reservations: ReservationList::new(),
                offers_dirty: true,
                rng: None,
            };
            u.spawn(ix, uid, Bundle::new().with(st).with(EconOffers(OfferBook::new()))).expect("spawn");
        }
    }
    u.commit();
    u
}

fn run(threads: usize, frames: u64) -> (Vec<u64>, Vec<String>) {
    let pool = Pool::new(threads);
    let mut u = universe(48, 4);
    let wares = Wares {
        volume: vec![1, 10, 14],
        prices: vec![
            WarePrices { avg: 1600, min: 1000, max: 2200 },
            WarePrices { avg: 5000, min: 4200, max: 5800 },
            WarePrices { avg: 13_800, min: 11_700, max: 15_900 },
        ],
    };
    // Reservations last 300 s (30 frames), so expiry is exercised.
    let econ = EconomySystem::new(wares, Some(Demand { ware: METAL, amount: 40, period: 7, duration: 300.0 }));
    let mut hashes = Vec::new();
    let mut entries = Vec::new();
    for frame in 1..=frames {
        // 10 s of game time per frame
        let fi = FrameInfo { frame, game_time: frame as f64 * 10.0, dt: 10.0 };
        let rep = u.step(&pool, &[&econ], &fi);
        assert!(rep.errors.is_empty(), "{:?}", rep.errors);
        hashes.push(u.state_hash());
        let mut n = 0;
        for s in 0..u.sector_count() {
            n += u
                .world(SectorIx(s as u32))
                .col::<EconStation>()
                .iter()
                .map(|st| st.reservations.entries().len())
                .sum::<usize>();
        }
        entries.push(format!("{frame} entries {n}"));
    }
    let mut log = std::mem::take(&mut *econ.log.lock().unwrap());
    log.extend(entries);
    (hashes, log)
}

#[test]
fn one_and_many_threads_give_identical_results() {
    let (h1, l1) = run(1, 120);
    let (applied, conflicts) = count_outcomes(&l1);
    assert!(applied > 0, "the scenario must apply cross-sector reservations");
    assert!(conflicts > 0, "the scenario must exercise stale decisions");
    // Expired reservations leave the lists again (the scans in add and refresh remove them).
    let counts: Vec<usize> =
        l1.iter().filter_map(|l| l.split_once(" entries ").map(|(_, n)| n.parse().unwrap())).collect();
    assert!(counts.windows(2).any(|w| w[1] < w[0]), "expiry must be exercised: {counts:?}");
    let refreshed: usize =
        l1.iter().filter_map(|l| l.split_once(" refresh [").map(|(_, r)| r.matches("0x").count())).sum();
    println!(
        "120 frames, 48 sectors x 4 stations: {applied} reservations applied, {conflicts} conflicts, {refreshed} barrier refreshes"
    );
    for threads in [2, 4, 16] {
        let (h, l) = run(threads, 120);
        assert_eq!(h1, h, "state hash differs with {threads} threads");
        assert_eq!(l1, l, "barrier log differs with {threads} threads");
    }
}
