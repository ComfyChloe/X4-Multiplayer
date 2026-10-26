#![allow(clippy::too_many_arguments)]
//! Offline replay tests of `move.generic` against hand-built traces.
//!
//! SYNTHETIC: each trace is written by walking `aiscripts/move.generic.xml` (base 9.00) line by line;
//! the comments cite the line. Draw values come from the reference functions below (an independent
//! transcription of reports/REWRITE-AI-ORDERS.md section 3), not from the crate's `rng` module. Engine
//! draws inside queries (`draws=`) are arbitrary here; natively they vary (section 9a).

use x4mp_ai::replay::replay;
use x4mp_ai::trace::{Record, Trace};
use x4mp_ai::val::{OrderId, Pos, Uid, Val};

// ---------- reference draw functions (independent transcription) ----------

fn ref_step(s: u64) -> u64 {
    s.wrapping_mul(0x5851_F42D_4C95_7F2D).wrapping_add(0x1405_7B7E_F767_814F).rotate_right(30)
}
fn ref_float(s: &mut u64, max: f32) -> f32 {
    *s = ref_step(*s);
    ((*s as u32) as f32) * (1.0f32 / 4294967296.0) * max
}
fn ref_chance(s: &mut u64, c: f32) -> bool {
    *s = ref_step(*s);
    let x = ((*s as u32) as f32) * (100.0f32 / 4294967296.0);
    // Match the engine's unordered comparison: NaN passes.
    #[allow(clippy::neg_cmp_op_on_partial_ord)]
    let pass = !(x >= c);
    pass
}
/// Time ranges: f64 difference, one flat float draw (none when the width is not positive), f64 sum.
fn ref_time(s: &mut u64, min: f64, max: f64) -> f64 {
    let d = max - min;
    #[allow(clippy::neg_cmp_op_on_partial_ord)]
    if !(d > 0.0) {
        return min;
    }
    min + ref_float(s, d as f32) as f64
}

// ---------- trace builder ----------

const P: Uid = Uid(0x100); // pilot (context entity)
const S: Uid = Uid(0x200); // ship
const Z: Uid = Uid(0x300); // start zone in X
const ZT: Uid = Uid(0x310); // target zone in X2
const ZE: Uid = Uid(0x311); // a zone next to the target in X2 (end of a long move)
const ZG: Uid = Uid(0x312); // the zone around gate G2 in X2
const X: Uid = Uid(0x400); // start sector
const X2: Uid = Uid(0x410); // target sector
const C: Uid = Uid(0x500); // cluster
const G1: Uid = Uid(0x600); // jump gate in X
const G2: Uid = Uid(0x601); // its destination gate in X2
const E: Uid = Uid(0x700); // a hostile ship in X
const O: OrderId = OrderId(0x51); // the ship's current order (Patrol)

struct B {
    t: Trace,
    rng: u64,
    now: f64,
}

fn c(u: Uid) -> Val {
    Val::uid(u)
}
fn s(x: &str) -> Val {
    Val::Str(x.into())
}
fn k(x: &str) -> Val {
    Val::key(x)
}
fn bv(x: bool) -> Val {
    Val::Bool(x)
}

