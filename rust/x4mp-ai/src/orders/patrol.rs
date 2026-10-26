//! `order.fight.patrol` (order id `Patrol`, script version 10), the default order of 138 jobs.
//!
//! Ported: the whole attention block except the `stayhidden` (ambush) and `organizesubordinates`
//! (carrier gate defence) branches, which stop with [`Stop::Unsupported`] before any side effect;
//! the inline handler and `SectorChangeHandler`. Called scripts: `move.seekenemies` (native),
//! `move.generic` (opaque). Line numbers: `aiscripts/order.fight.patrol.xml` (base 9.00).

use super::{p_bool, p_f32, p_f64, p_list, param, seekenemies::SeekEnemies};
use crate::rng::NumKind;
use crate::runtime::{Event, Frame, Outcome, RetVals, Wake, Yield};
use crate::val::{OrderId, Pos, Time, Uid, Val};
use crate::world::{Class, Cx, Effect, Facts, Prop, Query, Stop, R};

/// `$range`: the job-level patrol range.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Range {
    Zone,
    Sector,
    Cluster,
}

impl Range {
    fn parse(v: &Val) -> R<Option<Range>> {
        match v {
            Val::Null => Ok(None),
            Val::Key(k) => match k.as_str() {
                "class.zone" => Ok(Some(Range::Zone)),
                "class.sector" => Ok(Some(Range::Sector)),
                "class.cluster" => Ok(Some(Range::Cluster)),
                _ => Err(Stop::Mismatch(format!("param range: unexpected {k}"))),
            },
            other => Err(Stop::Mismatch(format!("param range: unexpected {other}"))),
        }
    }
    fn val(r: Option<Range>) -> Val {
        match r {
            None => Val::Null,
            Some(Range::Zone) => Val::key("class.zone"),
            Some(Range::Sector) => Val::key("class.sector"),
            Some(Range::Cluster) => Val::key("class.cluster"),
        }
    }
}

/// Order parameters (`<params>`, defaults as in the XML).
#[derive(Clone, Debug, PartialEq)]
pub struct PatrolParams {
    pub space: Uid,
    pub range: Option<Range>,
    pub pursuetargets: bool,
    pub pursuedistance: f32,
    pub includeroute: bool,
    pub allowothertargets: bool,
    pub targetpurposes: Vec<Val>,
    pub stayinspace: bool,
    pub usebasket: bool,
    pub timeout: f64,
    pub engageonlyonline: bool,
    pub targetclasses: Vec<Val>,
    pub stayhidden: bool,
    pub organizesubordinates: bool,
    pub checkrelation: bool,
    pub debugchance: f32,
}

