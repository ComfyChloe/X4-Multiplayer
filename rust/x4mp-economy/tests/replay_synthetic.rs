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
    /// Refined metals storage limit (storage full: the refinery retries in "choosing item").
    metal_cap: Option<i32>,
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
    fn can_store(&mut self, p: &[WareAmount]) -> bool {
        match self.metal_cap {
            Some(cap) => !p.iter().any(|&(w, a)| w == METAL && a > 0 && self.amount(METAL) >= cap),
            None => true,
        }
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

/// A capture of one station: solar module and refinery, ore runs out half way (retries);
/// with `metal_cap`, refined metals storage fills up first (storage retries).
fn synthetic_capture() -> String {
    capture_with(None, 3)
}

/// `stride`: the station is read every `stride` frames of 7.25 s.
fn capture_with(metal_cap: Option<i32>, stride: u64) -> String {
    let ms = methods();
    let mut out = String::from("X4ECON 1 build=23660954\nW energycells 1\nW ore 10\nW refinedmetals 14\n");
    for (i, m) in ms.iter().enumerate() {
        write_method(&mut out, i, m);
    }
    let f = Factors { product: 1.13, time: 0.97, ..Factors::default() };
    let mut mods = vec![module(0x5000, &ms[0], f), module(0x4000, &ms[1], f)];
    let mut env = Env { cargo: vec![(ECELLS, 300), (ORE, 1500)], metal_cap };
    let mut times = ContainerTimes::default();
    let mut rng = EconRng(42);
    let index = |m: &Method| ms.iter().position(|x| x == m);
    for frame in 0..400u64 {
        let now = 3.0 + frame as f64 * 7.25;
        // Lazy reads: the station is looked at only every few frames.
        if frame % stride != 0 {
            continue;
        }
        update_production(&mut times, now, false, &mut mods, &mut env, &mut rng as &mut dyn ProductionRng);
        out.push_str(&format!("F {} {:#x} 0x0\n", frame, now.to_bits()));
        let rec = StationRecord {
            uid: 0x123,
            times,
            cargo: env.cargo.clone(),
            modules: mods.iter().map(|m| (m.addr | 0x1, m.clone())).collect(),
            offers: Vec::new(),
            reservations: Vec::new(),
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

#[test]
fn offer_and_reservation_records_parse() {
    let text = "X4ECON 1 build=23660954
W refinedmetals 0
F 7 0x4029000000000000 0x1
C 0x5 0x4024000000000000 0xbff0000000000000 0xbff0000000000000 0 0 0
O 0x5 1 0x140003 0 0x0 0x9 600 0 600 17340 0x40
V 0x5 0x9 0x777 0x0 0 300 0x40ac340000000000
";
    let cap = Capture::parse(text).expect("parse");
    let st = &cap.frames[0].stations[0];
    assert_eq!(st.offers.len(), 1);
    assert_eq!((st.offers[0].amount, st.offers[0].unit_price, st.offers[0].flags), (600, 17340, 0x40));
    assert_eq!(st.reservations[0].amount, 300);
    assert_eq!(st.reservations[0].expiry, 3610.0);
}

#[test]
fn storage_retries_replay_with_the_recorded_storage_decision() {
    // Refined metals storage is full after two cycles: the refinery then retries in
    // "choosing item" with random delays. The replay does not model storage; it takes the
    // decision from the recorded state and must explain every transition.
    // Read every frame (7.25 s): mostly one 5-6 s retry per catch-up.
    let text = capture_with(Some(150), 1);
    let rep = replay(&Capture::parse(&text).expect("parse"));
    assert_eq!(rep.mismatched, 0, "{rep:?}");
    assert!(rep.exact_with_storage_decision > 0, "{rep:?}");
    assert_eq!(
        rep.transitions,
        rep.exact + rep.exact_with_recorded_draws + rep.exact_with_storage_decision + rep.rng_dependent + rep.external,
        "{rep:?}"
    );
}

#[test]
fn records_embedded_in_a_debug_log_are_found() {
    let text = synthetic_capture();
    let mut log = String::from(
        "Logfile started
[=ERROR=] 0.00 something else
",
    );
    for (i, l) in text.lines().enumerate() {
        log.push_str(&format!(
            "[=ERROR=] {}.00 [X4LAN] ECONCAP {l}
",
            i
        ));
        if i % 7 == 0 {
            log.push_str(
                "======================================
[General] 1.0 unrelated 1 2 3
",
            );
        }
    }
    let a = replay(&Capture::parse(&text).expect("plain"));
    let b = replay(&Capture::parse(&log).expect("embedded"));
    assert_eq!((a.transitions, a.exact, a.mismatched), (b.transitions, b.exact, b.mismatched));
}