impl B {
    fn new() -> B {
        B { t: Trace::new("handbuilt"), rng: 0, now: 0.0 }
    }
    fn rec(&mut self, r: Record) {
        self.t.push(r);
    }
    fn fact(&mut self, subj: Uid, prop: &str, v: Val) {
        self.rec(Record::new("fact", subj, prop).arg("v", v));
    }
    fn ofact(&mut self, o: OrderId, prop: &str, v: Val) {
        self.rec(Record::new("fact", Uid::NULL, prop).arg("order", Val::Order(o)).arg("v", v));
    }
    fn run(&mut self, t: f64, wake: &str, rng: u64) {
        self.run_of("move.generic", t, wake, rng);
    }
    fn run_of(&mut self, script: &str, t: f64, wake: &str, rng: u64) {
        self.now = t;
        self.rng = rng;
        self.rec(Record::new("run", P, script).arg("t", Val::F64(t)).arg("wake", k(wake)).arg("rng", Val::U64(rng)));
    }
    fn roll(&mut self, line: i64, chance: f32) -> bool {
        let pass = ref_chance(&mut self.rng, chance);
        self.rec(
            Record::new("roll", P, "-")
                .arg("line", Val::Int(line))
                .arg("chance", Val::F32(chance))
                .arg("pass", Val::Bool(pass)),
        );
        pass
    }
    fn rnd_time(&mut self, line: i64, min: f64, max: f64) -> f64 {
        let v = ref_time(&mut self.rng, min, max);
        self.rec(Record::new("rnd", P, "-").arg("line", Val::Int(line)).arg("v", Val::F64(v)));
        v
    }
    fn fx(&mut self, kind: &str, args: Vec<(&str, Val)>) {
        let mut r = Record::new("fx", P, kind);
        for (a, v) in args {
            r = r.arg(a, v);
        }
        self.rec(r);
    }
    fn q(&mut self, kind: &str, args: Vec<(&str, Val)>, res: Vec<(&str, Val)>, draws: u64) {
        let mut r = Record::new("q", P, kind);
        for (a, v) in args {
            r = r.arg(a, v);
        }
        for (a, v) in res {
            r = r.res(a, v);
        }
        r = r.res("draws", Val::Int(draws as i64));
        for _ in 0..draws {
            self.rng = ref_step(self.rng);
        }
        self.rec(r);
    }
    fn yield_(&mut self, kind: &str, args: Vec<(&str, Val)>) {
        self.yield_of("move.generic", kind, args);
    }
    fn yield_of(&mut self, script: &str, kind: &str, args: Vec<(&str, Val)>) {
        let mut r = Record::new("yield", P, script).arg("kind", k(kind));
        for (a, v) in args {
            r = r.arg(a, v);
        }
        let r = r.arg("rng", Val::U64(self.rng));
        self.rec(r);
    }
    fn wait(&mut self, until: f64) {
        self.yield_("wait", vec![("until", Val::F64(until))]);
    }
    /// Delivery with the engine stream before and after the conditions (`draws` made inside them).
    fn ev(&mut self, path: &str, name: &str, verdict: Val, rng: u64, draws: u64, args: Vec<(&str, Val)>) {
        let mut after = rng;
        for _ in 0..draws {
            after = ref_step(after);
        }
        let mut r = Record::new("ev", P, name)
            .arg("path", k(path))
            .arg("verdict", verdict)
            .arg("t", Val::F64(self.now))
            .arg("rng", Val::U64(rng))
            .arg("rng_after", Val::U64(after));
        for (a, v) in args {
            r = r.arg(a, v);
        }
        self.rng = rng;
        self.rec(r);
    }
    fn irun(&mut self, prog: &str, rng: u64) {
        self.rng = rng;
        self.rec(
            Record::new("irun", P, "move.generic")
                .arg("prog", k(prog))
                .arg("t", Val::F64(self.now))
                .arg("rng", Val::U64(rng)),
        );
    }
    fn irend(&mut self, args: Vec<(&str, Val)>) {
        let mut r = Record::new("irend", P, "-");
        for (a, v) in args {
            r = r.arg(a, v);
        }
        let r = r.arg("rng", Val::U64(self.rng));
        self.rec(r);
    }
    /// The ship (and its pilot) are now in `zone` of `sector`.
    fn at(&mut self, zone: Uid, sector: Uid) {
        for e in [P, S] {
            self.fact(e, "zone", c(zone));
            self.fact(e, "sector", c(sector));
        }
    }
}

fn targetclasses() -> Val {
    Val::List(
        ["class.ship_s", "class.ship_m", "class.ship_l", "class.ship_xl", "class.station"]
            .iter()
            .map(|x| k(x))
            .collect(),
    )
}

/// An NPC ship (ship_m, or ship_l when `capital`) of purpose fight in zone Z of sector X; the target
/// zone ZT is one jump away in X2. Lanes run with X4's script debug channel on.
fn world(b: &mut B, capital: bool) {
    b.fact(P, "ship", c(S));
    b.fact(P, "cluster", c(C));
    b.fact(P, "isplayerowned", bv(false));
    b.at(Z, X);
    b.fact(S, "class", k(if capital { "ship_l" } else { "ship_m" }));
    b.fact(S, "cluster", c(C));
    b.fact(S, "iscapitalship", bv(capital));
    b.fact(S, "isplayerowned", bv(false));
    b.fact(S, "isoperational", bv(true));
    b.fact(S, "primarypurpose", k("purpose.fight"));
    b.fact(S, "isformationleader", bv(false));
    b.fact(S, "size", Val::F32(40.0));
    b.fact(S, "safesize", Val::F32(80.0));
    b.fact(S, "maxradarrange", Val::F32(10000.0));
    b.fact(S, "pilot.skill.piloting", Val::Int(9));
    b.fact(S, "order", Val::Order(O));
    b.fact(S, "gatedistance", Val::List(vec![c(ZT), Val::Int(1)]));
    b.ofact(O, "id", s("Patrol"));
    b.ofact(O, "isrunning", bv(true));
    b.ofact(O, "state", k("orderstate.started"));
    for (z, x) in [(Z, X), (ZT, X2), (ZE, X2), (ZG, X2)] {
        b.fact(z, "class", k("zone"));
        b.fact(z, "sector", c(x));
        b.fact(z, "cluster", c(C));
    }
    for x in [X, X2] {
        b.fact(x, "class", k("sector"));
        b.fact(x, "cluster", c(C));
    }
    b.fact(C, "class", k("cluster"));
    b.fact(G1, "class", k("gate"));
    b.fact(G1, "sector", c(X));
    b.fact(G1, "destination", c(G2));
    b.fact(G2, "class", k("gate"));
    b.fact(G2, "sector", c(X2));
    b.fact(Uid::NULL, "param.spacereservation.maxduration", Val::F64(1800.0));
    b.fact(Uid::NULL, "debug.channel", bv(true));
}

