//! `move.seekenemies` (script version 7) as called by `order.fight.patrol` L493: fly to a reserved safe
//! position in the patrol zone, interrupt on enemies, then run `lib.target.selection` (opaque in M1)
//! and create an immediate `Attack` order.
//!
//! Ported paths: escort = null, recon = false, plunder = false, radius = null (all Patrol calls). The
//! move_to interrupt *conditions* are evaluated by the engine (capture verdict, including the
//! `chance="80"` distress roll); its *actions* are native. `TargetInvalidHandler` and the
//! `patch_reset_moveto` handler are native; the other library handlers are opaque.
//! Line numbers: `aiscripts/move.seekenemies.xml` (base 9.00; the ego_dlc_pirate diff only adds a
//! condition inside the distress check, which the engine evaluates).

use super::patrol::Patrol;
use crate::runtime::{Event, Outcome, RetVals, Wake, Yield};
use crate::val::{Pos, Time, Uid, Val};
use crate::world::{Class, Cx, Effect, Prop, Query, Stop, R};

const SCRIPT: &str = "move.seekenemies";

fn unsupported(line: u32, what: &'static str) -> Stop {
    Stop::Unsupported { script: SCRIPT, line, what }
}

/// `class.ship_m` -> [`Class::ShipM`] (values of `$targetclasses`).
pub fn class_of_val(v: &Val) -> Option<Class> {
    let k = v.as_key()?;
    let c = Class::parse(k.strip_prefix("class.").unwrap_or(k));
    (c != Class::Other).then_some(c)
}

