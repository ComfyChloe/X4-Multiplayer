#![allow(clippy::too_many_arguments, clippy::neg_cmp_op_on_partial_ord)] // test helpers mirror the engine comparisons
//! Offline replay tests of `order.fight.patrol` + `move.seekenemies` against hand-built traces.
//!
//! SYNTHETIC: no capture from a native X4 run exists yet. Each trace below is written by walking the
//! XML (base 9.00) line by line; the comments cite the line. Draw values are computed with the small
//! reference functions in this file (an independent transcription of the formulas in
//! reports/REWRITE-AI-ORDERS.md section 4), not with the crate's `rng` module. RNG states at run starts
//! are arbitrary, as in a real capture where other contexts draw between two runs.

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
    !(x >= c)
}
/// `<wait min max>` / time ranges: f64 difference, one flat float draw, f64 sum.
fn ref_time(s: &mut u64, min: f64, max: f64) -> f64 {
    min + ref_float(s, (max - min) as f32) as f64
}
/// Length ranges: f32 difference, one flat float draw, f64 sum rounded to f32.
fn ref_len(s: &mut u64, min: f32, max: f32) -> f32 {
    ((min as f64) + ref_float(s, max - min) as f64) as f32
}

// ---------- trace builder ----------

const P: Uid = Uid(0x100); // pilot (context entity)
const S: Uid = Uid(0x200); // ship
const Z: Uid = Uid(0x300); // job main zone
const X: Uid = Uid(0x400); // sector
const C: Uid = Uid(0x500); // cluster
const Z2: Uid = Uid(0x301); // second normal zone in X
const HW: Uid = Uid(0x302); // a highway zone in X
const O: OrderId = OrderId(0x51); // the Patrol default order