/// The call of order.fight.patrol L434 (`destination` = the patrol zone in another sector).
fn start_patrol_call(b: &mut B, dest: Uid) {
    b.rec(
        Record::new("start", P, "move.generic")
            .arg("param.destination", c(dest))
            .arg("param.endintargetzone", bv(true))
            .arg("param.activepatrol", bv(true))
            .arg("param.targetclasses", targetclasses())
            .arg("param.debugchance", Val::F32(0.0)),
    );
}

fn safe_pos_res(pos: Pos, index: i64, zone: Uid) -> Vec<(&'static str, Val)> {
    vec![("pos", Val::Pos(pos)), ("index", Val::Int(index)), ("zone", c(zone))]
}

/// Runs 1-2: start (GetBlacklistgroup, label start, check) and the input case (a normal zone).
fn first_runs(b: &mut B, t0: f64) -> f64 {
    b.run(t0, "start", 0x0123_4567_89ab_cdef);
    b.roll(27, 0.0); // lib.define.blacklistgroup L27 (operational, purpose fight -> military)
    b.fx("set_to_default_flight_control_model", vec![("object", c(S))]); // L364
    b.roll(366, 0.0);
    let t1 = t0 + 0.001; // L382 wait exact 1ms
    b.wait(t1);
    b.run(t1, "timer", 0x1111_2222_3333_4444);
    b.roll(400, 0.0);
    // L403 zone, not a highway, not a temp zone: $targetzone = $positionspace = ZT; L608 move
    let t2 = t1 + 0.001; // L616 wait exact 1ms
    b.wait(t2);
    t2
}

/// The `move_to` flags of L1051.
fn long_move_flags(localhw: bool, abortpath: bool, blacklist: Val) -> Vec<(&'static str, Val)> {
    vec![
        ("uselocalhighways", bv(localhw)),
        ("useknownpath", bv(false)),
        ("abortpath", bv(abortpath)),
        ("forcesteering", bv(false)),
        ("forceposition", bv(false)),
        ("travel", bv(true)),
        ("useblacklist", blacklist),
        ("commandaction", bv(true)),
    ]
}

/// Small ship to a zone in the next sector: stagger wait, long move ended by the zone-change interrupt,
/// then in the target zone with endintargetzone: finish without a short move. Returns true.
fn scenario_small_ship() -> Trace {
    let mut b = B::new();
    world(&mut b, false);
    start_patrol_call(&mut b, ZT);
    let t2 = first_runs(&mut b, 100.0);

    // Run 3: after the L616 wait
    b.run(t2, "timer", 0x2222_3333_4444_5555);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]); // L644
                                                                                         // L651 new reservation table; L656 no position; ZT is a zone that has itself as context: L664
    let p1 = Pos::new(1200.0, -50.0, 800.0);
    b.q(
        "get_safe_pos_general",
        vec![
            ("zone", c(ZT)),
            ("object", c(ZT)),
            ("radius", Val::F32(160.0)),
            ("max", Val::F32(240.0)),
            ("ignored", c(S)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        safe_pos_res(p1, 7, ZT),
        4,
    );
    b.roll(677, 0.0);
    b.roll(782, 0.0);
    b.roll(788, 0.0);
    // L794: other sector -> long distance
    b.roll(795, 0.0);
    // L803 inter-sector: gatedistance 1; L845 (small ship may use local highways)
    b.q(
        "get_jump_path",
        vec![
            ("start", c(S)),
            ("end", c(X2)),
            ("uselocalhighways", bv(true)),
            ("useblacklist", k("blacklistgroup.military")),
            ("refobject", c(S)),
            ("useknownpath", bv(false)),
        ],
        vec![("path", Val::List(vec![c(G1), c(G2)]))],
        0,
    );
    // L883 loctargetzone = ZT; no subordinates; L949 not a capital ship -> L1022
    let max = (5.1f32 - (9 / 3) as f32) as f64;
    let w = b.rnd_time(1022, 0.0, max);
    let t3 = t2 + w;
    b.wait(t3);

    // Run 4: L1048, L1051
    b.run(t3, "timer", 0x3333_4444_5555_6666);
    b.roll(1048, 0.0);
    b.q(
        "move_to",
        vec![("object", c(S)), ("destination", c(ZT)), ("pos", Val::Pos(p1))]
            .into_iter()
            .chain(long_move_flags(true, true, k("blacklistgroup.military")))
            .collect(),
        vec![("endzone", c(ZT))],
        0,
    );
    b.yield_("moveto", vec![]);

    // The ship enters ZT: the move_to's interrupt (L1053-1058). The debug condition L1056 draws once.
    b.now = t3 + 40.0;
    b.at(ZT, X2);
    b.ev(
        "block",
        "event_object_changed_zone",
        k("matched"),
        0x4444_5555_6666_7777,
        1,
        vec![("object", c(S)), ("param", c(ZT)), ("param2", c(Z))],
    );
    b.irun("block", 0x5555_6666_7777_8888);
    b.roll(1061, 0.0);
    b.irend(vec![("outcome", k("block"))]);

    // Run 5: after the move: L1065; endzone set; L1115 resume check
    b.run(t3 + 40.0, "block", 0x6666_7777_8888_9999);
    b.roll(1065, 0.0);
    let t5 = t3 + 40.0 + 0.001;
    b.wait(t5);
    // Run 6: check again, same input case
    b.run(t5, "timer", 0x7777_8888_9999_aaaa);
    b.roll(400, 0.0);
    let t6 = t5 + 0.001;
    b.wait(t6);
    // Run 7: the reservation is renewed; $position is kept: L757 with value
    b.run(t6, "timer", 0x8888_9999_aaaa_bbbb);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(7)), ("zone", c(ZT))]); // L648
    b.roll(649, 0.0);
    let p2 = Pos::new(1210.0, -50.0, 790.0);
    b.q(
        "get_safe_pos_general",
        vec![
            ("zone", c(ZT)),
            ("value", Val::Pos(p1)),
            ("radius", Val::F32(40.0)),
            ("max", Val::F32(240.0)),
            ("ignored", c(S)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        safe_pos_res(p2, 8, ZT),
        3,
    );
    b.roll(778, 0.0); // the position changed
    b.roll(782, 0.0);
    b.roll(788, 0.0);
    // L794: same sector, in the target zone -> short distance
    b.roll(1118, 0.0);
    // L1161 endintargetzone and in the target zone
    b.roll(1162, 0.0);
    // L1286 finish, L1326 cleanup
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(8)), ("zone", c(ZT))]); // L1329
    b.fx("reset_avoid_collisions", vec![("object", c(S))]);
    b.fx("set_to_default_flight_control_model", vec![("object", c(S))]);
    b.fx("stop_boost", vec![("object", c(S))]);
    b.yield_("return", vec![("v", bv(true))]);
    b.t
}

