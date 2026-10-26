//! Synthetic test (no X4): one economy, run with 1, 2, 4 and 16 worker threads, must give the
//! same state hash after every frame and the same barrier log (messages, order, outcomes).

use x4mp_economy::ids::{OrderKey, SectorIx, UniverseId, WareIx};
use x4mp_economy::market::{OfferBook, OfferSnapshot};
use x4mp_economy::production::{ContainerTimes, Factors, Item, Method, Module, State};
use x4mp_economy::sector::{state_hash, step_frame, Demand, EconRng, SectorEconomy, Station, WareVolumes};
use x4mp_economy::time::UNSET;

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

fn universe(sectors: u32, stations: u32) -> Vec<SectorEconomy> {
    let mut out = Vec::new();
    for s in 0..sectors {
        let mut sts = Vec::new();
        for k in 0..stations {
            let uid = UniverseId(0x1_0000 + (s as u64) * 64 + k as u64);
            // Varied cycle times so that event times interleave across stations and sectors.
            let solar = method(ECELLS, 60.0 + (k as f64) * 7.5, 175, vec![]);
            let refinery = method(METAL, 150.0 + (s % 5) as f64, 88, vec![(ECELLS, 90), (ORE, 240)]);
            sts.push(Station {
                uid,
                key: OrderKey(k as u64),
                times: ContainerTimes::default(),
                modules: vec![
                    module(0x7000_0000 + uid.0 * 0x100, solar),
                    module(0x7000_0000 + uid.0 * 0x100 + 0x40, refinery),
                ],
                cargo: vec![(ECELLS, 500), (ORE, 1000 + (s as i32) * 37 % 900)],
                capacity: 200_000 + (k as i64) * 10_000,
                rng: EconRng::for_entity(0xC0FFEE, uid, 0x4543_4f4e),
            });
        }
        out.push(SectorEconomy { sector: SectorIx(s), stations: sts, book: OfferBook::new() });
    }
    out
}

fn run(threads: usize, frames: u64) -> (Vec<u64>, Vec<String>) {
    let mut sectors = universe(48, 4);
    let volumes = WareVolumes(vec![1, 10, 14]);
    let mut snapshot = OfferSnapshot::default();
    let demand = Some(Demand { ware: METAL, amount: 40, period: 7 });
    let mut hashes = Vec::new();
    let mut logs = Vec::new();
    for frame in 1..=frames {
        let now = frame as f64 * (1.0 / 60.0) * 600.0; // 10 s of game time per frame
        let (log, snap) = step_frame(&mut sectors, threads, frame, now, &volumes, &snapshot, demand);
        snapshot = snap;
        hashes.push(state_hash(&sectors));
        for (m, o) in log {
            logs.push(format!("{frame} {:?} {:?} {:?} {:?}", m.stamp, m.actor, m.op, o));
        }
    }
    (hashes, logs)
}

#[test]
fn one_and_many_threads_give_identical_results() {
    let (h1, l1) = run(1, 120);
    assert!(!l1.is_empty(), "the scenario must produce cross-sector messages");
    assert!(l1.iter().any(|l| l.contains("Conflict")), "the scenario must exercise stale decisions");
    assert!(l1.iter().any(|l| l.contains("Applied")));
    for threads in [2, 4, 16] {
        let (h, l) = run(threads, 120);
        assert_eq!(h1, h, "state hash differs with {threads} threads");
        assert_eq!(l1, l, "barrier log differs with {threads} threads");
    }
}