struct B {
    t: Trace,
    /// The entity's RNG as the engine would hold it (TLS+0x318) during the current run.
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

impl B {
    fn new() -> B {
        let mut t = Trace::new("handbuilt");
        // These scenarios model lib.target.selection as an engine run (its internals are opaque).
        t.header.push(("opaque".into(), "lib.target.selection".into()));
        B { t, rng: 0, now: 0.0 }
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
    fn run(&mut self, script: &str, t: f64, wake: &str, rng: u64) {
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
    fn rnd_len(&mut self, line: i64, min: f32, max: f32) -> f32 {
        let v = ref_len(&mut self.rng, min, max);
        self.rec(Record::new("rnd", P, "-").arg("line", Val::Int(line)).arg("v", Val::F64(v as f64)));
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
        // Engine-side draws inside the service advance the stream.
        for _ in 0..draws {
            self.rng = ref_step(self.rng);
        }
        self.rec(r);
    }
    fn yield_(&mut self, script: &str, kind: &str, args: Vec<(&str, Val)>) {
        let mut r = Record::new("yield", P, script).arg("kind", k(kind));
        for (a, v) in args {
            r = r.arg(a, v);
        }
        let r = r.arg("rng", Val::U64(self.rng));
        self.rec(r);
    }
    /// An opaque run of a not-ported script: its internals are unknown, only the exit state counts.
    fn opaque_run(
        &mut self,
        script: &str,
        t: f64,
        wake: &str,
        rng_in: u64,
        rng_out: u64,
        kind: &str,
        args: Vec<(&str, Val)>,
    ) {
        self.run(script, t, wake, rng_in);
        self.rng = rng_out;
        self.yield_(script, kind, args);
    }
    /// Delivery to the global handlers (conditions only, no draws).
    fn ev_global(&mut self, name: &str, verdict: Val, args: Vec<(&str, Val)>, nested: bool) {
        let mut r =
            Record::new("ev", P, name).arg("path", k("global")).arg("verdict", verdict).arg("t", Val::F64(self.now));
        if nested {
            r = r.arg("nested", Val::Bool(true));
        }
        for (a, v) in args {
            r = r.arg(a, v);
        }
        self.rec(r);
    }
    /// Delivery to the blocking action's interrupt (engine verdict).
    fn ev_block(&mut self, name: &str, verdict: &str, args: Vec<(&str, Val)>) {
        let mut r =
            Record::new("ev", P, name).arg("path", k("block")).arg("verdict", k(verdict)).arg("t", Val::F64(self.now));
        for (a, v) in args {
            r = r.arg(a, v);
        }
        self.rec(r);
    }
    /// Interrupt runner bracket in AIDirector::Move.
    fn irun(&mut self, script: &str, prog: &str, rng: u64) {
        self.rng = rng;
        self.rec(
            Record::new("irun", P, script).arg("prog", k(prog)).arg("t", Val::F64(self.now)).arg("rng", Val::U64(rng)),
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
}

/// World of the scenarios: an NPC job ship (ship_m) whose job main zone is Z in sector X.
fn world(b: &mut B) {
    b.fact(P, "ship", c(S));
    b.fact(P, "zone", c(Z));
    b.fact(P, "sector", c(X));
    b.fact(P, "isplayerowned", Val::Bool(false));
    b.fact(S, "class", k("ship_m"));
    b.fact(S, "zone", c(Z));
    b.fact(S, "sector", c(X));
    b.fact(S, "jobmainzone", c(Z));
    b.fact(S, "iscommandeered", Val::Bool(false));
    b.fact(S, "isjobship", Val::Bool(true));
    b.fact(S, "jobexpired", Val::Bool(false));
    b.fact(S, "size", Val::F32(40.0));
    b.fact(S, "safesize", Val::F32(80.0));
    b.fact(S, "maxradarrange", Val::F32(10000.0));
    b.fact(S, "order", Val::Order(O));
    b.fact(S, "defaultorder", Val::Order(O));
    b.fact(S, "isoperational", Val::Bool(true));
    b.ofact(O, "id", s("Patrol"));
    b.ofact(O, "isrunning", Val::Bool(true));
    for (z, size) in [(Z, 30000.0f32), (Z2, 24000.0)] {
        b.fact(z, "class", k("zone"));
        b.fact(z, "sector", c(X));
        b.fact(z, "cluster", c(C));
        b.fact(z, "isnormalzone", Val::Bool(true));
        b.fact(z, "size", Val::F32(size));
    }
    b.fact(HW, "class", k("highway"));
    b.fact(HW, "sector", c(X));
    b.fact(X, "class", k("sector"));
    b.fact(X, "cluster", c(C));
    b.fact(C, "class", k("cluster"));
    b.fact(Uid::NULL, "param.spacereservation.maxduration", Val::F64(1800.0));
}

fn targetclasses_default() -> Val {
    Val::List(
        ["class.ship_s", "class.ship_m", "class.ship_l", "class.ship_xl", "class.station"]
            .iter()
            .map(|x| k(x))
            .collect(),
    )
}

fn seek_call_params(b: &B, pz: Uid, pos: Pos) -> Vec<(&'static str, Val)> {
    let _ = b;
    vec![
        ("name", s("move.seekenemies")),
        ("param.destination", c(pz)),
        ("param.pos", Val::Pos(pos)),
        ("param.pursuetargets", Val::Bool(false)),
        ("param.pursuedistance", Val::F32(10000.0)),
        ("param.allowothertargets", Val::Bool(true)),
        ("param.targetpurposes", Val::List(vec![])),
        ("param.stayinspace", Val::Bool(false)),
        ("param.plunderbasket", Val::List(vec![])),
        ("param.internalorder", Val::Bool(true)),
        ("param.engageonlyonline", Val::Bool(false)),
        ("param.targetclasses", Val::List(vec![])),
        ("param.checkrelation", Val::Bool(true)),
        ("param.debugchance", Val::F32(0.0)),
    ]
}

fn targetsel_params() -> Vec<(&'static str, Val)> {
    vec![
        ("name", s("lib.target.selection")),
        ("param.primarytarget", Val::Null),
        ("param.secondarytargets", Val::Null),
        ("param.escort", Val::Null),
        ("param.pursuedistance", Val::F32(10000.0)),
        ("param.pursuetargets", Val::Bool(false)),
        ("param.list_objectstoignore", Val::List(vec![])),
        ("param.allowothertargets", Val::Bool(true)),
        ("param.checkrelation", Val::Bool(true)),
        ("param.targetpurposes", Val::List(vec![])),
        ("param.targetclasses", targetclasses_default()),
        ("param.engageonlyonline", Val::Bool(false)),
        ("param.radius", Val::Null),
        ("param.radiusanchorpos", Val::Null),
        ("param.radiusanchorspace", Val::Null),
        ("param.debugchance", Val::F32(0.0)),
    ]
}

/// One patrol leg from label find_position (L458) to the yield at the seekenemies call (L493).
/// `cp_draws`: engine-side draws inside create_position (unknown natively; exercised here).
fn leg_find_position(b: &mut B, pz: Uid, base: Pos, cp_draws: u64) -> Pos {
    // L484 create_position space=$patrolzone max=$patrolzone.size / 2
    let half = if pz == Z { 15000.0f32 } else { 12000.0 };
    b.q("create_position", vec![("space", c(pz)), ("max", Val::F32(half))], vec![("pos", Val::Pos(base))], cp_draws);
    // L485 set_value $y min=-2km max=2km
    let y = b.rnd_len(485, -2000.0, 2000.0);
    // L486 get_safe_pos radius=this.ship.size + 200m value=position.[$base_pos.x, $y, $base_pos.z]
    let want = Pos::new(base.x, y, base.z);
    let safe = Pos::new(base.x + 5.0, y, base.z); // the engine nudged it
    b.q(
        "get_safe_pos",
        vec![
            ("value", Val::Pos(want)),
            ("zone", c(pz)),
            ("radius", Val::F32(240.0)),
            ("ignored", c(S)),
            ("allowyaxis", Val::Bool(false)),
            ("ignoredangerousregions", Val::Bool(false)),
        ],
        vec![("pos", Val::Pos(safe))],
        0,
    );
    // L493 run_script move.seekenemies
    let ps = seek_call_params(b, pz, safe);
    b.fx("run_script", ps);
    b.yield_("order.fight.patrol", "call", vec![("script", s("move.seekenemies"))]);
    safe
}

/// move.seekenemies first run (Init..L187) for destination zone `pz`, pos `pos`.
/// `prev_res`: an existing reservation (index) to cancel at L162 (after a restart at moveto).
fn seek_first_run(b: &mut B, t: f64, wake: &str, rng: u64, pz: Uid, pos: Pos, prev_res: Option<i64>, idx: i64) -> Pos {
    b.run("move.seekenemies", t, wake, rng);
    if wake == "start" {
        // L90 create_position space=$destination.sector value=$pos object=$destination
        let in_sector = Pos::new(pos.x + 50000.0, pos.y, pos.z);
        b.q(
            "create_position_in",
            vec![("space", c(X)), ("value", Val::Pos(pos)), ("object", c(pz))],
            vec![("pos", Val::Pos(in_sector))],
            0,
        );
        // L110 start: destination X exists, is a sector, this.sector == X, not in a highway
    }
    // L157 moveto
    b.roll(159, 0.0);
    if let Some(old) = prev_res {
        b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(old)), ("zone", c(pz))]);
    }
    let value = if wake == "start" {
        Pos::new(pos.x + 50000.0, pos.y, pos.z)
    } else {
        // after a restart the reserved position of the previous attempt is the input
        Pos::new(pos.x + 50000.0, pos.y + 1.0, pos.z)
    };
    let reserved = Pos::new(value.x, value.y + 1.0, value.z);
    // L170 get_safe_pos ... sector=$destination radius=safesize/2 + spacereservation
    b.q(
        "get_safe_pos_reserve",
        vec![
            ("value", Val::Pos(value)),
            ("sector", c(X)),
            ("radius", Val::F32(40.0)),
            ("component", c(S)),
            ("duration", Val::F64(1800.0)),
        ],
        vec![("pos", Val::Pos(reserved)), ("index", Val::Int(idx)), ("zone", c(pz))],
        0,
    );
    b.roll(183, 0.0); // chance="@global.$spacereservationspam * 100" with the global unset -> 0
                      // L187 move_to
    b.fx(
        "move_to",
        vec![("object", c(S)), ("destination", c(X)), ("pos", Val::Pos(reserved)), ("travel", Val::Bool(false))],
    );
    b.yield_("move.seekenemies", "moveto", vec![]);
    reserved
}

/// Scenario A: range=class.zone job ship; recovery of the missing $space, two legs, a resupply
/// signal with a nested opaque handler, an enemy found by the gravidar interrupt, the Attack order,
/// a TargetInvalidHandler restart, an opaque AttackHandler, and the final abort by orders.base.
fn scenario_zone() -> Trace {
    let mut b = B::new();
    world(&mut b);
    b.rec(
        Record::new("start", P, "order.fight.patrol")
            .arg("param.range", k("class.zone"))
            .arg("param.debugchance", Val::F32(0.0)),
    );
    // <init> L89-90
    b.fx("set_command", vec![("command", k("command.patrol"))]);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);