/// Call parameters (`<params>`, defaults as in the XML; Patrol passes the ones marked).
#[derive(Clone, Debug, PartialEq)]
pub struct SeekParams {
    pub destination: Uid, // Patrol: $patrolzone
    pub pos: Pos,         // Patrol: $pos
    pub pursuetargets: bool,
    pub escort: Uid,
    pub pursuedistance: f32,
    pub allowothertargets: bool,
    pub targetclasses: Vec<Val>,
    pub targetpurposes: Vec<Val>,
    pub list_objectstoignore: Vec<Val>,
    pub recon: bool,
    pub travel: bool,
    pub plunder: bool,
    pub plunderbasket: Vec<Val>,
    pub stayinspace: bool,
    pub internalorder: bool,
    pub engageonlyonline: bool,
    pub checkrelation: bool,
    pub engageonsight: bool,
    pub enforceradius: bool,
    pub debugchance: f32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Label {
    Start,
    Moveto,
    Attack,
    End,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Pc {
    Init,
    Start,
    AfterMoveGeneric,
    AfterHighwayWait,
    Moveto,
    AfterMoveTo,
    AfterTargetSelection,
    Attack,
    End,
    Done,
}

/// `$reservedspace` table.
#[derive(Clone, Debug, PartialEq)]
struct Reservation {
    duration: f64,
    index: Val,
    zone: Uid,
}

#[derive(Clone, Debug)]
pub struct SeekEnemies {
    pub p: SeekParams,
    ship: Uid,
    pc: Pc,
    last_label: Label,
    enemy: Uid,
    targetpos: Pos,
    referenceobject: Uid,
    reservedspace: Option<Reservation>,
    interrupted: bool,
    /// `$lostcover` is set at L507/L512 and never removed.
    lostcover: bool,
    time_movestarted: Option<Time>,
    detected: Vec<Uid>,
}

fn ret_uid(v: &RetVals, k: &str) -> Uid {
    v.iter().find(|(n, _)| n == k).and_then(|(_, v)| v.as_uid()).unwrap_or(Uid::NULL)
}

fn ret_bool(v: &RetVals) -> bool {
    v.iter()
        .find(|(k, _)| k == "v")
        .map(|(_, v)| !matches!(v, Val::Null | Val::Bool(false) | Val::Int(0)))
        .unwrap_or(false)
}

impl SeekEnemies {
    pub const NAME: &'static str = SCRIPT;
    pub const HANDLERS: &'static [&'static str] = &[
        "AttackHandler",
        "MissileLockHandler",
        "ScannedHandler",
        "InspectedHandler",
        "FoundAbandonedHandler",
        "FoundLockboxHandler",
        "TargetInvalidHandler",
        "inline1",
    ];

    pub fn new(p: SeekParams, ship: Uid) -> SeekEnemies {
        SeekEnemies {
            p,
            ship,
            pc: Pc::Init,
            last_label: Label::Start,
            enemy: Uid::NULL,
            targetpos: Pos::default(),
            referenceobject: Uid::NULL,
            reservedspace: None,
            interrupted: false,
            lostcover: false,
            time_movestarted: None,
            detected: Vec::new(),
        }
    }

    /// The call of `order.fight.patrol` L493-507.
    pub fn for_patrol(
        patrol: &Patrol,
        ship: Uid,
        destination: Uid,
        pos: Pos,
        plunderbasket: Val,
        facts: &dyn crate::world::Facts,
    ) -> R<SeekEnemies> {
        let pp = &patrol.p;
        Ok(SeekEnemies::new(
            SeekParams {
                destination,
                pos,
                pursuetargets: pp.pursuetargets,
                escort: Uid::NULL,
                pursuedistance: pp.pursuedistance,
                allowothertargets: pp.allowothertargets,
                targetclasses: pp.targetclasses.clone(),
                targetpurposes: pp.targetpurposes.clone(),
                list_objectstoignore: Vec::new(),
                recon: false,
                travel: false,
                plunder: false,
                plunderbasket: plunderbasket.as_list().map(|l| l.to_vec()).unwrap_or_default(),
                stayinspace: pp.stayinspace,
                internalorder: true,
                engageonlyonline: pp.engageonlyonline,
                checkrelation: pp.checkrelation,
                engageonsight: true,
                // param default `this.isplayerowned`, evaluated at the call
                enforceradius: facts.flag(ship, Prop::IsPlayerOwned),
                debugchance: pp.debugchance,
            },
            ship,
        ))
    }

    pub fn restart_at(&mut self, label: Option<&str>) -> R<()> {
        let l = match label {
            None => self.last_label,
            Some("start") => Label::Start,
            Some("moveto") => Label::Moveto,
            Some("attack") => Label::Attack,
            Some("end") => Label::End,
            Some(s) => return Err(Stop::Mismatch(format!("{SCRIPT} has no label {s:?}"))),
        };
        self.pc = match l {
            Label::Start => Pc::Start,
            Label::Moveto => Pc::Moveto,
            Label::Attack => Pc::Attack,
            Label::End => Pc::End,
        };
        Ok(())
    }

    fn after_move_generic_or_start(&mut self, cx: &mut Cx<'_>) -> R<Option<Yield>> {
        // L145-155: wait while in a highway
        let z = cx.facts.uid_of(cx.ent, Prop::Zone);
        if cx.facts.is_class(z, Class::Highway) {
            self.pc = Pc::AfterHighwayWait;
            return Ok(Some(Yield::Wait { until: None }));
        }
        self.pc = Pc::Moveto;
        Ok(None)
    }

    pub fn resume(&mut self, cx: &mut Cx<'_>, wake: Wake) -> R<Yield> {
        let ship = self.ship;
        let mut movesuccess = false;
        match (&wake, self.pc) {
            (Wake::Start, Pc::Init) | (Wake::Restart, _) => {}
            (Wake::Returned(v), Pc::AfterMoveGeneric) => movesuccess = ret_bool(v),
            (Wake::Returned(v), Pc::AfterTargetSelection) => self.enemy = ret_uid(v, "target"),
            (Wake::Block, Pc::AfterMoveTo | Pc::AfterHighwayWait) => {}
            _ => {
                return Err(Stop::Mismatch(format!(
                    "{SCRIPT}: wake {} does not fit continuation {:?}",
                    wake.name(),
                    self.pc
                )))
            }
        }
        loop {
            match self.pc {
                Pc::Init => {
                    self.enemy = Uid::NULL; // L83
                    let f = cx.facts;
                    let dest = self.p.destination;
                    if f.is_class(dest, Class::Sector) {
                        return Err(unsupported(86, "destination is a sector"));
                    }
                    let dsec = f.uid_of(dest, Prop::Sector);
                    if dsec.is_null() {
                        // L94 error output only
                        self.pc = Pc::End;
                        continue;
                    }
                    // L90-91
                    self.targetpos =
                        cx.pos_query(90, Query::CreatePositionIn { space: dsec, value: self.p.pos, object: dest })?;
                    self.p.destination = dsec;
                    // L99-104 (escort is null on the Patrol path)
                    self.referenceobject =
                        if cx.facts.flag(self.p.escort, Prop::IsOperational) { self.p.escort } else { ship };
                    // L106-108
                    if self.p.targetclasses.is_empty() {
                        self.p.targetclasses =
                            ["class.ship_s", "class.ship_m", "class.ship_l", "class.ship_xl", "class.station"]
                                .iter()
                                .map(|k| Val::key(k))
                                .collect();
                    }
                    self.pc = Pc::Start;
                }
                Pc::Start => {
                    self.last_label = Label::Start; // L110
                    let f = cx.facts;
                    let dest = self.p.destination;
                    if !f.exists(dest) {
                        cx.debug(114, self.p.debugchance)?;
                        self.pc = Pc::End;
                        continue;
                    }
                    if !f.is_class(dest, Class::Zone) && !f.is_class(dest, Class::Sector) {
                        cx.debug(118, self.p.debugchance)?;
                        self.pc = Pc::End;
                        continue;
                    }
                    if f.uid_of(cx.ent, Prop::Sector) != dest {
                        cx.debug(124, self.p.debugchance)?;
                        let params = vec![
                            ("destination", Val::uid(dest)),
                            ("position", Val::Pos(self.targetpos)),
                            ("endintargetzone", Val::Bool(true)),
                            ("activepatrol", Val::Bool(true)),
                            ("radius", Val::Null),
                            ("radiusanchorpos", Val::Null),
                            ("radiusanchorspace", Val::Null),
                            ("strictblacklist", Val::Bool(self.p.internalorder)),
                            ("debugchance", Val::F32(self.p.debugchance)),
                        ];
                        cx.fx(125, Effect::RunScript { name: "move.generic", params: params.clone() })?;
                        self.pc = Pc::AfterMoveGeneric;
                        return Ok(Yield::Call { script: "move.generic", params });
                    }
                    if let Some(y) = self.after_move_generic_or_start(cx)? {
                        return Ok(y);
                    }
                }
                Pc::AfterMoveGeneric => {
                    if !movesuccess {
                        cx.debug(137, self.p.debugchance)?;
                        let cur = cx.facts.order_of(ship);
                        if cx.facts.order_running(cur) {
                            cx.fx(139, Effect::OrderFailed { order: cur, text: "{1045, 101}" })?;
                        }
                        self.pc = Pc::End;
                        continue;
                    }
                    if let Some(y) = self.after_move_generic_or_start(cx)? {
                        return Ok(y);
                    }
                }
                Pc::AfterHighwayWait => {
                    self.pc = Pc::Moveto;
                }
                Pc::Moveto => {
                    self.last_label = Label::Moveto; // L157
                    cx.debug(159, self.p.debugchance)?;
                    // L161-166
                    match &self.reservedspace {
                        Some(r) if !r.zone.is_null() => {
                            let (index, zone) = (r.index.clone(), r.zone);
                            cx.fx(162, Effect::CancelSpaceReservation { component: ship, index, zone })?;
                        }
                        _ => {
                            let d =
                                cx.facts.get(Uid::NULL, Prop::SpaceReservationMaxDuration).as_f64().unwrap_or(1800.0);
                            self.reservedspace = Some(Reservation { duration: d, index: Val::Null, zone: Uid::NULL });
                        }
                    }
                    // L169-182: safe position with a space reservation
                    let f = cx.facts;
                    let dest = self.p.destination;
                    let in_sector = f.is_class(dest, Class::Sector);
                    let radius = f.length(ship, Prop::SafeSize) / 2.0;
                    let duration = self.reservedspace.as_ref().map(|r| r.duration).unwrap_or(1800.0);
                    let line = if in_sector { 170 } else { 177 };
                    let q = Query::SafePosReserve {
                        value: self.targetpos,
                        space: dest,
                        in_sector,
                        radius,
                        component: ship,
                        duration,
                    };
                    match cx.query(line, q)? {
                        crate::world::Answer::Reserved { pos, index, zone } => {
                            self.targetpos = pos;
                            let r = self.reservedspace.as_mut().expect("reservation");
                            r.index = index;
                            r.zone = zone;
                        }
                        other => {
                            return Err(Stop::Mismatch(format!("line {line}: expected a reservation, got {other:?}")))
                        }
                    }
                    // L183: chance="@global.$spacereservationspam * 100"
                    let spam = match cx.facts.get(Uid::NULL, Prop::SpaceReservationSpam) {
                        Val::Int(i) => (i * 100) as f32,
                        Val::F32(v) => v * 100.0,
                        Val::Bool(b) => (b as i32 * 100) as f32,
                        _ => 0.0,
                    };
                    cx.debug(183, spam)?;
                    self.time_movestarted = Some(cx.now()); // L185
                                                            // L187
                    cx.fx(
                        187,
                        Effect::MoveTo {
                            object: ship,
                            destination: self.p.destination,
                            pos: self.targetpos,
                            travel: self.p.travel,
                        },
                    )?;
                    self.pc = Pc::AfterMoveTo;
                    return Ok(Yield::MoveTo);
                }
                Pc::AfterMoveTo => {
                    // L537-539
                    let quick = self.time_movestarted.is_some_and(|t0| cx.now() - t0 < 1.0);
                    if !self.interrupted && quick && cx.facts.length(ship, Prop::DistanceToTarget) > 1000.0 {
                        cx.debug(538, self.p.debugchance)?;
                    }
                    self.time_movestarted = None;
                    self.interrupted = false;
                    // L545-566
                    if !cx.facts.flag(self.enemy, Prop::CanBeAttacked) && self.p.plunderbasket.is_empty() {
                        let params = vec![
                            ("primarytarget", Val::Null),
                            ("secondarytargets", Val::Null),
                            ("escort", Val::uid(self.p.escort)),
                            ("pursuedistance", Val::F32(self.p.pursuedistance)),
                            ("pursuetargets", Val::Bool(self.p.pursuetargets)),
                            ("list_objectstoignore", Val::List(self.p.list_objectstoignore.clone())),
                            ("allowothertargets", Val::Bool(true)),
                            ("checkrelation", Val::Bool(self.p.checkrelation)),
                            ("targetpurposes", Val::List(self.p.targetpurposes.clone())),
                            ("targetclasses", Val::List(self.p.targetclasses.clone())),
                            ("engageonlyonline", Val::Bool(self.p.engageonlyonline)),
                            ("radius", Val::Null),
                            ("radiusanchorpos", Val::Null),
                            ("radiusanchorspace", Val::Null),
                            ("debugchance", Val::F32(self.p.debugchance)),
                        ];
                        cx.fx(547, Effect::RunScript { name: "lib.target.selection", params: params.clone() })?;
                        self.pc = Pc::AfterTargetSelection;
                        return Ok(Yield::Call { script: "lib.target.selection", params });
                    }
                    self.pc = Pc::Attack;
                }
                Pc::AfterTargetSelection => {
                    self.pc = Pc::Attack;
                }
                Pc::Attack => {
                    self.last_label = Label::Attack; // L568
                    let f = cx.facts;
                    let enemy = self.enemy;
                    if f.flag(enemy, Prop::CanBeAttacked) {
                        // L572: rolled first, condition false on this path ($plunder is false)
                        if cx.roll(572, self.p.debugchance)? && self.p.plunder {
                            return Err(unsupported(572, "plunder"));
                        }
                        if self.p.recon || self.p.plunder {
                            self.pc = Pc::End; // L577-579
                            continue;
                        }
                        // L581-585
                        let f = cx.facts;
                        let cur = f.order_of(ship);
                        let ospace = f.order_get(cur, Prop::OrderSpace).as_uid().unwrap_or(Uid::NULL);
                        let ezone = f.uid_of(enemy, Prop::Zone);
                        if !self.p.stayinspace
                            && f.flag(ship, Prop::IsJobShip)
                            && f.is_class(ospace, Class::Zone)
                            && ospace != ezone
                            && f.uid_of(enemy, Prop::Sector) == f.uid_of(cx.ent, Prop::Sector)
                            && !f.is_class(ezone, Class::Highway)
                        {
                            cx.debug(583, self.p.debugchance)?;
                            cx.fx(584, Effect::EditOrderParam { order: cur, param: "space", value: Val::uid(ezone) })?;
                        }
                        // L587: chance="0" never runs, but rolls
                        cx.roll(587, 0.0)?;
                        // L602-613
                        let f = cx.facts;
                        if !f.uid_of(ship, Prop::Commander).is_null() && !f.uid_of(enemy, Prop::Pilot).is_null() {
                            return Err(unsupported(602, "npc_usecase comm chatter of a subordinate"));
                        }
                        cx.debug(615, self.p.debugchance)?;
                        // L621-636
                        let params = vec![
                            ("primarytarget", Val::uid(enemy)),
                            ("escort", Val::uid(self.p.escort)),
                            ("pursuedistance", Val::F32(self.p.pursuedistance)),
                            ("pursuetargets", Val::Bool(self.p.pursuetargets)),
                            ("allowothertargets", Val::Bool(self.p.allowothertargets)),
                            ("targetclasses", Val::List(self.p.targetclasses.clone())),
                            ("disable", Val::Bool(!self.p.plunderbasket.is_empty())),
                            ("radius", Val::Null),
                            ("radiusanchorpos", Val::Null),
                            ("radiusanchorspace", Val::Null),
                            ("internalorder", Val::Bool(self.p.internalorder)),
                            ("checkrelation", Val::Bool(self.p.checkrelation)),
                            ("enforceradius", Val::Bool(self.p.enforceradius)),
                            ("debugchance", Val::F32(self.p.debugchance)),
                        ];
                        cx.fx(621, Effect::CreateOrder { id: "Attack", object: ship, immediate: true, params })?;
                    } else {
                        cx.debug(639, self.p.debugchance)?;
                    }
                    self.pc = Pc::End;
                }
                Pc::End => {
                    self.last_label = Label::End; // L642
                    if let Some(r) = &self.reservedspace {
                        if !r.zone.is_null() {
                            let (index, zone) = (r.index.clone(), r.zone);
                            cx.fx(645, Effect::CancelSpaceReservation { component: ship, index, zone })?;
                        }
                    }
                    self.pc = Pc::Done;
                    if cx.facts.flag(ship, Prop::JobExpired) {
                        let cap = cx.facts.flag(ship, Prop::IsCapitalShip);
                        cx.fx(
                            650,
                            Effect::CreateOrder {
                                id: "MoveDie",
                                object: ship,
                                immediate: true,
                                params: vec![("byhighway", Val::Bool(!cap)), ("byhostile", Val::Bool(true))],
                            },
                        )?;
                        return Ok(Yield::Return(Vec::new()));
                    }
                    if self.p.recon || self.p.plunder {
                        return Err(unsupported(655, "recon/plunder return"));
                    }
                    return Ok(Yield::Return(vec![
                        ("target".into(), Val::Null),
                        ("list_targets".into(), Val::List(vec![])),
                    ]));
                }
                Pc::Done => return Err(Stop::Mismatch(format!("{SCRIPT} resumed after it ended"))),
            }
        }
    }

    /// Conditions of global handler `i` (`None` = not ported). They never draw.
    pub fn matches(&self, _f: &dyn crate::world::Facts, ent: Uid, i: usize, ev: &Event) -> Option<bool> {
        match Self::HANDLERS[i] {
            // interrupt.targetinvalid.xml L9-11
            "TargetInvalidHandler" => Some(ev.name == "event_object_target_invalid" && ev.object == self.ship),
            // L39-41: event_object_signalled object="this" param="'patch_reset_moveto'"
            "inline1" => Some(
                ev.name == "event_object_signalled"
                    && ev.object == ent
                    && ev.param == Val::Str("patch_reset_moveto".into()),
            ),
            _ => None,
        }
    }

    /// Actions of a matched global handler.
    pub fn handler_actions(&mut self, cx: &mut Cx<'_>, i: usize, _ev: &Event) -> R<Outcome> {
        let ship = self.ship;
        match Self::HANDLERS[i] {
            "TargetInvalidHandler" => {
                cx.roll(14, 0.0)?; // set_value $dbg_target chance="0"
                cx.roll(15, self.p.debugchance)?; // debug_text chance="@$debugchance"
                Ok(Outcome::AbortCalled { label: None }) // L17
            }
            "inline1" => {
                cx.fx(44, Effect::StopMoving { object: ship })?;
                cx.fx(45, Effect::DefaultFlightControlModel { object: ship })?;
                Ok(Outcome::AbortCalled { label: Some("start") })
            }
            h => Err(Stop::Mismatch(format!("{SCRIPT}: actions of opaque handler {h} requested"))),
        }
    }

    /// The controlled ship.
    pub fn ship(&self) -> Uid {
        self.ship
    }

    /// Blocked in an action that has an `<interrupt>` (the move_to at L187 or the highway wait at L146).
    pub fn blocked_with_interrupt(&self) -> bool {
        matches!(self.pc, Pc::AfterMoveTo | Pc::AfterHighwayWait)
    }

    /// Actions of the blocking action's `<interrupt>` once the engine reported that its conditions held:
    /// the highway `<wait>` (L146-153, no actions) or the `<move_to>` (L392-533).
    pub fn block_interrupt(&mut self, cx: &mut Cx<'_>, ev: &Event, detected: Option<&[Uid]>) -> R<Outcome> {
        match self.pc {
            Pc::AfterHighwayWait => Ok(Outcome::BlockDone),
            Pc::AfterMoveTo => self.moveto_interrupt(cx, ev, detected),
            pc => Err(Stop::Mismatch(format!("{SCRIPT}: blocking interrupt at {pc:?}"))),
        }
    }

    // The filter branches mirror the XML do_if/do_elseif chain one to one (several only remove).
    #[allow(clippy::if_same_then_else)]
    fn moveto_interrupt(&mut self, cx: &mut Cx<'_>, ev: &Event, detected: Option<&[Uid]>) -> R<Outcome> {
        let ship = self.ship;
        self.interrupted = true; // L393
        cx.debug(394, self.p.debugchance)?;
        match ev.name.as_str() {
            "event_gravidar_has_scanned" => {
                self.detected = detected.map(|d| d.to_vec()).unwrap_or_default();
                let this_sector = cx.facts.uid_of(cx.ent, Prop::Sector);
                let occupied = cx.facts.uid_of(Uid::NULL, Prop::PlayerOccupiedShip);
                // L396-431 (reverse)
                let mut i = self.detected.len();
                while i > 0 {
                    let o = self.detected[i - 1];
                    let f = cx.facts;
                    if f.uid_of(o, Prop::Sector) != this_sector || !f.can_see(ship, o) {
                        cx.debug(398, self.p.debugchance)?;
                        self.detected.remove(i - 1);
                    } else if self.p.list_objectstoignore.contains(&Val::uid(o)) {
                        self.detected.remove(i - 1);
                    } else if !self.p.targetpurposes.is_empty()
                        && !self.p.targetpurposes.contains(&f.get(o, Prop::PrimaryPurpose))
                        && o != occupied
                    {
                        self.detected.remove(i - 1); // L407: error output only
                    } else if !self.p.targetclasses.is_empty()
                        && !self.p.targetclasses.iter().filter_map(class_of_val).any(|c| f.is_class(o, c))
                    {
                        self.detected.remove(i - 1);
                    } else if self.p.engageonlyonline && !f.flag(o, Prop::IsOnlineObject) {
                        self.detected.remove(i - 1);
                    } else if self.p.plunder || self.p.recon {
                        return Err(unsupported(416, "plunder/recon contact filter"));
                    }
                    i -= 1;
                }
                // L432: rolled first; the body only prints
                cx.roll(432, self.p.debugchance)?;
                if self.detected.is_empty() {
                    cx.debug(445, self.p.debugchance)?;
                    return Ok(Outcome::Resume { label: "moveto" });
                }
                cx.debug(448, self.p.debugchance)?;
                if !self.p.plunderbasket.is_empty() {
                    return Err(unsupported(462, "plunderbasket mark selection"));
                }
                Ok(Outcome::BlockDone)
            }
            "event_object_signalled" => {
                // L493-528
                let police = ev.param == Val::Str("police".into());
                self.enemy = if police { ev.param2.as_uid() } else { ev.param3.as_uid() }.unwrap_or(Uid::NULL);
                if police {
                    cx.debug(498, self.p.debugchance)?;
                }
                if !cx.facts.uid_of(self.enemy, Prop::CoverOwner).is_null() {
                    cx.debug(505, self.p.debugchance)?;
                    let e = self.enemy;
                    cx.fx(
                        506,
                        Effect::SignalObjects {
                            object: e,
                            param: Val::Str("LoseCover".into()),
                            param2: Val::Null,
                            param3: Val::Null,
                        },
                    )?;
                    self.lostcover = true;
                }
                if !cx.facts.uid_of(ship, Prop::CoverOwner).is_null() {
                    cx.debug(510, self.p.debugchance)?;
                    cx.fx(
                        511,
                        Effect::SignalObjects {
                            object: ship,
                            param: Val::Str("LoseCover".into()),
                            param2: Val::Null,
                            param3: Val::Null,
                        },
                    )?;
                    self.lostcover = true;
                }
                if self.lostcover {
                    cx.debug(515, self.p.debugchance)?;
                }
                if !cx.facts.may_attack(cx.ent, self.enemy) {
                    return Err(unsupported(527, "BoostRelations (lib.boost.relations)"));
                }
                Ok(Outcome::BlockDone)
            }
            "event_object_changed_attention" => Ok(Outcome::Resume { label: "start" }),
            _ => Ok(Outcome::BlockDone),
        }
    }

    /// `<on_abort>` (L676-682).
    pub fn on_abort(&mut self, cx: &mut Cx<'_>) -> R<()> {
        let ship = self.ship;
        if cx.facts.flag(ship, Prop::IsOperational) {
            if let Some(r) = &self.reservedspace {
                if !r.zone.is_null() {
                    let (index, zone) = (r.index.clone(), r.zone);
                    cx.fx(679, Effect::CancelSpaceReservation { component: ship, index, zone })?;
                }
            }
        }
        Ok(())
    }
}