/// Capital ship: through the gate with move.gate (called script, opaque here), the planned gate entry
/// does not restart the move (SectorChangeHandler with `$nextsector`), then a long move inside the
/// target sector that ends in a neighbouring zone, then the short move to a safe position. Returns true.
fn scenario_capital_ship() -> Trace {
    let mut b = B::new();
    world(&mut b, true);
    start_patrol_call(&mut b, ZT);
    let t2 = first_runs(&mut b, 200.0);

    // Run 3: as for the small ship up to the jump path (no local highways for capital ships, L851)
    b.run(t2, "timer", 0x2121_3131_4141_5151);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    let p1 = Pos::new(-3000.0, 100.0, 2500.0);
    b.q(
        "get_safe_pos_general",
        vec![
            ("zone", c(ZT)),
            ("object", c(ZT)),
            ("radius", Val::F32(160.0)),
            ("max", Val::F32(240.0)),
            ("ignored", c(S)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        safe_pos_res(p1, 7, ZT),
        2,
    );
    b.roll(677, 0.0);
    b.roll(782, 0.0);
    b.roll(788, 0.0);
    b.roll(795, 0.0);
    b.q(
        "get_jump_path",
        vec![
            ("start", c(S)),
            ("end", c(X2)),
            ("uselocalhighways", bv(false)),
            ("useblacklist", k("blacklistgroup.military")),
            ("refobject", c(S)),
            ("useknownpath", bv(false)),
        ],
        vec![("path", Val::List(vec![c(G1), c(G2)]))],
        0,
    );
    // L949 capital ship: gate G1 -> G2; L972 nextpos = [ZT, p1]; L979, L983 (chance ... * 0)
    b.roll(979, 0.0);
    b.roll(983, 0.0);
    b.fx(
        "run_script",
        vec![
            ("name", s("move.gate")),
            ("param.gate", c(G1)),
            ("param.recallsubordinates", bv(false)),
            ("param.waitfor", Val::Null),
            ("param.noboost", bv(false)),
            ("param.nextpos", Val::List(vec![c(ZT), Val::Pos(p1)])),
            ("param.debugchance", Val::F32(0.0)),
        ],
    );
    b.yield_("call", vec![("script", s("move.gate"))]);

    // move.gate (not ported): flies to the gate, enters it
    b.run_of("move.gate", t2, "start", 0x3131_4141_5151_6161);
    b.rng = ref_step(ref_step(b.rng));
    b.yield_of("move.gate", "moveto", vec![]);
    b.now = t2 + 90.0;
    // Entering G1 (param = the entered gate, param2 = the destination gate in X2): move.gate has no
    // handler for it; move.generic's SectorChangeHandler matches, but X2 is the planned $nextsector.
    b.ev(
        "global",
        "event_object_entered_gate",
        s("move.generic/SectorChangeHandler"),
        0x4141_5151_6161_7171,
        0,
        vec![("object", c(S)), ("param", c(G1)), ("param2", c(G2))],
    );
    b.irun("handler", 0x5151_6161_7171_8181);
    b.irend(vec![("outcome", k("handled"))]);
    b.at(ZG, X2);
    b.run_of("move.gate", t2 + 95.0, "block", 0x6161_7171_8181_9191);
    b.rng = ref_step(b.rng);
    b.yield_of("move.gate", "return", vec![]);

    // Run 4: back after the call: L1005 no further gate -> $locabortpath = false; L1115 resume check
    let t4 = t2 + 95.0;
    b.run(t4, "return", 0x7171_8181_9191_a1a1);
    b.wait(t4 + 0.001);
    b.run(t4 + 0.001, "timer", 0x8181_9191_a1a1_b1b1);
    b.roll(400, 0.0);
    b.wait(t4 + 0.001 + 0.001);

    // Run 5: in X2 but in ZG, endintargetzone: long distance again, now inside the sector
    let t5 = t4 + 0.001 + 0.001;
    b.run(t5, "timer", 0x9191_a1a1_b1b1_c1c1);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(7)), ("zone", c(ZT))]);
    b.roll(649, 0.0);
    let p2 = Pos::new(-2990.0, 100.0, 2510.0);
    b.q(
        "get_safe_pos_general",
        vec![
            ("zone", c(ZT)),
            ("value", Val::Pos(p1)),
            ("radius", Val::F32(40.0)),
            ("max", Val::F32(240.0)),
            ("ignored", c(S)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        safe_pos_res(p2, 8, ZT),
        3,
    );
    b.roll(778, 0.0);
    b.roll(782, 0.0);
    b.roll(788, 0.0);
    b.roll(795, 0.0);
    // same sector: no jump path; no subordinates; not inter-sector -> L1018 with $bigship: max 0, no draw
    let w = b.rnd_time(1022, 0.0, 0.0);
    assert_eq!(w, 0.0);
    b.wait(t5);
    // Run 6: same frame, next pass
    b.run(t5, "timer", 0xa1a1_b1b1_c1c1_d1d1);
    b.roll(1048, 0.0);
    b.q(
        "move_to",
        vec![("object", c(S)), ("destination", c(ZT)), ("pos", Val::Pos(p2))]
            .into_iter()
            .chain(long_move_flags(false, false, Val::Null))
            .collect(),
        vec![("endzone", c(ZE))],
        0,
    );
    b.yield_("moveto", vec![]);
    // arrives in ZE (no interrupt: the endzone is reached as the move ends)
    let t6 = t5 + 60.0;
    b.at(ZE, X2);
    b.run(t6, "block", 0xb1b1_c1c1_d1d1_e1e1);
    b.roll(1065, 0.0);
    b.wait(t6 + 0.001);
    b.run(t6 + 0.001, "timer", 0xc1c1_d1d1_e1e1_f1f1);
    b.roll(400, 0.0);
    b.wait(t6 + 0.001 + 0.001);

    // Run 7: in the endzone: short distance (L794 false because this.zone == $endzone)
    let t7 = t6 + 0.001 + 0.001;
    b.run(t7, "timer", 0xd1d1_e1e1_f1f1_0101);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(8)), ("zone", c(ZT))]);
    b.roll(649, 0.0);
    let p3 = Pos::new(-2985.0, 100.0, 2515.0);
    b.q(
        "get_safe_pos_general",
        vec![
            ("zone", c(ZT)),
            ("value", Val::Pos(p2)),
            ("radius", Val::F32(40.0)),
            ("max", Val::F32(240.0)),
            ("ignored", c(S)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        safe_pos_res(p3, 9, ZT),
        2,
    );
    b.roll(778, 0.0);
    b.roll(782, 0.0);
    b.roll(788, 0.0);
    b.roll(1118, 0.0);
    // L1161: endintargetzone, but ZE is not the target zone -> L1168
    b.fx("reset_avoid_collisions", vec![("object", c(S))]);
    b.roll(1171, 0.0);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(9)), ("zone", c(ZT))]); // L1181
    b.roll(1187, 0.0);
    let sp = Pos::new(-2980.0, 100.0, 2520.0);
    b.q(
        "get_safe_pos_general",
        vec![
            ("zone", c(ZT)),
            ("value", Val::Pos(p3)),
            ("object", c(ZT)),
            ("radius", Val::F32(80.0)),
            ("ignored", c(S)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        safe_pos_res(sp, 10, ZT),
        4,
    );
    b.roll(1200, 0.0); // inside the 50 km box
    let p4 = Pos::new(-2985.0, 100.0, 2515.0);
    b.q(
        "create_position_in",
        vec![("space", c(ZT)), ("value", Val::Pos(p3)), ("object", c(ZT))],
        vec![("pos", Val::Pos(p4))],
        0,
    );
    b.roll(1214, 0.0);
    b.roll(1223, 0.0);
    // L1243: 5 km away (more than the ship's size). Publish before the last observed
    // action so the replay input cursor applies this fact before the native condition reads it.
    b.rec(
        Record::new("fact", S, "distanceto.pos")
            .arg("a", Val::List(vec![c(ZT), Val::Pos(sp)]))
            .arg("v", Val::F32(5000.0)),
    );
    b.roll(1239, 0.0);
    b.roll(1240, 0.0);
    b.q(
        "move_to",
        vec![
            ("object", c(S)),
            ("destination", c(ZT)),
            ("pos", Val::Pos(sp)),
            ("uselocalhighways", bv(false)),
            ("abortpath", bv(true)),
            ("forcesteering", bv(false)),
            ("forceposition", bv(false)),
            ("forcerotation", bv(false)),
            ("finishonapproach", bv(false)),
            ("travel", bv(true)),
            ("relativemovement", bv(false)),
            ("commandaction", bv(true)),
        ],
        vec![("endzone", c(ZT))],
        0,
    );
    b.yield_("moveto", vec![]);
    // Run 8: arrived: finish, cleanup, return true
    b.at(ZT, X2);
    b.run(t7 + 20.0, "block", 0xe1e1_f1f1_0101_1111);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(10)), ("zone", c(ZT))]);
    b.fx("reset_avoid_collisions", vec![("object", c(S))]);
    b.fx("set_to_default_flight_control_model", vec![("object", c(S))]);
    b.fx("stop_boost", vec![("object", c(S))]);
    b.yield_("return", vec![("v", bv(true))]);
    b.t
}