    // Run 1: label start
    b.run("order.fight.patrol", 100.0, "start", 0x0123_4567_89ab_cdef);
    // L129: jobmainzone exists, not commandeered -> $startzone = Z. L158: $space is null:
    // L159 skipped ($space null), L164 skipped (has a main zone); L179 range zone -> $space = Z.
    b.roll(199, 0.0); // chance = $alarm? * $debugchance = 0
    b.fx("edit_order_param", vec![("order", Val::Order(O)), ("param", s("space")), ("value", c(Z))]); // L201
    b.fx("edit_order_param", vec![("order", Val::Order(O)), ("param", s("space")), ("value", c(Z))]); // L204
                                                                                                      // L279: the pilot is in Z -> no move.generic. L311:
    b.fx("set_order_syncpoint_reached", vec![("order", Val::Order(O))]);
    // L315 find_endzone, L317 wait min=1ms max=1s
    let w = b.rnd_time(317, 0.001, 1.0);
    let until = 100.0 + w;
    b.yield_("order.fight.patrol", "wait", vec![("until", Val::F64(until))]);
    // the edit is visible to later reads
    b.ofact(O, "$space", c(Z));

    // Run 2: timer
    let t2 = until + 0.01;
    b.run("order.fight.patrol", t2, "timer", 0x1111_2222_3333_4444);
    // L320 $space is a zone -> $endzone = Z; L398
    b.roll(398, 0.0);
    // L408 find_patrolzone: $patrolzone = $space; L426 no exit; L430
    b.roll(430, 0.0);
    // L433 same sector; L455 $inittime = t2; L458 find_position
    let pos1 = leg_find_position(&mut b, Z, Pos::new(1000.0, 0.0, -2000.0), 3);

    // Run 3: move.seekenemies starts
    let r1 = seek_first_run(&mut b, t2, "start", 0x5555_6666_7777_8888, Z, pos1, None, 7);