impl PatrolParams {
    /// From order parameters; `pursuedistance` defaults to `[this.ship.maxradarrange, 1km].max`.
    pub fn from_params(ps: &[(String, Val)], ship_max_radar: f32) -> R<PatrolParams> {
        Ok(PatrolParams {
            space: match param(ps, "space") {
                None => Uid::NULL,
                Some(v) => v.as_uid().ok_or_else(|| Stop::Mismatch(format!("param space: {v}")))?,
            },
            range: Range::parse(&param(ps, "range").unwrap_or(Val::Null))?,
            pursuetargets: p_bool(ps, "pursuetargets", false)?,
            pursuedistance: p_f32(ps, "pursuedistance", if ship_max_radar > 1000.0 { ship_max_radar } else { 1000.0 })?,
            includeroute: p_bool(ps, "includeroute", true)?,
            allowothertargets: p_bool(ps, "allowothertargets", true)?,
            targetpurposes: p_list(ps, "targetpurposes")?,
            stayinspace: p_bool(ps, "stayinspace", false)?,
            usebasket: p_bool(ps, "usebasket", false)?,
            timeout: p_f64(ps, "timeout", 0.0)?,
            engageonlyonline: p_bool(ps, "engageonlyonline", false)?,
            targetclasses: p_list(ps, "targetclasses")?,
            stayhidden: p_bool(ps, "stayhidden", false)?,
            organizesubordinates: p_bool(ps, "organizesubordinates", false)?,
            checkrelation: p_bool(ps, "checkrelation", true)?,
            debugchance: p_f32(ps, "debugchance", 0.0)?,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Label {
    Start,
    FindEndzone,
    FindPatrolzone,
    FindPosition,
    Patrol,
    Finish,
}

impl Label {
    fn parse(s: &str) -> R<Label> {
        Ok(match s {
            "start" => Label::Start,
            "find_endzone" => Label::FindEndzone,
            "find_patrolzone" => Label::FindPatrolzone,
            "find_position" => Label::FindPosition,
            "patrol" => Label::Patrol,
            "finish" => Label::Finish,
            _ => return Err(Stop::Mismatch(format!("order.fight.patrol has no label {s:?}"))),
        })
    }
    fn pc(self) -> Pc {
        match self {
            Label::Start => Pc::Start,
            Label::FindEndzone => Pc::FindEndzone,
            Label::FindPatrolzone => Pc::FindPatrolzone,
            Label::FindPosition => Pc::FindPosition,
            Label::Patrol => Pc::Patrol,
            Label::Finish => Pc::Finish,
        }
    }
}

/// Program counter: labels and continuations after blocking actions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Pc {
    Start,
    AfterMoveToSpace,
    FindEndzone,
    AfterEndzoneWait,
    FindPatrolzone,
    AfterMoveToPatrolzone,
    FindPosition,
    AfterSafetyWait,
    Patrol,
    AfterSeek,
    AfterPatrolWait,
    Finish,
    Done,
}

/// The script instance: parameters (also script variables) plus the other variables.
#[derive(Clone, Debug)]
pub struct Patrol {
    pub p: PatrolParams,
    ship: Uid,
    pc: Pc,
    last_label: Label,
    capship: bool,
    /// `$route`: null or a list.
    route: Option<Vec<Uid>>,
    endzone: Uid,
    /// `$patrolzone` (undefined until first set).
    patrolzone: Option<Uid>,
    /// `$inittime`.
    inittime: Option<Time>,
    pos: Pos,
    next_resupply_check: Option<Time>,
    /// Pending native callee, handed to the runtime on `Yield::Call`.
    callee: Option<Frame>,
}

const SCRIPT: &str = "order.fight.patrol";

fn unsupported(line: u32, what: &'static str) -> Stop {
    Stop::Unsupported { script: SCRIPT, line, what }
}

fn ret_bool(v: &RetVals) -> bool {
    // `<return value=..>` arrives as the unnamed value `v`.
    v.iter()
        .find(|(k, _)| k == "v")
        .map(|(_, v)| !matches!(v, Val::Null | Val::Bool(false) | Val::Int(0)))
        .unwrap_or(false)
}

impl Patrol {
    pub const NAME: &'static str = SCRIPT;
    pub const HANDLERS: &'static [&'static str] = &[
        "SectorChangeHandler",
        "AttackHandler",
        "MissileLockHandler",
        "ScannedHandler",
        "InspectedHandler",
        "FoundAbandonedHandler",
        "FoundLockboxHandler",
        "ResupplyHandler",
        "TideHandler",
        "inline1",
    ];

    pub fn new(p: PatrolParams, ship: Uid) -> Patrol {
        Patrol {
            p,
            ship,
            pc: Pc::Start,
            last_label: Label::Start,
            capship: false,
            route: None,
            endzone: Uid::NULL,
            patrolzone: None,
            inittime: None,
            pos: Pos::default(),
            next_resupply_check: None,
            callee: None,
        }
    }

    /// `<init>` (L88-91), run when the script is started.
    pub fn init(&mut self, cx: &mut Cx<'_>) -> R<()> {
        cx.fx(89, Effect::SetCommand { command: "command.patrol" })?;
        cx.fx(90, Effect::SetCommandAction { action: "commandaction.calculating" })
    }

    pub fn take_callee(&mut self) -> Option<Frame> {
        self.callee.take()
    }

    pub fn restart_at(&mut self, label: Option<&str>) -> R<()> {
        let l = match label {
            None => self.last_label,
            Some(s) => Label::parse(s)?,
        };
        self.pc = l.pc();
        Ok(())
    }

    fn expired_or_timed_out(&self, f: &dyn Facts, need_inittime: bool) -> bool {
        // (this.assignedcontrolled.jobexpired and this.assignedcontrolled.order.id != 'MoveDie')
        //   or ($timeout and [$inittime? and] (player.age ge $inittime + $timeout))
        let ship = self.ship;
        let expired = f.flag(ship, Prop::JobExpired) && f.order_kind(f.order_of(ship)) != "MoveDie";
        if expired {
            return true;
        }
        if self.p.timeout != 0.0 {
            match self.inittime {
                Some(t0) => f.now() >= t0 + self.p.timeout,
                // L461 reads $inittime without '?': undefined is null (0 in arithmetic).
                None => !need_inittime && f.now() >= self.p.timeout,
            }
        } else {
            false
        }
    }

    fn wait_range(&mut self, cx: &mut Cx<'_>, line: u32, min: f64, max: f64, then: Pc) -> R<Yield> {
        let d = cx.range(line, NumKind::F64, min, max)?;
        self.pc = then;
        Ok(Yield::Wait { until: Some(cx.now() + d) })
    }

    fn call_move_generic(&mut self, cx: &mut Cx<'_>, line: u32, dest: Uid, then: Pc) -> R<Yield> {
        let params = vec![
            ("destination", Val::uid(dest)),
            ("endintargetzone", Val::Bool(true)),
            ("activepatrol", Val::Bool(self.p.includeroute)),
            ("targetclasses", Val::List(self.p.targetclasses.clone())),
            ("debugchance", Val::F32(self.p.debugchance)),
        ];
        cx.fx(line, Effect::RunScript { name: "move.generic", params: params.clone() })?;
        self.pc = then;
        Ok(Yield::Call { script: "move.generic", params })
    }

    pub fn resume(&mut self, cx: &mut Cx<'_>, wake: Wake) -> R<Yield> {
        let ship = self.ship;
        // Entry: check that the wake fits the continuation.
        let mut movesuccess = false;
        match (&wake, self.pc) {
            (Wake::Start, Pc::Start) | (Wake::Restart, _) => {}
            (Wake::Returned(v), Pc::AfterMoveToSpace | Pc::AfterMoveToPatrolzone) => movesuccess = ret_bool(v),
            (Wake::Returned(_), Pc::AfterSeek) => {}
            (Wake::Timer, Pc::AfterEndzoneWait | Pc::AfterSafetyWait | Pc::AfterPatrolWait) => {}
            _ => {
                return Err(Stop::Mismatch(format!(
                    "{SCRIPT}: wake {} does not fit continuation {:?}",
                    wake.name(),
                    self.pc
                )))
            }
        }
        loop {
            let f = cx.facts;
            match self.pc {
                Pc::Start => {
                    self.last_label = Label::Start; // L125
                                                    // L127
                    self.capship = f.is_class(ship, Class::ShipL) || f.is_class(ship, Class::ShipXL);
                    // L129-156: $startzone
                    let jobzone = f.uid_of(ship, Prop::JobMainZone);
                    let this_zone = f.uid_of(cx.ent, Prop::Zone);
                    let space = self.p.space;
                    let startzone = if f.exists(jobzone) && !f.flag(ship, Prop::IsCommandeered) {
                        jobzone
                    } else if f.is_class(space, Class::Zone) {
                        space
                    } else if f.exists(space) {
                        let sector = f.uid_of(space, Prop::Sector);
                        let eval = if sector.is_null() { space } else { sector };
                        // L141, L143
                        let mut z = cx.zone_query(
                            141,
                            Query::FindZone {
                                space: eval,
                                normalzone: true,
                                knownto: Val::Null,
                                min_distance: None,
                                not_highway: false,
                            },
                        )?;
                        if !cx.facts.exists(z) {
                            z = cx.zone_query(
                                143,
                                Query::FindZone {
                                    space: eval,
                                    normalzone: false,
                                    knownto: Val::Null,
                                    min_distance: None,
                                    not_highway: true,
                                },
                            )?;
                        }
                        z
                    } else if !f.is_class(this_zone, Class::Highway) {
                        this_zone
                    } else {
                        f.uid_of(this_zone, Prop::Destination)
                    };
                    let f = cx.facts;
                    // L158: recover a missing $space
                    if !f.exists(self.p.space) {
                        let mut alarm = false;
                        if !self.p.space.is_null() {
                            cx.debug(160, self.p.debugchance)?;
                            alarm = true;
                        }
                        if cx.facts.flag(ship, Prop::IsJobShip) && cx.facts.uid_of(ship, Prop::JobMainZone).is_null() {
                            cx.debug(165, self.p.debugchance)?;
                            alarm = true;
                        }
                        let f = cx.facts;
                        if !f.exists(startzone) || f.is_class(startzone, Class::Highway) {
                            // L170-175: error output only (filter="error", no chance)
                            self.pc = Pc::Finish;
                            continue;
                        }
                        self.p.space = match self.p.range {
                            Some(Range::Zone) => startzone,
                            Some(Range::Sector) => f.uid_of(startzone, Prop::Sector),
                            Some(Range::Cluster) => f.uid_of(startzone, Prop::Cluster),
                            None => f.uid_of(startzone, Prop::Sector),
                        };
                        let alarm_chance = if alarm { self.p.debugchance } else { 0.0 };
                        if cx.facts.exists(self.p.space) {
                            cx.debug(199, alarm_chance)?;
                            let f = cx.facts;
                            let def = f.default_order_of(ship);
                            if f.order_kind(def) == "Patrol" {
                                cx.fx(
                                    201,
                                    Effect::EditOrderParam {
                                        order: def,
                                        param: "space",
                                        value: Val::uid(self.p.space),
                                    },
                                )?;
                            }
                            let cur = cx.facts.order_of(ship);
                            if cx.facts.order_kind(cur) == "Patrol" {
                                cx.fx(
                                    204,
                                    Effect::EditOrderParam {
                                        order: cur,
                                        param: "space",
                                        value: Val::uid(self.p.space),
                                    },
                                )?;
                            }
                        } else {
                            cx.debug(209, alarm_chance)?;
                            self.pc = Pc::Finish;
                            continue;
                        }
                    }
                    if self.p.organizesubordinates {
                        return Err(unsupported(215, "organizesubordinates (OrganizeSubordinates_Defences)"));
                    }
                    if self.p.stayhidden {
                        return Err(unsupported(221, "stayhidden (gravidar obscuring positions)"));
                    }
                    // L279: if not in $space, go there with move.generic
                    if !cx.facts.has_context(cx.ent, self.p.space) {
                        return self.call_move_generic(cx, 280, self.p.space, Pc::AfterMoveToSpace);
                    }
                    self.pc = Pc::AfterMoveToSpace;
                    movesuccess = true; // no move needed: the do_if at L287 is inside L279's block
                    continue;
                }
                Pc::AfterMoveToSpace => {
                    if !movesuccess {
                        // L287-293
                        let cur = cx.facts.order_of(ship);
                        if cx.facts.order_running(cur) {
                            cx.fx(289, Effect::OrderFailed { order: cur, text: "{1045, 101}" })?;
                        }
                        cx.debug(291, self.p.debugchance)?;
                        self.pc = Pc::Finish;
                        continue;
                    }
                    // L296-307: stayhidden only (refused above). L309-312:
                    let cur = cx.facts.order_of(ship);
                    if !cur.is_null() {
                        cx.fx(311, Effect::SyncpointReached { order: cur })?;
                    }
                    self.pc = Pc::FindEndzone;
                }
                Pc::FindEndzone => {
                    self.last_label = Label::FindEndzone; // L315
                    return self.wait_range(cx, 317, 0.001, 1.0, Pc::AfterEndzoneWait);
                }
                Pc::AfterEndzoneWait => {
                    self.route = None; // L319
                    let f = cx.facts;
                    if f.is_class(self.p.space, Class::Zone) {
                        self.endzone = self.p.space; // L321
                    } else {
                        let player = f.flag(cx.ent, Prop::IsPlayerOwned);
                        let knownto = if player { f.get(cx.ent, Prop::TrueOwner) } else { Val::Null };
                        let (l1, l2) = if player { (325, 338) } else { (330, 341) };
                        self.endzone = cx.zone_query(
                            l1,
                            Query::FindZone {
                                space: self.p.space,
                                normalzone: true,
                                knownto: knownto.clone(),
                                min_distance: Some((ship, 40000.0)),
                                not_highway: false,
                            },
                        )?;
                        if self.endzone.is_null() {
                            self.endzone = cx.zone_query(
                                l2,
                                Query::FindZone {
                                    space: self.p.space,
                                    normalzone: true,
                                    knownto,
                                    min_distance: None,
                                    not_highway: false,
                                },
                            )?;
                            if self.endzone.is_null() {
                                let z = cx.facts.uid_of(cx.ent, Prop::Zone);
                                if cx.facts.exists(z) && !cx.facts.is_class(z, Class::Highway) {
                                    cx.debug(346, self.p.debugchance)?;
                                    self.p.space = z;
                                    self.p.range = Some(Range::Zone);
                                    self.pc = Pc::Start;
                                    continue;
                                }
                                // L352 error output (no chance), L353-355
                                let f = cx.facts;
                                let cur = f.order_of(ship);
                                let def = f.default_order_of(ship);
                                if !cur.is_null() && cur == def {
                                    cx.fx(354, Effect::CancelOrder { order: def })?;
                                }
                                self.pc = Pc::Finish;
                                continue;
                            }
                        }
                        // L361-376: stayhidden only.
                        if self.p.includeroute {
                            let mut route = match cx.query(379, Query::GlobalPath { start: ship, end: self.endzone })? {
                                crate::world::Answer::Path(p) => p,
                                other => {
                                    return Err(Stop::Mismatch(format!("line 379: expected a path, got {other:?}")))
                                }
                            };
                            // L385: append $endzone if the path does not end there
                            if !route.is_empty() && route[route.len() - 1] != self.endzone {
                                route.push(self.endzone);
                            }
                            // L389-394: drop entries that are not normal zones (reverse order)
                            let mut i = route.len();
                            while i > 0 {
                                let z = route[i - 1];
                                if !cx.facts.is_class(z, Class::Zone) || !cx.facts.flag(z, Prop::IsNormalZone) {
                                    cx.debug(391, self.p.debugchance)?;
                                    route.remove(i - 1);
                                }
                                i -= 1;
                            }
                            self.route = Some(route);
                        }
                    }
                    cx.debug(398, self.p.debugchance)?;
                    // L400-404: debug output without chance.
                    self.pc = Pc::FindPatrolzone;
                }
                Pc::FindPatrolzone => {
                    self.last_label = Label::FindPatrolzone; // L408
                    let f = cx.facts;
                    if f.is_class(self.p.space, Class::Zone) {
                        self.patrolzone = Some(self.p.space); // L411
                    } else if self.route.as_ref().is_some_and(|r| !r.is_empty()) {
                        let r = self.route.as_mut().expect("route");
                        self.patrolzone = Some(r.remove(0)); // L414-415
                    } else if self.patrolzone.unwrap_or(Uid::NULL) != self.endzone {
                        self.patrolzone = Some(self.endzone); // L418
                    } else {
                        self.pc = Pc::FindEndzone; // L422
                        continue;
                    }
                    // L426
                    if self.expired_or_timed_out(f, true) {
                        self.pc = Pc::Finish;
                        continue;
                    }
                    cx.debug(430, self.p.debugchance)?;
                    let pz = self.patrolzone.unwrap_or(Uid::NULL);
                    let f = cx.facts;
                    if f.uid_of(cx.ent, Prop::Sector) != f.uid_of(pz, Prop::Sector) {
                        return self.call_move_generic(cx, 434, pz, Pc::AfterMoveToPatrolzone);
                    }
                    self.pc = Pc::AfterMoveToPatrolzone;
                    movesuccess = true;
                    continue;
                }
                Pc::AfterMoveToPatrolzone => {
                    if !movesuccess {
                        // L441-451
                        let pz = self.patrolzone.unwrap_or(Uid::NULL);
                        if self.p.space == pz {
                            let cur = cx.facts.order_of(ship);
                            if cx.facts.order_running(cur) {
                                cx.fx(444, Effect::OrderFailed { order: cur, text: "{1045, 101}" })?;
                            }
                            cx.debug(446, self.p.debugchance)?;
                            self.pc = Pc::Finish;
                            continue;
                        }
                        cx.debug(449, self.p.debugchance)?;
                        self.pc = Pc::FindPatrolzone;
                        continue;
                    }
                    if self.inittime.is_none() {
                        self.inittime = Some(cx.now()); // L455
                    }
                    self.pc = Pc::FindPosition;
                }
                Pc::FindPosition => {
                    self.last_label = Label::FindPosition; // L458
                    if self.expired_or_timed_out(cx.facts, false) {
                        self.pc = Pc::Finish; // L461-463
                        continue;
                    }
                    let f = cx.facts;
                    let pz = self.patrolzone.unwrap_or(Uid::NULL);
                    let ez = self.endzone;
                    let bad = !f.exists(ez)
                        || !f.exists(pz)
                        || f.is_class(ez, Class::Highway)
                        || f.is_class(pz, Class::Highway);
                    if bad {
                        // L466-482: one of four debug lines, then wait 1-2 s and restart
                        let line = if !f.exists(ez) {
                            468
                        } else if !f.exists(pz) {
                            471
                        } else if f.is_class(ez, Class::Highway) {
                            474
                        } else {
                            477
                        };
                        cx.debug(line, self.p.debugchance)?;
                        return self.wait_range(cx, 480, 1.0, 2.0, Pc::AfterSafetyWait);
                    }
                    // L484: random position in the patrol zone
                    let half = f.length(pz, Prop::Size) / 2.0;
                    let base = cx.pos_query(484, Query::CreatePosition { space: pz, max: half })?;
                    // L485: $y in [-2km, 2km] (lengths are f32)
                    let y = cx.range(485, NumKind::F32, -2000.0, 2000.0)? as f32;
                    // L486: safe position
                    let radius = cx.facts.length(ship, Prop::Size) + 200.0;
                    self.pos = cx.pos_query(
                        486,
                        Query::SafePos {
                            value: Pos::new(base.x, y, base.z),
                            zone: pz,
                            radius,
                            ignored: ship,
                            allowyaxis: false,
                            ignoredangerousregions: self.p.stayhidden,
                        },
                    )?;
                    self.pc = Pc::Patrol;
                }
                Pc::AfterSafetyWait => {
                    self.pc = Pc::Start; // L481
                }
                Pc::Patrol => {
                    self.last_label = Label::Patrol; // L491
                    let f = cx.facts;
                    let plunderbasket = if self.p.usebasket {
                        match f.get(ship, Prop::WareBasket) {
                            Val::List(l) if !l.is_empty() => Val::List(l),
                            _ => Val::List(vec![]),
                        }
                    } else {
                        Val::List(vec![])
                    };
                    let pz = self.patrolzone.unwrap_or(Uid::NULL);
                    let params = vec![
                        ("destination", Val::uid(pz)),
                        ("pos", Val::Pos(self.pos)),
                        ("pursuetargets", Val::Bool(self.p.pursuetargets)),
                        ("pursuedistance", Val::F32(self.p.pursuedistance)),
                        ("allowothertargets", Val::Bool(self.p.allowothertargets)),
                        ("targetpurposes", Val::List(self.p.targetpurposes.clone())),
                        ("stayinspace", Val::Bool(self.p.stayinspace)),
                        ("plunderbasket", plunderbasket.clone()),
                        ("internalorder", Val::Bool(true)),
                        ("engageonlyonline", Val::Bool(self.p.engageonlyonline)),
                        ("targetclasses", Val::List(self.p.targetclasses.clone())),
                        ("checkrelation", Val::Bool(self.p.checkrelation)),
                        ("debugchance", Val::F32(self.p.debugchance)),
                    ];
                    cx.fx(493, Effect::RunScript { name: SeekEnemies::NAME, params: params.clone() })?;
                    let seek = SeekEnemies::for_patrol(self, ship, pz, self.pos, plunderbasket, cx.facts)?;
                    self.callee = Some(Frame::Seek(Box::new(seek)));
                    self.pc = Pc::AfterSeek;
                    return Ok(Yield::Call { script: SeekEnemies::NAME, params });
                }
                Pc::AfterSeek => {
                    return self.wait_range(cx, 509, 1.0, 2.0, Pc::AfterPatrolWait);
                }
                Pc::AfterPatrolWait => {
                    // L511: chance 30 is rolled before the condition is evaluated
                    if cx.roll(511, 30.0)? && cx.now() > self.next_resupply_check.unwrap_or(0.0) {
                        cx.debug(512, self.p.debugchance)?;
                        cx.fx(
                            513,
                            Effect::SignalObjects {
                                object: ship,
                                param: Val::Str("resupply".into()),
                                param2: Val::List(vec![Val::Bool(false)]),
                                param3: Val::F32(self.p.debugchance),
                            },
                        )?;
                        self.next_resupply_check = Some(cx.now() + 1800.0); // L514
                    }
                    self.pc = Pc::FindPatrolzone; // L517
                }
                Pc::Finish => {
                    self.last_label = Label::Finish; // L520
                    let f = cx.facts;
                    let def = f.default_order_of(ship);
                    if !def.is_null() && def == f.order_of(ship) {
                        cx.fx(523, Effect::CancelOrder { order: def })?;
                    }
                    let f = cx.facts;
                    if f.flag(ship, Prop::JobExpired) && f.order_kind(f.order_of(ship)) != "MoveDie" {
                        cx.debug(528, self.p.debugchance)?;
                        let big = cx.facts.is_class(ship, Class::ShipL) || cx.facts.is_class(ship, Class::ShipXL);
                        cx.fx(
                            529,
                            Effect::CreateOrder {
                                id: "MoveDie",
                                object: ship,
                                immediate: true,
                                params: vec![("byhighway", Val::Bool(!big)), ("byhostile", Val::Bool(true))],
                            },
                        )?;
                    }
                    self.pc = Pc::Done;
                    return Ok(Yield::Return(Vec::new()));
                }
                Pc::Done => return Err(Stop::Mismatch(format!("{SCRIPT} resumed after it ended"))),
            }
        }
    }

    /// Conditions of global handler `i` (`None` = not ported). They never draw.
    pub fn matches(&self, _f: &dyn Facts, i: usize, ev: &Event) -> Option<bool> {
        let ship = self.ship;
        match Self::HANDLERS[i] {
            // interrupt.changedsector.xml L7-10
            "SectorChangeHandler" => Some(
                (ev.name == "event_object_entered_anomaly" || ev.name == "event_object_entered_gate")
                    && ev.object == ship,
            ),
            // L74-79
            "inline1" => Some(
                (ev.name == "event_object_target_invalid" && ev.object == ship)
                    || (ev.name == "event_object_destroyed" && ev.object == self.p.space && !self.p.space.is_null()),
            ),
            _ => None,
        }
    }

    /// Actions of a matched global handler (run in `AIDirector::Move` by the interrupt runner).
    pub fn handler_actions(&mut self, cx: &mut Cx<'_>, i: usize, ev: &Event) -> R<Outcome> {
        let ship = self.ship;
        match Self::HANDLERS[i] {
            "SectorChangeHandler" => {
                // interrupt.changedsector.xml L16-63. In this script's scope $nextsector and
                // $pathsectors never exist, so the L16 test is always true.
                let p2 = ev.param2.as_uid().unwrap_or(Uid::NULL);
                let newsector = cx.facts.uid_of(p2, Prop::Sector);
                cx.roll(18, self.p.debugchance)?;
                if cx.facts.flag(ship, Prop::IsPlayerOwned) {
                    let p1 = ev.param.as_uid().unwrap_or(Uid::NULL);
                    let cluster = cx.facts.uid_of(newsector, Prop::Cluster);
                    cx.fx(22, Effect::SetKnown { object: p1 })?;
                    cx.fx(23, Effect::SetKnown { object: p2 })?;
                    cx.fx(24, Effect::SetKnown { object: newsector })?;
                    cx.fx(25, Effect::SetKnown { object: cluster })?;
                }
                if self.p.debugchance != 0.0 {
                    cx.roll(54, self.p.debugchance)?;
                }
                Ok(Outcome::AbortCalled { label: Some("start") })
            }
            // L81-83: debug output without chance; L84
            "inline1" => Ok(Outcome::AbortCalled { label: Some("start") }),
            h => Err(Stop::Mismatch(format!("{SCRIPT}: actions of opaque handler {h} requested"))),
        }
    }

    /// The `$space` the order currently patrols (for tests and diagnostics).
    pub fn space(&self) -> Uid {
        self.p.space
    }

    pub fn range(&self) -> Val {
        Range::val(self.p.range)
    }
}

#[allow(dead_code)]
fn _order_unused(_: OrderId) {}