/// Runs 1-4 of the small-ship scenario (up to the blocking move_to of L1051).
fn small_ship_until_long_move() -> B {
    let mut b = B::new();
    world(&mut b, false);
    start_patrol_call(&mut b, ZT);
    let t2 = first_runs(&mut b, 100.0);
    b.run(t2, "timer", 0x2222_3333_4444_5555);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    let p1 = Pos::new(1200.0, -50.0, 800.0);
    b.q(
        "get_safe_pos_general",
        vec![
            ("zone", c(ZT)),
            ("object", c(ZT)),
            ("radius", Val::F32(160.0)),
            ("max", Val::F32(240.0)),
            ("ignored", c(S)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        safe_pos_res(p1, 7, ZT),
        4,
    );
    for l in [677, 782, 788, 795] {
        b.roll(l, 0.0);
    }
    b.q(
        "get_jump_path",
        vec![
            ("start", c(S)),
            ("end", c(X2)),
            ("uselocalhighways", bv(true)),
            ("useblacklist", k("blacklistgroup.military")),
            ("refobject", c(S)),
            ("useknownpath", bv(false)),
        ],
        vec![("path", Val::List(vec![c(G1), c(G2)]))],
        0,
    );
    let w = b.rnd_time(1022, 0.0, (5.1f32 - 3.0) as f64);
    let t3 = t2 + w;
    b.wait(t3);
    b.run(t3, "timer", 0x3333_4444_5555_6666);
    b.roll(1048, 0.0);
    b.q(
        "move_to",
        vec![("object", c(S)), ("destination", c(ZT)), ("pos", Val::Pos(p1))]
            .into_iter()
            .chain(long_move_flags(true, true, k("blacklistgroup.military")))
            .collect(),
        vec![("endzone", c(ZT))],
        0,
    );
    b.yield_("moveto", vec![]);
    b
}

/// The small ship's long move, then a gravidar scan finds an enemy (handler 3: count_gravidar_contacts
/// in the conditions draws once) and creates an Attack order; orders.base then aborts the stack.
fn scenario_gravidar_attack() -> Trace {
    let mut b = small_ship_until_long_move();
    b.fact(E, "class", k("ship_s"));
    b.fact(E, "sector", c(X));
    b.fact(S, "cansee", Val::List(vec![c(E)]));
    b.now += 5.0;
    b.ev(
        "global",
        "event_gravidar_has_scanned",
        s("move.generic/inline3"),
        0x0a0a_0b0b_0c0c_0d0d,
        1,
        vec![("object", c(S))],
    );
    b.q(
        "count_gravidar_contacts",
        vec![("object", c(S)), ("class", targetclasses()), ("maybeattackedby", c(S))],
        vec![("contact", c(E))],
        1,
    );
    b.irun("handler", 0x0e0e_0f0f_1010_1111);
    b.roll(165, 0.0);
    b.roll(167, 0.0);
    b.roll(168, 0.0);
    b.fx(
        "create_order",
        vec![
            ("id", s("Attack")),
            ("object", c(S)),
            ("immediate", bv(true)),
            ("param.primarytarget", c(E)),
            ("param.escort", Val::Null),
            ("param.pursuetargets", bv(false)),
            ("param.targetclasses", targetclasses()),
            ("param.radius", Val::Null),
            ("param.radiusanchorpos", Val::Null),
            ("param.radiusanchorspace", Val::Null),
            ("param.internalorder", bv(true)),
            ("param.debugchance", Val::F32(0.0)),
        ],
    );
    b.irend(vec![("outcome", k("handled"))]);
    // orders.base starts the Attack order: the whole stack is aborted; move.generic's <on_abort>
    b.rec(Record::new("abort", P, "orders.base"));
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(7)), ("zone", c(ZT))]); // L1382
    b.fx("reset_avoid_collisions", vec![("object", c(S))]);
    b.fx("set_to_default_flight_control_model", vec![("object", c(S))]);
    b.fx("stop_boost", vec![("object", c(S))]);
    b.fx("stop_moving", vec![("object", c(S))]); // L1388: the next order is not Flee
    b.t
}