    // Run 4: arrived (no interrupt); L537 not quick (29 s); L545 lib.target.selection
    b.run("move.seekenemies", t2 + 29.0, "block", 0x9999_aaaa_bbbb_cccc);
    b.fx("run_script", targetsel_params());
    b.yield_("move.seekenemies", "call", vec![("script", s("lib.target.selection"))]);
    // Run 5: lib.target.selection (opaque): found nothing
    b.opaque_run(
        "lib.target.selection",
        t2 + 29.0,
        "start",
        0xdddd_eeee_ffff_0000,
        ref_step(ref_step(0xdddd_eeee_ffff_0000)),
        "return",
        vec![("target", Val::Null)],
    );
    // Run 6: back in move.seekenemies: L571 no enemy -> L639; L642 end
    b.run("move.seekenemies", t2 + 29.0, "return", 0x0f0f_0f0f_0f0f_0f0f);
    b.roll(639, 0.0);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(7)), ("zone", c(Z))]); // L645
    b.yield_("move.seekenemies", "return", vec![("target", Val::Null), ("list_targets", Val::List(vec![]))]);
    let _ = r1;
    // Run 7: back in Patrol: L509 wait min=1s max=2s
    b.run("order.fight.patrol", t2 + 29.0, "return", 0x1357_9bdf_2468_ace0);
    let w2 = b.rnd_time(509, 1.0, 2.0);
    let until2 = t2 + 29.0 + w2;
    b.yield_("order.fight.patrol", "wait", vec![("until", Val::F64(until2))]);

    // Run 8: L511 do_if player.age gt @$next_resupply_check chance=30 (rolled first).
    // Pick a run-start state whose roll passes, as a capture would show either way.
    let t8 = until2 + 0.02;
    let mut st = 0x2468_ace0_1357_9bdfu64;
    loop {
        let mut probe = st;
        if ref_chance(&mut probe, 30.0) {
            break;
        }
        st = st.wrapping_add(1);
    }
    b.run("order.fight.patrol", t8, "timer", st);
    assert!(b.roll(511, 30.0));
    b.roll(512, 0.0);
    b.fx(
        "signal_objects",
        vec![
            ("object", c(S)),
            ("param", s("resupply")),
            ("param2", Val::List(vec![Val::Bool(false)])),
            ("param3", Val::F32(0.0)),
        ],
    );
    // The signal is delivered synchronously, but only the conditions run now (no draws): the
    // ResupplyHandler (opaque) is queued and its actions run in the next Move pass.
    b.ev_global(
        "event_object_signalled",
        s("order.fight.patrol/ResupplyHandler"),
        vec![("object", c(S)), ("param", s("resupply")), ("param2", Val::List(vec![Val::Bool(false)]))],
        true,
    );
    // L514, L517 find_patrolzone -> Z again; L430
    b.roll(430, 0.0);
    // L458 find_position, second leg (create_position without engine draws this time)
    let pos2 = leg_find_position(&mut b, Z, Pos::new(-3000.0, 0.0, 4000.0), 0);

    // Next Move pass: the queued ResupplyHandler's actions (opaque: three draws, an effect),
    // then the main run of the top frame (move.seekenemies, start).
    b.irun("order.fight.patrol", "handler", 0x2222_2222_2222_2222);
    b.fx("create_order", vec![("id", s("Resupply")), ("object", c(S))]); // opaque: not compared
    b.rng = ref_step(ref_step(ref_step(0x2222_2222_2222_2222)));
    b.irend(vec![]);
    // Run 9: move.seekenemies starts the second leg
    let r2 = seek_first_run(&mut b, t8, "start", 0x3333_3333_3333_3333, Z, pos2, None, 9);

    // Event: gravidar scan while flying. Global handlers do not match; the move_to interrupt does.
    // $detected = [0x900, 0x901]; 0x901 is in another sector.
    b.fact(Uid(0x900), "class", k("ship_m"));
    b.fact(Uid(0x900), "sector", c(X));
    b.fact(Uid(0x900), "zone", c(Z));
    b.fact(Uid(0x901), "class", k("ship_s"));
    b.fact(Uid(0x901), "sector", c(Uid(0x401)));
    b.fact(S, "cansee", Val::List(vec![c(Uid(0x900)), c(Uid(0x901))]));
    b.now = t8 + 4.0;
    b.ev_global("event_gravidar_has_scanned", k("none"), vec![("object", c(S))], false);
    b.ev_block(
        "event_gravidar_has_scanned",
        "matched",
        vec![("object", c(S)), ("detected", Val::List(vec![c(Uid(0x900)), c(Uid(0x901))]))],
    );
    // Move: the move_to interrupt's actions (L392-533) run before the main run
    b.irun("move.seekenemies", "block", 0x4444_4444_4444_4444);
    b.roll(394, 0.0);
    b.roll(398, 0.0); // reverse loop: 0x901 first, other sector -> debug + remove
    b.roll(432, 0.0);
    b.roll(448, 0.0);
    b.irend(vec![("outcome", k("block"))]);
    // Run 10: move_to ended by the interrupt: L537 skipped ($interrupted), lib.target.selection
    b.run("move.seekenemies", t8 + 4.0, "block", 0x5555_5555_5555_5555);
    b.fx("run_script", targetsel_params());
    b.yield_("move.seekenemies", "call", vec![("script", s("lib.target.selection"))]);
    b.opaque_run(
        "lib.target.selection",
        t8 + 4.0,
        "start",
        0x6666_6666_6666_6666,
        0x7777_7777_7777_7777,
        "return",
        vec![("target", c(Uid(0x900)))],
    );
    // Run 11: L571 enemy can be attacked
    b.fact(Uid(0x900), "canbeattacked", Val::Bool(true));
    b.run("move.seekenemies", t8 + 4.0, "return", 0x8888_8888_8888_8888);
    b.roll(572, 0.0);
    // L581: order.$space (Z) == enemy zone (Z) -> no edit. L587 chance=0. L602 no commander.
    b.roll(587, 0.0);
    b.roll(615, 0.0);
    b.fx(
        "create_order",
        vec![
            ("id", s("Attack")),
            ("object", c(S)),
            ("immediate", Val::Bool(true)),
            ("param.primarytarget", c(Uid(0x900))),
            ("param.escort", Val::Null),
            ("param.pursuedistance", Val::F32(10000.0)),
            ("param.pursuetargets", Val::Bool(false)),
            ("param.allowothertargets", Val::Bool(true)),
            ("param.targetclasses", targetclasses_default()),
            ("param.disable", Val::Bool(false)),
            ("param.radius", Val::Null),
            ("param.radiusanchorpos", Val::Null),
            ("param.radiusanchorspace", Val::Null),
            ("param.internalorder", Val::Bool(true)),
            ("param.checkrelation", Val::Bool(true)),
            ("param.enforceradius", Val::Bool(false)),
            ("param.debugchance", Val::F32(0.0)),
        ],
    );
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(9)), ("zone", c(Z))]);
    b.yield_("move.seekenemies", "return", vec![("target", Val::Null), ("list_targets", Val::List(vec![]))]);
    let _ = r2;
    // Run 12: Patrol L509 wait
    b.run("order.fight.patrol", t8 + 4.0, "return", 0x9999_9999_9999_9999);
    let w3 = b.rnd_time(509, 1.0, 2.0);
    b.yield_("order.fight.patrol", "wait", vec![("until", Val::F64(t8 + 4.0 + w3))]);
    // An opaque library handler (AttackHandler of Patrol) matches an attack event; its actions run
    // in the next Move pass; no control change in Patrol.
    b.ev_global(
        "event_object_attacked",
        s("order.fight.patrol/AttackHandler"),
        vec![("object", c(S)), ("param", c(Uid(0x900)))],
        false,
    );
    b.irun("order.fight.patrol", "handler", 0xaaaa_aaaa_aaaa_aaaa);
    b.fx("create_order", vec![("id", s("Attack")), ("object", c(S))]); // opaque: not compared
    b.rng = ref_step(0xaaaa_aaaa_aaaa_aaaa);
    b.irend(vec![]);
    // orders.base sees the new order ready and aborts the default order (its own handler, opaque).
    b.ev_global("event_object_order_ready", s("orders.base/inline1"), vec![("object", c(S))], false);
    b.irun("orders.base", "orders.base", 0xbbbb_bbbb_bbbb_bbbb);
    b.irend(vec![]);
    b.rec(Record::new("abort", P, "loop"));
    b.t
}

