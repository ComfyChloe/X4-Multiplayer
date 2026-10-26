//! Synthetic test (no X4): the model writes an X4ECON capture of its own catch-ups, the replay
//! must classify every transition as exact; a corrupted record must be reported with its
//! first difference. This checks the format and the replay pipeline, not the engine.

use x4mp_economy::capture::{replay, write_method, write_station, Capture, StationRecord};
use x4mp_economy::ids::WareIx;
use x4mp_economy::production::{
    update_production, ContainerTimes, Factors, Item, Method, Module, ProductionRng, State, StationEnv, WareAmount,
};
use x4mp_economy::sector::EconRng;
use x4mp_economy::time::UNSET;

const ECELLS: WareIx = WareIx(0);
const ORE: WareIx = WareIx(1);
const METAL: WareIx = WareIx(2);

struct Env {
    cargo: Vec<WareAmount>,
}

impl Env {
    fn amount(&self, w: WareIx) -> i32 {
        self.cargo.iter().find(|e| e.0 == w).map_or(0, |e| e.1)
    }
    fn add(&mut self, w: WareIx, a: i32) {
        match self.cargo.iter_mut().find(|e| e.0 == w) {
            Some(e) => e.1 += a,
            None => self.cargo.push((w, a)),
        }
    }
}

impl StationEnv for Env {
    fn efficiency(&mut self, m: &Module, _i: usize, _t: f64) -> Factors {
        m.factors
    }
    fn can_store(&mut self, _p: &[WareAmount]) -> bool {
        true
    }
    fn has_resources(&mut self, r: &[WareAmount]) -> bool {
        r.iter().all(|&(w, a)| self.amount(w) >= a)
    }
    fn consume(&mut self, r: &[WareAmount], _t: f64) {
        for &(w, a) in r {
            self.add(w, -a);
        }
    }
    fn store(&mut self, p: &[WareAmount], _t: f64) -> Vec<WareAmount> {
        for &(w, a) in p {
            self.add(w, a);
        }
        p.to_vec()
    }
}

fn methods() -> Vec<Method> {
    vec![
        Method {
            time: 60.0,
            product: ECELLS,
            amount: 175,
            primary: vec![],
            secondary: vec![],
            tertiary: vec![],
            special: false,
        },
        Method {
            time: 150.0,
            product: METAL,
            amount: 88,
            primary: vec![(ECELLS, 90), (ORE, 240)],
            secondary: vec![],
            tertiary: vec![],
            special: false,
        },
    ]
}

fn module(addr: u64, m: &Method, factors: Factors) -> Module {
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
        factors,
        items: vec![Item {
            ware: m.product,
            method: Some(m.clone()),
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

/// A capture of one station: solar module and refinery, ore runs out half way (retries).
fn synthetic_capture() -> String {
    let ms = methods();
    let mut out = String::from("X4ECON 1 build=23660954\nW energycells 1\nW ore 10\nW refinedmetals 14\n");
    for (i, m) in ms.iter().enumerate() {
        write_method(&mut out, i, m);
    }
    let f = Factors { product: 1.13, time: 0.97, ..Factors::default() };
    let mut mods = vec![module(0x5000, &ms[0], f), module(0x4000, &ms[1], f)];
    let mut env = Env { cargo: vec![(ECELLS, 300), (ORE, 1500)] };
    let mut times = ContainerTimes::default();
    let mut rng = EconRng(42);
    let index = |m: &Method| ms.iter().position(|x| x == m);
    for frame in 0..400u64 {
        let now = 3.0 + frame as f64 * 7.25;
        // Lazy reads: the station is looked at only every few frames.
        if frame % 3 != 0 {
            continue;
        }
        update_production(&mut times, now, false, &mut mods, &mut env, &mut rng as &mut dyn ProductionRng);
        out.push_str(&format!("F {} {:#x} 0x0\n", frame, now.to_bits()));
        let rec = StationRecord {
            uid: 0x123,
            times,
            cargo: env.cargo.clone(),
            modules: mods.iter().map(|m| (m.addr | 0x1, m.clone())).collect(),
        };
        write_station(&mut out, &rec, &index);
    }
    out
}

#[test]
fn model_capture_replays_exactly() {
    let text = synthetic_capture();
    let cap = Capture::parse(&text).expect("parse");
    assert_eq!(cap.methods.len(), 2);
    let rep = replay(&cap);
    assert!(rep.transitions > 100, "{rep:?}");
    assert_eq!(rep.mismatched, 0, "{rep:?}");
    assert_eq!(rep.external, 0, "{rep:?}");
    assert!(rep.exact > 0, "{rep:?}");
    // Ore runs out: the refinery retries with random delays, which the replay explains from
    // the recorded next times (one draw per module per catch-up) or flags as undecidable.
    assert!(rep.exact_with_recorded_draws + rep.rng_dependent > 0, "{rep:?}");
    assert_eq!(rep.transitions, rep.exact + rep.exact_with_recorded_draws + rep.rng_dependent, "{rep:?}");
}

#[test]
fn corrupted_record_is_reported() {
    let text = synthetic_capture();
    // Change the state of the refinery in one record (8 = producing -> 6 = waiting).
    let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
    let pos = lines.iter().rposition(|l| l.starts_with("M 0x4001 0x4000 1 8 ")).expect("a producing refinery record");
    lines[pos] = lines[pos].replacen("M 0x4001 0x4000 1 8 ", "M 0x4001 0x4000 1 6 ", 1);
    let cap = Capture::parse(&lines.join("\n")).expect("parse");
    let rep = replay(&cap);
    assert!(rep.mismatched >= 1, "{rep:?}");
    assert!(rep.first_difference.as_deref().unwrap_or("").contains("module"), "{rep:?}");
}

#[test]
fn nan_times_are_rejected() {
    let text = "X4ECON 1 build=23660954\nF 1 0x7ff8000000000000 0x0\n";
    assert!(Capture::parse(text).is_err());
}