/// The small ship's long move, then a `move.generic_reinitialize` signal (handler 1): restart at check.
fn scenario_reinitialize() -> Trace {
    let mut b = small_ship_until_long_move();
    b.now += 3.0;
    b.ev(
        "global",
        "event_object_signalled",
        s("move.generic/inline1"),
        0x1212_1313_1414_1515,
        0,
        vec![("object", c(S)), ("param", s("move.generic_reinitialize"))],
    );
    b.irun("handler", 0x1616_1717_1818_1919);
    b.roll(103, 0.0);
    b.irend(vec![("outcome", k("abort")), ("label", k("check"))]);
    let t = b.now;
    b.run(t, "restart", 0x1a1a_1b1b_1c1c_1d1d);
    b.wait(t + 0.001); // L380 check, L382
    b.run(t + 0.001, "timer", 0x1e1e_1f1f_2020_2121);
    b.roll(400, 0.0);
    b.wait(t + 0.001 + 0.001);
    b.t
}

fn check(t: &Trace) {
    let rep = replay(t);
    assert!(rep.ok(), "{}", rep.summary());
}

#[test]
fn small_ship_long_move_to_the_next_sector_replays_bit_exactly() {
    let t = scenario_small_ship();
    let rep = replay(&t);
    assert!(rep.ok(), "{}", rep.summary());
    let e = &rep.entities[&P.0];
    assert_eq!(e.runs, 7);
    assert_eq!(e.deliveries, 1);
    assert_eq!(e.interrupt_runs, 1);
    check(&Trace::parse(&t.to_string()).expect("text round trip"));
}