#[test]
fn zone_patrol_replays_bit_exactly() {
    let t = scenario_zone();
    let rep = replay(&t);
    assert!(rep.ok(), "{}", rep.summary());
    let e = &rep.entities[&P.0];
    assert_eq!(e.runs, 11, "{}", rep.summary()); // runs 1-4, 6-9, 10, 11, 12 (5 and 10a are opaque)
    assert_eq!(e.opaque_runs, 2);
    assert_eq!(e.deliveries, 5);
    assert_eq!(e.interrupt_runs, 4);
    assert_eq!(e.yields_checked, 11);
    assert_eq!(e.rng_checked, 12); // 11 yields + 1 native interrupt run
}

#[test]
fn trace_text_round_trip_replays() {
    let t = scenario_zone();
    let text = t.to_string();
    let back = Trace::parse(&text).expect("parse");
    let rep = replay(&back);
    assert!(rep.ok(), "{}", rep.summary());
}

#[test]
fn a_changed_draw_is_reported_at_its_line() {
    let mut t = scenario_zone();
    // Corrupt the first rnd value (L317 wait): replay must stop exactly there.
    let i = t.records.iter().position(|r| r.tag == "rnd").expect("rnd");
    t.records[i].args[1].1 = Val::F64(0.5);
    for (n, r) in t.records.iter_mut().enumerate() {
        r.line = n + 2;
    }
    let rep = replay(&t);
    let m = rep.entities[&P.0].first_mismatch.clone().expect("mismatch");
    assert!(m.contains(&format!("trace line {}", i + 2)) && m.contains("rnd"), "{m}");
}

#[test]
fn a_missing_debug_roll_is_caught_by_the_rng_state() {
    // Remove the L398 roll record and fix nothing else: the roll comparison fails at the next roll.
    let mut t = scenario_zone();
    let i = t.records.iter().position(|r| r.tag == "roll" && r.get("line") == Some(&Val::Int(398))).expect("roll 398");
    t.records.remove(i);
    let rep = replay(&t);
    assert!(rep.entities[&P.0].first_mismatch.is_some());
}

#[test]
fn an_extra_engine_draw_in_a_query_is_caught_at_the_yield() {
    // Claim create_position drew 4 times instead of 3: the stream is then one step ahead.
    let mut t = scenario_zone();
    let i = t.records.iter().position(|r| r.tag == "q" && r.name == "create_position").expect("cp");
    let d = t.records[i].results.iter_mut().find(|(k, _)| k == "draws").expect("draws");
    d.1 = Val::Int(4);
    let rep = replay(&t);
    let m = rep.entities[&P.0].first_mismatch.clone().expect("mismatch");
    assert!(m.contains("rnd") || m.contains("RNG"), "{m}");
}

#[test]
fn stayhidden_is_refused_before_any_side_effect() {
    let mut b = B::new();
    world(&mut b);
    b.rec(
        Record::new("start", P, "order.fight.patrol").arg("param.space", c(Z)).arg("param.stayhidden", Val::Bool(true)),
    );
    b.fx("set_command", vec![("command", k("command.patrol"))]);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    b.run("order.fight.patrol", 10.0, "start", 1);
    let rep = replay(&b.t);
    let u = rep.entities[&P.0].unsupported.clone().expect("unsupported");
    assert!(u.contains("L221") && u.contains("stayhidden"), "{u}");
}

/// Scenario B: range=class.sector; find_zone with the 40 km filter, get_global_path with a highway
/// in the route (removed with a debug roll), the route walked zone by zone, then a fresh endzone.
fn scenario_sector() -> Trace {
    let mut b = B::new();
    world(&mut b);
    b.rec(
        Record::new("start", P, "order.fight.patrol")
            .arg("param.range", k("class.sector"))
            .arg("param.debugchance", Val::F32(0.0)),
    );
    b.fx("set_command", vec![("command", k("command.patrol"))]);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    b.run("order.fight.patrol", 50.0, "start", 0xfeed_beef_0000_0001);
    // $space null -> range sector -> $space = Z.sector = X
    b.roll(199, 0.0);
    b.fx("edit_order_param", vec![("order", Val::Order(O)), ("param", s("space")), ("value", c(X))]);
    b.fx("edit_order_param", vec![("order", Val::Order(O)), ("param", s("space")), ("value", c(X))]);
    b.fx("set_order_syncpoint_reached", vec![("order", Val::Order(O))]);
    let w = b.rnd_time(317, 0.001, 1.0);
    b.yield_("order.fight.patrol", "wait", vec![("until", Val::F64(50.0 + w))]);
    // Run 2: L330 find_zone normalzone match_distance min 40km
    b.run("order.fight.patrol", 51.0, "timer", 0xfeed_beef_0000_0002);
    b.q(
        "find_zone",
        vec![
            ("space", c(X)),
            ("normalzone", Val::Bool(true)),
            ("mindist_object", c(S)),
            ("mindist", Val::F32(40000.0)),
        ],
        vec![("zone", c(Z2))],
        0,
    );
    // L379 get_global_path start=ship end=Z2 -> [HW, Z2]: HW is not a normal zone
    b.q("get_global_path", vec![("start", c(S)), ("end", c(Z2))], vec![("path", Val::List(vec![c(HW), c(Z2)]))], 0);
    b.roll(391, 0.0); // HW removed (reverse loop reaches it second; Z2 is kept silently)
    b.roll(398, 0.0);
    // find_patrolzone: route [Z2] -> $patrolzone = Z2
    b.roll(430, 0.0);
    let pos = leg_find_position(&mut b, Z2, Pos::new(10.0, 0.0, 20.0), 1);
    let _ = seek_first_run(&mut b, 51.0, "start", 0xfeed_beef_0000_0003, Z2, pos, None, 3);
    // A target-invalid event: seekenemies' native TargetInvalidHandler aborts called scripts (none)
    // and restarts move.seekenemies at its current label (moveto).
    b.now = 52.0;
    b.ev_global(
        "event_object_target_invalid",
        s("move.seekenemies/TargetInvalidHandler"),
        vec![("object", c(S))],
        false,
    );
    b.irun("move.seekenemies", "handler", 0xfeed_beef_0000_0004);
    b.roll(14, 0.0);
    b.roll(15, 0.0);
    b.irend(vec![("outcome", k("abort")), ("label", Val::Null)]);
    let _ = seek_first_run(&mut b, 52.0, "restart", 0xfeed_beef_0000_0005, Z2, pos, Some(3), 4);
    // arrival, nothing found
    b.run("move.seekenemies", 70.0, "block", 0xfeed_beef_0000_0006);
    b.fx("run_script", targetsel_params());
    b.yield_("move.seekenemies", "call", vec![("script", s("lib.target.selection"))]);
    b.opaque_run(
        "lib.target.selection",
        70.0,
        "start",
        0xfeed_beef_0000_0007,
        0xfeed_beef_0000_0008,
        "return",
        vec![("target", Val::Null)],
    );
    b.run("move.seekenemies", 70.0, "return", 0xfeed_beef_0000_0009);
    b.roll(639, 0.0);
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(4)), ("zone", c(Z2))]);
    b.yield_("move.seekenemies", "return", vec![("target", Val::Null), ("list_targets", Val::List(vec![]))]);
    b.run("order.fight.patrol", 70.0, "return", 0xfeed_beef_0000_000a);
    let w = b.rnd_time(509, 1.0, 2.0);
    b.yield_("order.fight.patrol", "wait", vec![("until", Val::F64(70.0 + w))]);
    // L511: choose a state whose roll fails; then find_patrolzone: route empty, $patrolzone == $endzone
    // -> L422 resume find_endzone -> L317 wait again.
    let mut st = 0xfeed_beef_0000_000bu64;
    loop {
        let mut probe = st;
        if !ref_chance(&mut probe, 30.0) {
            break;
        }
        st = st.wrapping_add(1);
    }
    b.run("order.fight.patrol", 72.0, "timer", st);
    assert!(!b.roll(511, 30.0));
    let w = b.rnd_time(317, 0.001, 1.0);
    b.yield_("order.fight.patrol", "wait", vec![("until", Val::F64(72.0 + w))]);
    b.t
}