#[test]
fn capital_ship_through_a_gate_then_a_short_move_replays_bit_exactly() {
    let t = scenario_capital_ship();
    let rep = replay(&t);
    assert!(rep.ok(), "{}", rep.summary());
    let e = &rep.entities[&P.0];
    assert_eq!((e.runs, e.opaque_runs, e.deliveries, e.interrupt_runs), (11, 2, 1, 1));
}

#[test]
fn a_gravidar_target_creates_an_attack_order() {
    let t = scenario_gravidar_attack();
    let rep = replay(&t);
    assert!(rep.ok(), "{}", rep.summary());
    assert_eq!(rep.entities[&P.0].interrupt_runs, 1);
}

#[test]
fn the_reinitialize_signal_restarts_at_check() {
    check(&scenario_reinitialize());
}

#[test]
fn a_wrong_engine_draw_count_in_the_conditions_is_caught() {
    // The gravidar conditions drew once (count_gravidar_contacts); claim two in the capture.
    let mut t = scenario_gravidar_attack();
    for r in t.records.iter_mut() {
        if r.tag == "ev" && r.name == "event_gravidar_has_scanned" {
            let after = r.get("rng_after").and_then(|v| v.as_u64()).unwrap();
            r.args.retain(|(k, _)| k != "rng_after");
            r.args.push(("rng_after".into(), Val::U64(ref_step(after))));
        }
    }
    let rep = replay(&t);
    let m = rep.entities[&P.0].first_mismatch.clone().unwrap_or_default();
    assert!(m.contains("RNG after the delivery"), "{m}");
}

#[test]
fn unrelated_global_events_still_check_the_rng_state() {
    let mut b = small_ship_until_long_move();
    b.ev(
        "global",
        "event_object_signalled",
        k("none"),
        0xabcdef,
        0,
        vec![("object", c(S)), ("param", s("unrelated_signal"))],
    );
    check(&b.t);
    let r = b.t.records.last_mut().unwrap();
    for (key, v) in &mut r.args {
        if key == "rng_after" {
            *v = Val::U64(ref_step(0xabcdef));
        }
    }
    let rep = replay(&b.t);
    let m = rep.entities[&P.0].first_mismatch.as_deref().unwrap_or_default();
    assert!(m.contains("RNG after the delivery"), "{m}");
}

#[test]
fn destroyed_destination_queues_a_nonconsuming_cleanup_handler() {
    let mut b = small_ship_until_long_move();
    // consume=false queues actions but does not report a consuming handler.
    b.ev("global", "event_object_destroyed", k("none"), 0x1234, 0, vec![("object", c(ZT))]);
    b.irun("handler", 0x2345);
    b.roll(113, 0.0);
    b.irend(vec![("outcome", k("abort")), ("label", k("cleanup"))]);
    b.run(b.now, "restart", 0x3456);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(7)), ("zone", c(ZT))]);
    b.fx("reset_avoid_collisions", vec![("object", c(S))]);
    b.fx("set_to_default_flight_control_model", vec![("object", c(S))]);
    b.fx("stop_boost", vec![("object", c(S))]);
    b.yield_("return", vec![("v", bv(false))]);
    check(&b.t);
}

#[test]
fn expired_reservation_is_not_cancelled_again_on_abort() {
    let mut b = small_ship_until_long_move();
    b.ev(
        "global",
        "event_space_reservation_expired",
        s("move.generic/LostReservationHandler"),
        0x1234,
        0,
        vec![("object", c(S)), ("param", c(ZT)), ("param2", Val::Int(7))],
    );
    b.irun("handler", 0x2345);
    b.irend(vec![("outcome", k("handled"))]);
    b.rec(Record::new("abort", P, "orders.base"));
    b.fx("reset_avoid_collisions", vec![("object", c(S))]);
    b.fx("set_to_default_flight_control_model", vec![("object", c(S))]);
    b.fx("stop_boost", vec![("object", c(S))]);
    b.fx("stop_moving", vec![("object", c(S))]);
    check(&b.t);
}