#[test]
fn sector_patrol_replays_bit_exactly() {
    let t = scenario_sector();
    let rep = replay(&t);
    assert!(rep.ok(), "{}", rep.summary());
    let e = &rep.entities[&P.0];
    assert_eq!(e.deliveries, 1);
    assert_eq!(e.interrupt_runs, 1);
    assert!(e.yields_checked >= 8, "{}", rep.summary());
}

#[test]
fn checked_in_example_trace_replays() {
    let text = include_str!("data/patrol_zone.x4aitrace");
    let t = Trace::parse(text).expect("parse");
    let rep = replay(&t);
    assert!(rep.ok(), "{}", rep.summary());
    // The checked-in file is the text form of scenario A.
    let mut fresh = Trace::parse(&scenario_zone().to_string()).expect("parse");
    let mut file = t;
    for r in fresh.records.iter_mut().chain(file.records.iter_mut()) {
        r.line = 0;
    }
    assert_eq!(
        file.records, fresh.records,
        "regenerate tests/data/patrol_zone.x4aitrace (see test write_example_trace)"
    );
}

/// Writes the example file when X4MP_AI_WRITE_EXAMPLE=1 (documentation of the format).
#[test]
fn write_example_trace() {
    if std::env::var("X4MP_AI_WRITE_EXAMPLE").ok().as_deref() != Some("1") {
        return;
    }
    let mut text = String::from(
        "# X4AITRACE v1 example: scenario A of tests/patrol_replay.rs (SYNTHETIC, hand-built from the XML;\n\
         # not a native capture). order.fight.patrol, range=class.zone job ship, two legs, nested opaque\n\
         # ResupplyHandler delivery + its interrupt run, gravidar move_to interrupt, Attack order, opaque\n\
         # AttackHandler, orders.base abort.\n",
    );
    text.push_str(&scenario_zone().to_string());
    std::fs::write(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/data/patrol_zone.x4aitrace"), text).expect("write");
}

/// Scenario C: like the first leg of scenario A, but lib.target.selection runs natively. Gravidar
/// reports three contacts: 0x910 docked (skipped, L103), 0x911 hostile ship_m at 6 km (no distance
/// bonus), 0x912 hostile ship_s at 1 km (distance bonus 9). Priorities (f32, XML order):
/// 0x911: 0 - (-0.6)*5 + 10*120/(4000+1000) = 3 + 0.24 = 3.24;
/// 0x912: (10 - (1000-500)/500) - (-0.2)*5 + 10*80/(1500+500) = 9 + 1 + 0.4 = 10.4 -> selected.
fn scenario_native_target_selection() -> Trace {
    let mut b = B::new();
    b.t.header.retain(|(k, _)| k != "opaque");
    world(&mut b);
    b.fact(P, "trueowner", k("faction.argon"));
    b.fact(S, "iscapitalship", Val::Bool(false));
    let hostiles = [Uid(0x910), Uid(0x911), Uid(0x912)];
    b.fact(P, "mayattack", Val::List(hostiles.iter().map(|u| c(*u)).collect()));
    for (u, cls, dist, rel, dps, hull, shield, docked) in [
        (Uid(0x910), "ship_m", 2000.0f32, -0.5f32, 50.0f32, 3000.0f64, 1000.0f64, true),
        (Uid(0x911), "ship_m", 6000.0, -0.6, 120.0, 4000.0, 1000.0, false),
        (Uid(0x912), "ship_s", 1000.0, -0.2, 80.0, 1500.0, 500.0, false),
    ] {
        b.fact(u, "class", k(cls));
        b.fact(u, "sector", c(X));
        b.fact(u, "zone", c(Z));
        b.fact(u, "canbeattacked", Val::Bool(true));
        b.fact(u, "trueowner", k("faction.xenon"));
        b.fact(u, "dock", Val::Bool(docked));
        b.fact(u, "distanceto", Val::List(vec![c(S), Val::F32(dist)]));
        b.fact(u, "relationto", Val::List(vec![c(S), Val::F32(rel)]));
        b.fact(u, "dps.all", Val::F32(dps));
        b.fact(u, "hull", Val::F64(hull));
        b.fact(u, "shield", Val::F64(shield));
    }
    b.rec(
        Record::new("start", P, "order.fight.patrol").arg("param.space", c(Z)).arg("param.debugchance", Val::F32(0.0)),
    );
    b.fx("set_command", vec![("command", k("command.patrol"))]);
    b.fx("set_command_action", vec![("commandaction", k("commandaction.calculating"))]);
    // $space given: no recovery, no edits; L311; L317
    b.run("order.fight.patrol", 10.0, "start", 0x00c0_ffee_0000_0001);
    b.fx("set_order_syncpoint_reached", vec![("order", Val::Order(O))]);
    let w = b.rnd_time(317, 0.001, 1.0);
    b.yield_("order.fight.patrol", "wait", vec![("until", Val::F64(10.0 + w))]);
    b.run("order.fight.patrol", 11.0, "timer", 0x00c0_ffee_0000_0002);
    b.roll(398, 0.0);
    b.roll(430, 0.0);
    let pos = leg_find_position(&mut b, Z, Pos::new(0.0, 0.0, 0.0), 3);
    let _ = seek_first_run(&mut b, 11.0, "start", 0x00c0_ffee_0000_0003, Z, pos, None, 5);
    b.run("move.seekenemies", 40.0, "block", 0x00c0_ffee_0000_0004);
    b.fx("run_script", targetsel_params());
    b.yield_("move.seekenemies", "call", vec![("script", s("lib.target.selection"))]);
    // native lib.target.selection
    b.run("lib.target.selection", 40.0, "start", 0x00c0_ffee_0000_0005);
    b.roll(67, 0.0);
    b.roll(80, 0.0);
    b.q(
        "find_gravidar_contact",
        vec![
            ("object", c(S)),
            ("class", targetclasses_default()),
            ("maybeattackedby", c(S)),
            ("excluded", Val::List(vec![])),
        ],
        vec![("contacts", Val::List(hostiles.iter().map(|u| c(*u)).collect()))],
        0,
    );
    b.roll(103, 0.0); // 0x910 docked
    b.roll(184, 0.0); // 0x911: in sector, chance="0"; 6 km: no L233
    b.roll(251, 0.0);
    b.roll(286, 0.0); // first valid: new highest
    b.roll(184, 0.0); // 0x912
    b.roll(233, 0.0);
    b.roll(251, 0.0);
    b.roll(286, 0.0); // 10.4 > 3.24
    b.roll(298, 0.0); // changed target
    b.yield_("lib.target.selection", "return", vec![("target", c(Uid(0x912)))]);
    // seek attacks 0x912
    b.run("move.seekenemies", 40.0, "return", 0x00c0_ffee_0000_0006);
    b.roll(572, 0.0);
    b.roll(587, 0.0);
    b.roll(615, 0.0);
    b.fx(
        "create_order",
        vec![
            ("id", s("Attack")),
            ("object", c(S)),
            ("immediate", Val::Bool(true)),
            ("param.primarytarget", c(Uid(0x912))),
            ("param.escort", Val::Null),
            ("param.pursuedistance", Val::F32(10000.0)),
            ("param.pursuetargets", Val::Bool(false)),
            ("param.allowothertargets", Val::Bool(true)),
            ("param.targetclasses", targetclasses_default()),
            ("param.disable", Val::Bool(false)),
            ("param.radius", Val::Null),
            ("param.radiusanchorpos", Val::Null),
            ("param.radiusanchorspace", Val::Null),
            ("param.internalorder", Val::Bool(true)),
            ("param.checkrelation", Val::Bool(true)),
            ("param.enforceradius", Val::Bool(false)),
            ("param.debugchance", Val::F32(0.0)),
        ],
    );
    b.fx("cancel_space_reservation", vec![("component", c(S)), ("index", Val::Int(5)), ("zone", c(Z))]);
    b.yield_("move.seekenemies", "return", vec![("target", Val::Null), ("list_targets", Val::List(vec![]))]);
    b.t
}

#[test]
fn native_target_selection_scores_and_draws_like_the_script() {
    // The expected selection, by hand in f32 as written in the XML (independent of the crate).
    let p911: f32 = 0.0 - (-0.6f32) * 5.0 + 10.0 * 120.0 / ((4000.0f64 + 1000.0) as f32);
    let p912: f32 = (10.0 - (1000.0f32 - 500.0) / 500.0) - (-0.2f32) * 5.0 + 10.0 * 80.0 / ((1500.0f64 + 500.0) as f32);
    assert!(p912 > p911);
    let t = scenario_native_target_selection();
    let rep = replay(&t);
    assert!(rep.ok(), "{}", rep.summary());
    assert_eq!(rep.entities[&P.0].opaque_runs, 0);
}

#[test]
fn native_target_selection_refuses_a_capital_target_for_a_fighter() {
    let mut t = scenario_native_target_selection();
    // make the winner a capital ship: the big-target reselection is not ported
    let i = t.records.iter().position(|r| r.tag == "start").unwrap();
    t.records.insert(i, Record::new("fact", Uid(0x912), "iscapitalship").arg("v", Val::Bool(true)));
    let rep = replay(&t);
    let u = rep.entities[&P.0].unsupported.clone().expect("unsupported");
    assert!(u.contains("lib.target.selection L306"), "{u}");
}