#[test]
fn blocking_delivery_without_debug_does_not_draw_and_a_duplicate_is_ignored() {
    let mut b = small_ship_until_long_move();
    b.fact(Uid::NULL, "debug.channel", bv(false));
    b.ev("block", "event_object_changed_zone", k("matched"), 0x1234, 0, vec![("object", c(S)), ("param", c(ZT))]);
    // With a pending interrupt, the engine does not re-evaluate its conditions.
    b.fact(Uid::NULL, "debug.channel", bv(true));
    b.ev("block", "event_object_changed_zone", k("ignored"), 0x2345, 0, vec![("object", c(S)), ("param", c(ZT))]);
    b.irun("block", 0x3456);
    b.roll(1061, 0.0);
    b.irend(vec![("outcome", k("block"))]);
    check(&b.t);
}

#[test]
fn unported_input_modes_refuse_before_any_draw_or_effect() {
    use x4mp_ai::orders::movegeneric::MoveGeneric;
    use x4mp_ai::replay::FactStore;
    use x4mp_ai::rng::Stream;
    use x4mp_ai::runtime::Wake;
    use x4mp_ai::world::{Answer, Cx, Effect, Io, Query, Stop, R};

    struct NoIo;
    impl Io for NoIo {
        fn roll(&mut self, _: Uid, _: u32, _: f32, _: bool) -> R<()> {
            panic!("draw before refusal")
        }
        fn rnd(&mut self, _: Uid, _: u32, _: f64) -> R<()> {
            panic!("range before refusal")
        }
        fn query(&mut self, _: Uid, _: u32, _: &Query, _: &mut Stream) -> R<Answer> {
            panic!("query before refusal")
        }
        fn effect(&mut self, _: Uid, _: u32, _: &Effect, _: &mut Stream) -> R<()> {
            panic!("effect before refusal")
        }
    }
    for (param, value) in [
        ("rotation", k("rotation")),
        ("lookat", Val::Pos(Pos::default())),
        ("relativemovement", bv(true)),
        ("stayatrelativemovement", bv(true)),
    ] {
        let mut frame = MoveGeneric::new(vec![("destination".into(), c(ZT)), (param.into(), value)], S);
        let facts = FactStore::default();
        let mut io = NoIo;
        let mut rng = Stream::from_state(0x1234);
        let result = frame.resume(&mut Cx { ent: P, facts: &facts, io: &mut io, rng: &mut rng }, Wake::Start);
        assert!(matches!(result, Err(Stop::Unsupported { script: "move.generic", .. })), "{param}: {result:?}");
        assert_eq!(rng.state(), 0x1234, "{param}");
    }
}

#[test]
fn global_restart_discards_the_same_frames_pending_block() {
    let mut b = small_ship_until_long_move();
    b.fact(Uid::NULL, "debug.channel", bv(false));
    b.ev("block", "event_object_changed_zone", k("matched"), 0x1234, 0, vec![("object", c(S)), ("param", c(ZT))]);
    b.ev(
        "global",
        "event_object_signalled",
        s("move.generic/inline1"),
        0x2345,
        0,
        vec![("object", c(S)), ("param", s("move.generic_reinitialize"))],
    );
    b.irun("handler", 0x3456);
    b.roll(103, 0.0);
    b.irend(vec![("outcome", k("abort")), ("label", k("check"))]);
    check(&b.t);
    // A fresh event after the restart must not be ignored as though the old block
    // interrupt were still pending. This deliberately corrupt trace must be rejected.
    b.ev("block", "event_object_changed_zone", k("ignored"), 0x4567, 0, vec![("object", c(S)), ("param", c(ZT))]);
    let rep = replay(&b.t);
    let m = rep.entities[&P.0].first_mismatch.as_deref().unwrap_or_default();
    assert!(m.contains("none is queued natively"), "{m}");
}

#[test]
fn competing_global_restarts_are_refused_without_claiming_equivalent_ordering() {
    let mut b = small_ship_until_long_move();
    for seed in [0x1234, 0x2345] {
        b.ev(
            "global",
            "event_object_signalled",
            s("move.generic/inline1"),
            seed,
            0,
            vec![("object", c(S)), ("param", s("move.generic_reinitialize"))],
        );
    }
    b.irun("handler", 0x3456);
    b.roll(103, 0.0);
    b.irend(vec![("outcome", k("abort")), ("label", k("check"))]);
    let rep = replay(&b.t);
    assert!(!rep.ok());
    let r = &rep.entities[&P.0];
    assert!(r.unsupported.as_deref().unwrap_or_default().contains("additional queued global"));
    assert!(r.first_mismatch.is_none(), "do not continue a partially modeled bracket: {r:?}");
}
