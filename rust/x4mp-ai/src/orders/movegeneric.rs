//! `move.generic` (script version 23, order id `MoveGeneric`): the movement helper behind about 60
//! orders and helpers (order.fight.patrol L280/L434, move.seekenemies L125, Escort, trade, mining). The
//! most expensive AI script by measured cost (24 % of AI run time, reports/REWRITE-AI-ORDERS.md 1.3).
//!
//! Ported: the input cases zone (normal, temporary; highways up to the long-distance move), sector
//! and cluster (with and without a position, global path, find_sector) and object (not in a local
//! highway); safe positions with space reservations (all `get_safe_pos` forms, `precise` and
//! `playerprecise`); the long-distance part (jump path with the blacklist override for non-strict
//! orders, subordinate recall and wait checks, capital ships and waits for subordinates through
//! `move.gate` as a called script, the small-ship stagger wait and `move_to` with its zone-change
//! interrupt, failed-path retries); the short-distance part; finish (incl. moving ship destinations),
//! cleanup and `on_abort`; the handlers for target-invalid / reinitialize, destroyed destination,
//! gravidar targets (Attack order, `stopondetect`), destination moved, docked/undocked,
//! `SectorChangeHandler` and `LostReservationHandler`.
//!
//! Refused with [`Stop::Unsupported`] before the branch's first side effect: rotation and lookat
//! targets, relative movement, `traveltoend` checks on a strict path, moves without a position
//! (highway destinations, L1072), objects in local highways (L561), strict blacklists without a path
//! (L859), the job-ship recovery warp (L821), adjacent-zone checks of player ships (L894), the cleanup
//! of recalled subordinates' orders (L1336/L1392), debug-only position output (L1030), and the
//! `TideHandler` (opaque: engine verdict and actions).
//!
//! Line numbers: `aiscripts/move.generic.xml` (base 9.00). Included library actions cite their own file:
//! `lib.define.blacklistgroup.xml` (GetBlacklistgroup L17-31), `interrupt.changedsector.xml`
//! (SectorChangeHandler), `interrupt.lostreservation.xml`.

use super::{p_bool, p_f32, p_list, param};
use crate::rng::NumKind;
use crate::runtime::{Event, Outcome, RetVals, Wake, Yield};
use crate::val::{Pos, Uid, Val};
use crate::world::{Answer, Class, Cx, Effect, Facts, Prop, Query, Stop, R};

const SCRIPT: &str = "move.generic";

fn unsupported(line: u32, what: &'static str) -> Stop {
    Stop::Unsupported { script: SCRIPT, line, what }
}

/// 10 km (the long-distance threshold of L794 and L1277).
const TEN_KM: f32 = 10000.0;

/// Call parameters (`<params>`), with the XML defaults evaluated for the moving ship.
#[derive(Clone, Debug, PartialEq)]
pub struct MgParams {
    pub destination: Uid,
    pub position: Option<Pos>,
    /// A rotation was passed (not ported).
    pub rotation: bool,
    /// A lookat position was passed (not ported).
    pub lookat: bool,
    pub noboost: bool,
    pub disablecollisionavoidance: bool,
    pub abortpath: bool,
    pub forcesteering: bool,
    pub endintargetzone: bool,
    pub endintargetsector: bool,
    pub activepatrol: bool,
    pub recallsubordinates: bool,
    pub strictblacklist: bool,
    pub precise: bool,
    pub playerprecise: bool,
    pub waitforatgate: Uid,
    pub uselocalhighways: bool,
    pub pursuetargets: bool,
    pub escort: Uid,
    pub pursuedistance: f32,
    pub changecommandaction: bool,
    pub targetclasses: Vec<Val>,
    pub radius: Option<f32>,
    pub radiusanchorpos: Option<Pos>,
    pub radiusanchorspace: Uid,
    pub stopondetect: Uid,
    pub allowwreck: bool,
    pub traveltoend: bool,
    pub relativemovement: bool,
    pub stayatrelativemovement: bool,
    pub enforceradius: bool,
    pub useknownpath: bool,
    pub debugchance: f32,
}

fn p_uid(ps: &[(String, Val)], name: &str) -> R<Uid> {
    match param(ps, name) {
        None => Ok(Uid::NULL),
        Some(v) => v.as_uid().ok_or_else(|| Stop::Mismatch(format!("param {name}: expected a component, got {v}"))),
    }
}

fn p_pos(ps: &[(String, Val)], name: &str) -> R<Option<Pos>> {
    match param(ps, name) {
        None | Some(Val::Null) => Ok(None),
        Some(Val::Pos(p)) => Ok(Some(p)),
        Some(other) => Err(Stop::Mismatch(format!("param {name}: expected a position, got {other}"))),
    }
}

fn p_len(ps: &[(String, Val)], name: &str) -> R<Option<f32>> {
    match param(ps, name) {
        None | Some(Val::Null) => Ok(None),
        Some(Val::F32(v)) => Ok(Some(v)),
        Some(Val::Int(i)) => Ok(Some(i as f32)),
        Some(other) => Err(Stop::Mismatch(format!("param {name}: expected a length, got {other}"))),
    }
}

impl MgParams {
    /// From `run_script` parameters; the defaults that are expressions read the ship's facts:
    /// `uselocalhighways = not this.ship.iscapitalship`, `pursuedistance =
    /// this.assignedcontrolled.maxradarrange`, `enforceradius = useknownpath = this.ship.isplayerowned`.
    pub fn from_params(ps: &[(String, Val)], f: &dyn Facts, ship: Uid) -> R<MgParams> {
        let player = f.flag(ship, Prop::IsPlayerOwned);
        Ok(MgParams {
            destination: p_uid(ps, "destination")?,
            position: p_pos(ps, "position")?,
            rotation: !matches!(param(ps, "rotation"), None | Some(Val::Null)),
            lookat: !matches!(param(ps, "lookat"), None | Some(Val::Null)),
            noboost: p_bool(ps, "noboost", false)?,
            disablecollisionavoidance: p_bool(ps, "disablecollisionavoidance", false)?,
            abortpath: p_bool(ps, "abortpath", true)?,
            forcesteering: p_bool(ps, "forcesteering", false)?,
            endintargetzone: p_bool(ps, "endintargetzone", false)?,
            endintargetsector: p_bool(ps, "endintargetsector", false)?,
            activepatrol: p_bool(ps, "activepatrol", false)?,
            recallsubordinates: p_bool(ps, "recallsubordinates", true)?,
            strictblacklist: p_bool(ps, "strictblacklist", false)?,
            precise: p_bool(ps, "precise", false)?,
            playerprecise: p_bool(ps, "playerprecise", false)?,
            waitforatgate: p_uid(ps, "waitforatgate")?,
            uselocalhighways: p_bool(ps, "uselocalhighways", !f.flag(ship, Prop::IsCapitalShip))?,
            pursuetargets: p_bool(ps, "pursuetargets", true)?,
            escort: p_uid(ps, "escort")?,
            pursuedistance: p_f32(ps, "pursuedistance", f.length(ship, Prop::MaxRadarRange))?,
            changecommandaction: p_bool(ps, "changecommandaction", true)?,
            targetclasses: p_list(ps, "targetclasses")?,
            radius: p_len(ps, "radius")?,
            radiusanchorpos: p_pos(ps, "radiusanchorpos")?,
            radiusanchorspace: p_uid(ps, "radiusanchorspace")?,
            stopondetect: p_uid(ps, "stopondetect")?,
            allowwreck: p_bool(ps, "allowwreck", false)?,
            traveltoend: p_bool(ps, "traveltoend", false)?,
            relativemovement: p_bool(ps, "relativemovement", false)?,
            stayatrelativemovement: p_bool(ps, "stayatrelativemovement", false)?,
            enforceradius: p_bool(ps, "enforceradius", player)?,
            useknownpath: p_bool(ps, "useknownpath", player)?,
            debugchance: p_f32(ps, "debugchance", 0.0)?,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Label {
    Start,
    Check,
    Move,
    Finish,
    Cleanup,
}

impl Label {
    fn parse(s: &str) -> R<Label> {
        Ok(match s {
            "start" => Label::Start,
            "check" => Label::Check,
            "move" => Label::Move,
            "finish" => Label::Finish,
            "cleanup" => Label::Cleanup,
            _ => return Err(Stop::Mismatch(format!("{SCRIPT} has no label {s:?}"))),
        })
    }
    fn pc(self) -> Pc {
        match self {
            Label::Start => Pc::Start,
            Label::Check => Pc::Check,
            Label::Move => Pc::Move,
            Label::Finish => Pc::Finish,
            Label::Cleanup => Pc::Cleanup,
        }
    }
}

/// Program counter: labels and continuations after blocking actions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Pc {
    /// First run: parameters, `<init>`, L355-359.
    Init,
    Start,
    Check,
    AfterCheckWait,
    Move,
    AfterMoveWait,
    AfterRecallWait,
    AfterGate,
    AfterStagger,
    AfterResetWait1,
    LongMove,
    AfterLongMove,
    /// Tail of the long-distance block (L1109-1115).
    LongTail,
    Short,
    AfterResetWait2,
    ShortMove,
    AfterShortMove,
    Finish,
    Cleanup,
    Done,
}

/// `$reservedspace` (`table[$duration = param.spacereservation.maxduration]`, `$zone`, `$index`).
#[derive(Clone, Debug, PartialEq)]
struct Reservation {
    duration: f64,
    index: Val,
    zone: Uid,
}

/// The script instance.
#[derive(Clone, Debug)]
pub struct MoveGeneric {
    raw: Vec<(String, Val)>,
    p: Option<MgParams>,
    ship: Uid,
    pc: Pc,
    last_label: Label,
    returnvalue: bool,
    blacklistgroup: Val,
    pos_input: Option<Pos>,
    bigship: bool,
    canuselocalhighways: bool,
    resetsignalled: bool,
    targetsector: Uid,
    targetzone: Uid,
    /// `$positionspace` (`None` = not defined yet).
    positionspace: Option<Uid>,
    /// `$position` (`None` = null).
    position: Option<Pos>,
    endintargetzone: bool,
    endintargetzone_input: Option<bool>,
    /// `$pos_initial` (`None` = not defined; `Some(None)` = null).
    pos_initial: Option<Option<Pos>>,
    reservedspace: Option<Reservation>,
    /// `$endzone` (`None` = removed or never set; `Some(NULL)` = null, no path).
    endzone: Option<Uid>,
    failedattempts: i64,
    intersector: bool,
    strictblacklist: bool,
    resetstrictblacklist: bool,
    blacklistoverride: bool,
    jumppath: Vec<Uid>,
    /// `$nextsector`, read by this frame's SectorChangeHandler while move.gate runs.
    nextsector: Option<Uid>,
    waitforsubordinates: bool,
    locabortpath: Option<bool>,
    locblacklistgroup: Val,
    /// `$locendzone` of the short-distance move_to (`None` = not defined).
    locendzone: Option<Uid>,
    safepos: Option<Pos>,
    /// `$attacktarget`, set by the gravidar handler's conditions.
    attacktarget: Uid,
    /// `$_stop`, set by the gravidar handler's conditions.
    stop_flag: bool,
    /// The blocking action with an `<interrupt>` the frame waits in (L616 wait, L1051 move_to).
    blocked: Option<u32>,
}

impl MoveGeneric {
    pub const NAME: &'static str = SCRIPT;
    /// Global handlers in declaration order (L95-L241).
    pub const HANDLERS: &'static [&'static str] = &[
        "inline1",
        "inline2",
        "inline3",
        "inline4",
        "inline5",
        "SectorChangeHandler",
        "TideHandler",
        "LostReservationHandler",
    ];

    /// A new instance for the `run_script` parameters `raw`; the parameters are resolved at its first
    /// run (when the engine evaluates the defaults).
    pub fn new(raw: Vec<(String, Val)>, ship: Uid) -> MoveGeneric {
        MoveGeneric {
            raw,
            p: None,
            ship,
            pc: Pc::Init,
            last_label: Label::Start,
            returnvalue: false,
            blacklistgroup: Val::Null,
            pos_input: None,
            bigship: false,
            canuselocalhighways: false,
            resetsignalled: false,
            targetsector: Uid::NULL,
            targetzone: Uid::NULL,
            positionspace: None,
            position: None,
            endintargetzone: false,
            endintargetzone_input: None,
            pos_initial: None,
            reservedspace: None,
            endzone: None,
            failedattempts: 0,
            intersector: false,
            strictblacklist: false,
            resetstrictblacklist: false,
            blacklistoverride: false,
            jumppath: Vec::new(),
            nextsector: None,
            waitforsubordinates: false,
            locabortpath: None,
            locblacklistgroup: Val::Null,
            locendzone: None,
            safepos: None,
            attacktarget: Uid::NULL,
            stop_flag: false,
            blocked: None,
        }
    }

    /// The controlled ship.
    pub fn ship(&self) -> Uid {
        self.ship
    }

    /// The parameters (after the first run).
    pub fn params(&self) -> Option<&MgParams> {
        self.p.as_ref()
    }

    /// Evaluate the parameters (defaults read the ship's facts) and run `<init>` (L243-247), once.
    fn ensure_params(&mut self, f: &dyn Facts) -> R<()> {
        if self.p.is_some() {
            return Ok(());
        }
        let mut p = MgParams::from_params(&self.raw, f, self.ship)?;
        if p.targetclasses.is_empty() {
            p.targetclasses = ["class.ship_s", "class.ship_m", "class.ship_l", "class.ship_xl", "class.station"]
                .iter()
                .map(|k| Val::key(k))
                .collect();
        }
        self.endintargetzone = p.endintargetzone;
        self.strictblacklist = p.strictblacklist;
        self.position = p.position;
        self.p = Some(p);
        Ok(())
    }

    fn p(&self) -> &MgParams {
        self.p.as_ref().expect("move.generic parameters are resolved at the first run")
    }

    fn dbg(&self, cx: &mut Cx<'_>, line: u32) -> R<()> {
        let c = self.p.as_ref().map(|p| p.debugchance).unwrap_or(0.0);
        cx.debug(line, c)
    }

    pub fn restart_at(&mut self, label: Option<&str>) -> R<()> {
        let l = match label {
            None => self.last_label,
            Some(s) => Label::parse(s)?,
        };
        self.pc = l.pc();
        self.blocked = None;
        Ok(())
    }

    /// Blocked in an action that has an `<interrupt>`.
    pub fn blocked_with_interrupt(&self) -> bool {
        self.blocked.is_some()
    }

    fn reservation_duration(f: &dyn Facts) -> f64 {
        f.get(Uid::NULL, Prop::SpaceReservationMaxDuration).as_f64().unwrap_or(1800.0)
    }

    /// GetBlacklistgroup (lib.define.blacklistgroup.xml L17-31) for `$loctarget = this.assignedcontrolled`.
    fn get_blacklistgroup(&mut self, cx: &mut Cx<'_>) -> R<()> {
        let ship = self.ship;
        if cx.facts.flag(ship, Prop::IsOperational) {
            let purpose = cx.facts.get(ship, Prop::PrimaryPurpose);
            let military = matches!(purpose.as_key(), Some("purpose.fight") | Some("purpose.auxiliary"));
            self.blacklistgroup =
                Val::key(if military { "blacklistgroup.military" } else { "blacklistgroup.civilian" });
            // L27: chance="@$debugchance"
            self.dbg(cx, 27)?;
        }
        // L29-31: error output without chance
        Ok(())
    }

    /// CheckCanUseLocalHighways (L80-92).
    fn check_local_highways(&mut self, cx: &mut Cx<'_>, locinit: bool) -> R<()> {
        let ship = self.ship;
        self.resetsignalled = false;
        self.canuselocalhighways = false;
        let f = cx.facts;
        if self.p().uselocalhighways && !self.bigship && !f.flag(ship, Prop::IsFormationLeader) {
            self.canuselocalhighways = true;
        }
        let dest = self.p().destination;
        let dz = f.uid_of(dest, Prop::Zone);
        let local = f.flag(dz, Prop::IsLocalHighway) || f.flag(dest, Prop::IsLocalHighway);
        if !locinit && !self.canuselocalhighways && local {
            self.dbg(cx, 88)?;
            self.resetsignalled = true;
            self.endzone = None;
            cx.fx(
                91,
                Effect::SignalObjects {
                    object: ship,
                    param: Val::Str("move.generic_reinitialize".into()),
                    param2: Val::Null,
                    param3: Val::Null,
                },
            )?;
        }
        Ok(())
    }

    /// `not $destination.exists or (not $allowwreck and $destination.iswreck)`.
    fn destination_gone(&self, f: &dyn Facts) -> bool {
        let d = self.p().destination;
        !f.exists(d) || (!self.p().allowwreck && f.flag(d, Prop::IsWreck))
    }

    /// The `$endintargetsector` test of L384 / L1157.
    fn in_target_sector(&self, f: &dyn Facts, ent: Uid) -> bool {
        let d = self.p().destination;
        let dsec = f.uid_of(d, Prop::Sector);
        ((f.is_class(d, Class::Sector) || f.is_class(d, Class::Cluster)) && f.has_context(ent, d))
            || dsec.is_null()
            || f.has_context(ent, dsec)
    }

    /// Reservation table of L647-652 / L1180-1185: cancel the previous one or create the table.
    fn renew_reservation(&mut self, cx: &mut Cx<'_>, cancel_line: u32, debug_line: Option<u32>) -> R<()> {
        match &self.reservedspace {
            Some(r) if !r.zone.is_null() => {
                let (index, zone) = (r.index.clone(), r.zone);
                cx.fx(cancel_line, Effect::CancelSpaceReservation { component: self.ship, index, zone })?;
                if let Some(l) = debug_line {
                    self.dbg(cx, l)?;
                }
            }
            Some(_) => {}
            None => {
                let d = Self::reservation_duration(cx.facts);
                self.reservedspace = Some(Reservation { duration: d, index: Val::Null, zone: Uid::NULL });
            }
        }
        Ok(())
    }

    /// A `get_safe_pos` with a space reservation; stores the reservation index and zone.
    #[allow(clippy::too_many_arguments)]
    fn safe_pos(
        &mut self,
        cx: &mut Cx<'_>,
        line: u32,
        space: Uid,
        in_sector: bool,
        value: Option<Pos>,
        object: Uid,
        radius: f32,
        max: Option<f32>,
        allowyaxis: bool,
    ) -> R<Pos> {
        let duration = self.reservedspace.as_ref().map(|r| r.duration).unwrap_or(1800.0);
        let q = Query::SafePosGeneral {
            space,
            in_sector,
            value,
            object,
            radius,
            max,
            ignored: self.ship,
            allowyaxis,
            reserve: Some((self.ship, duration)),
        };
        match cx.query(line, q)? {
            Answer::Reserved { pos, index, zone } => {
                if let Some(r) = self.reservedspace.as_mut() {
                    r.index = index;
                    r.zone = zone;
                }
                Ok(pos)
            }
            other => Err(Stop::Mismatch(format!("line {line}: expected a reservation, got {other:?}"))),
        }
    }

    fn create_position(&self, cx: &mut Cx<'_>, line: u32, space: Uid, value: Pos, object: Uid) -> R<Pos> {
        cx.pos_query(line, Query::CreatePositionIn { space, value, object })
    }

    /// L656-779: the position to move to in `$positionspace`.
    fn plot_position(&mut self, cx: &mut Cx<'_>) -> R<()> {
        let ship = self.ship;
        let dest = self.p().destination;
        let ps = self.positionspace.unwrap_or(Uid::NULL);
        let f = cx.facts;
        let safesize = f.length(ship, Prop::SafeSize);
        let size = f.length(ship, Prop::Size);
        match self.position {
            None => {
                if self.pos_initial.is_none() {
                    self.pos_initial = Some(None);
                }
                if f.is_class(ps, Class::Zone) {
                    if !f.is_class(ps, Class::Highway) {
                        let p = if f.has_context(dest, ps) {
                            self.safe_pos(cx, 664, ps, false, None, dest, safesize * 2.0, Some(safesize * 3.0), false)?
                        } else {
                            self.safe_pos(cx, 671, ps, false, None, Uid::NULL, safesize * 2.0, None, false)?
                        };
                        self.position = Some(p);
                        self.dbg(cx, 677)?;
                    } else {
                        self.dbg(cx, 680)?;
                    }
                } else if f.is_class(ps, Class::Sector) {
                    let p = if f.has_context(dest, ps) {
                        self.safe_pos(cx, 685, ps, true, None, dest, safesize * 2.0, Some(size * 3.0), false)?
                    } else {
                        self.safe_pos(cx, 692, ps, true, None, Uid::NULL, safesize * 2.0, Some(size * 3.0), false)?
                    };
                    self.position = Some(p);
                    self.dbg(cx, 698)?;
                } else if ps == dest {
                    let dz = f.uid_of(dest, Prop::Zone);
                    let p = self.safe_pos(cx, 702, dz, false, None, dest, safesize, None, true)?;
                    let p = self.create_position(cx, 707, dest, p, dz)?;
                    self.position = Some(p);
                    self.dbg(cx, 708)?;
                }
                // else L711: error output without chance
            }
            Some(pos) => {
                if self.pos_initial.is_none() {
                    self.pos_initial = Some(Some(pos));
                }
                if !self.p().precise {
                    let player = self.p().playerprecise;
                    let (lsec, lzone, lobj, lcp, ldbg) =
                        if player { (722, 729, 736, 741, 742) } else { (750, 757, 764, 769, 770) };
                    let half = safesize / 2.0;
                    if f.is_class(ps, Class::Sector) {
                        let max = if player { None } else { Some(size * 3.0) };
                        self.position =
                            Some(self.safe_pos(cx, lsec, ps, true, Some(pos), Uid::NULL, half, max, false)?);
                    } else if f.is_class(ps, Class::Zone) {
                        let max = if player { None } else { Some(safesize * 3.0) };
                        self.position =
                            Some(self.safe_pos(cx, lzone, ps, false, Some(pos), Uid::NULL, half, max, false)?);
                    } else if ps == dest {
                        let dz = f.uid_of(dest, Prop::Zone);
                        let p = self.safe_pos(cx, lobj, dz, false, Some(pos), dest, half, None, true)?;
                        self.position = Some(self.create_position(cx, lcp, dest, p, dz)?);
                        self.dbg(cx, ldbg)?;
                    }
                    // else: error output without chance
                }
                // L777
                if self.position != self.pos_initial.unwrap_or(None) {
                    self.dbg(cx, 778)?;
                }
            }
        }
        Ok(())
    }

    /// The long-distance test of L794.
    fn long_distance(&self, f: &dyn Facts, ent: Uid) -> bool {
        let ship = self.ship;
        let ps = self.positionspace.unwrap_or(Uid::NULL);
        let this_sector = f.uid_of(ent, Prop::Sector);
        let this_zone = f.uid_of(ent, Prop::Zone);
        if this_sector != self.targetsector {
            return true;
        }
        if self.p().traveltoend && !self.endintargetzone {
            if let Some(pos) = self.position {
                if f.distance_to_pos(ship, ps, pos) > f.length(ship, Prop::Size) {
                    return true;
                }
            }
        }
        this_zone != self.targetzone
            && (self.endzone.is_none_or(|z| z.is_null()) || this_zone != self.endzone.unwrap_or(Uid::NULL))
            && (self.endintargetzone
                || f.is_class(self.targetzone, Class::Highway)
                || f.bbox_distance_to_pos(ship, ps, self.position.unwrap_or_default(), self.p().destination) > TEN_KM)
    }

    fn jump_path(&self, cx: &mut Cx<'_>, line: u32, blacklist: bool) -> R<Vec<Uid>> {
        let q = Query::JumpPath {
            start: self.ship,
            end: self.targetsector,
            uselocalhighways: self.canuselocalhighways,
            useblacklist: if blacklist { self.blacklistgroup.clone() } else { Val::Null },
            refobject: if blacklist { self.ship } else { Uid::NULL },
            useknownpath: cx.facts.flag(cx.ent, Prop::IsPlayerOwned),
        };
        match cx.query(line, q)? {
            Answer::Path(p) => Ok(p),
            other => Err(Stop::Mismatch(format!("line {line}: expected a path, got {other:?}"))),
        }
    }

    /// `move_to` with an `endzone=` result.
    fn move_to(&self, cx: &mut Cx<'_>, line: u32, pos: Pos, flags: Vec<(&'static str, Val)>) -> R<Uid> {
        let q = Query::MoveTo {
            object: self.ship,
            destination: self.positionspace.unwrap_or(Uid::NULL),
            pos: Some(pos),
            flags,
        };
        match cx.query(line, q)? {
            Answer::Component(z) | Answer::Zone(z) => Ok(z),
            other => Err(Stop::Mismatch(format!("line {line}: expected the move's endzone, got {other:?}"))),
        }
    }

    fn exact_wait(&mut self, cx: &mut Cx<'_>, d: f64, then: Pc) -> Yield {
        self.pc = then;
        Yield::Wait { until: Some(cx.now() + d) }
    }

    pub fn resume(&mut self, cx: &mut Cx<'_>, wake: Wake) -> R<Yield> {
        let ship = self.ship;
        match (&wake, self.pc) {
            (Wake::Start, Pc::Init) | (Wake::Restart, _) => {}
            (
                Wake::Timer,
                Pc::AfterCheckWait
                | Pc::AfterMoveWait
                | Pc::AfterRecallWait
                | Pc::AfterStagger
                | Pc::AfterResetWait1
                | Pc::AfterResetWait2,
            ) => {}
            (Wake::Block, Pc::AfterMoveWait | Pc::AfterLongMove | Pc::AfterShortMove) => {}
            (Wake::Returned(_), Pc::AfterGate) => {}
            _ => {
                return Err(Stop::Mismatch(format!(
                    "{SCRIPT}: wake {} does not fit continuation {:?}",
                    wake.name(),
                    self.pc
                )))
            }
        }
        self.blocked = None;
        loop {
            let f = cx.facts;
            match self.pc {
                Pc::Init => {
                    self.ensure_params(f)?;
                    self.returnvalue = false; // L355
                    self.get_blacklistgroup(cx)?; // L357
                    self.pos_input = self.position; // L359
                    self.pc = Pc::Start;
                }
                Pc::Start => {
                    self.last_label = Label::Start; // L361
                    cx.fx(364, Effect::DefaultFlightControlModel { object: ship })?;
                    self.dbg(cx, 366)?;
                    if self.p().disablecollisionavoidance {
                        cx.fx(368, Effect::SetAvoidCollisions { object: ship })?;
                    }
                    self.bigship = cx.facts.flag(ship, Prop::IsCapitalShip); // L370
                    self.check_local_highways(cx, true)?; // L371-374
                                                          // L376-378: warning without chance
                    self.pc = Pc::Check;
                }
                Pc::Check => {
                    self.last_label = Label::Check; // L380
                    return Ok(self.exact_wait(cx, 0.001, Pc::AfterCheckWait)); // L382
                }
                Pc::AfterCheckWait => {
                    let ent = cx.ent;
                    if self.p().endintargetsector && self.in_target_sector(f, ent) {
                        self.dbg(cx, 385)?;
                        self.pc = Pc::Finish;
                        continue;
                    }
                    if self.destination_gone(f) {
                        self.dbg(cx, 390)?;
                        self.pc = Pc::Cleanup;
                        continue;
                    }
                    let sod = self.p().stopondetect;
                    if f.exists(sod)
                        && f.can_see(ship, sod)
                        && (!self.endintargetzone || f.uid_of(ent, Prop::Zone) == f.uid_of(sod, Prop::Zone))
                    {
                        self.dbg(cx, 395)?;
                        self.returnvalue = true;
                        self.pc = Pc::Cleanup;
                        continue;
                    }
                    self.dbg(cx, 400)?;
                    self.pc = self.input_case(cx)?;
                }
                Pc::Move => {
                    self.last_label = Label::Move; // L608
                    let dz = f.uid_of(self.p().destination, Prop::Zone);
                    let d = if f.is_class(dz, Class::Highway) { 120.0 } else { 0.001 }; // L612-615
                    let y = self.exact_wait(cx, d, Pc::AfterMoveWait); // L616 (with an interrupt)
                    self.blocked = Some(616);
                    return Ok(y);
                }
                Pc::AfterMoveWait => {
                    let dz = f.uid_of(self.p().destination, Prop::Zone);
                    if f.is_class(dz, Class::Highway) {
                        self.dbg(cx, 628)?;
                        self.pc = Pc::Move;
                        continue;
                    }
                    if self.destination_gone(f) {
                        self.dbg(cx, 634)?;
                        self.pc = Pc::Cleanup;
                        continue;
                    }
                    if self.targetsector.is_null() {
                        self.dbg(cx, 639)?;
                    }
                    if self.p().changecommandaction {
                        cx.fx(644, Effect::SetCommandAction { action: "commandaction.calculating" })?;
                    }
                    self.renew_reservation(cx, 648, Some(649))?;
                    self.plot_position(cx)?;
                    self.dbg(cx, 782)?;
                    if cx.facts.uid_of(cx.ent, Prop::Sector).is_null() {
                        self.dbg(cx, 785)?;
                    }
                    self.dbg(cx, 788)?;
                    if self.long_distance(cx.facts, cx.ent) {
                        self.dbg(cx, 795)?;
                        // L796-798: error output without chance
                        self.intersector = false; // L800
                        let this_sector = cx.facts.uid_of(cx.ent, Prop::Sector);
                        if !self.targetsector.is_null() && !this_sector.is_null() && this_sector != self.targetsector {
                            if let Some(pc) = self.inter_sector_path(cx)? {
                                self.pc = pc;
                                continue;
                            }
                        }
                        if let Some(y) = self.recall_subordinates(cx)? {
                            return Ok(y);
                        }
                        self.pc = Pc::AfterRecallWait;
                    } else {
                        self.pc = Pc::Short;
                    }
                }
                Pc::AfterRecallWait => {
                    // L921-926: locals of the recall removed; L928
                    if self.destination_gone(f) {
                        self.dbg(cx, 929)?;
                        self.pc = Pc::Cleanup;
                        continue;
                    }
                    // L933-940: wait for subordinates in formation or escorting us
                    self.waitforsubordinates = false;
                    for sub in f.get(ship, Prop::Subordinates).as_uid_list().unwrap_or_default() {
                        let docked_here = f.flag(sub, Prop::Docked) && f.has_context(sub, ship);
                        let follows = f.uid_of(sub, Prop::FormationLeader) == ship || {
                            let o = f.order_of(sub);
                            let k = f.order_kind(o);
                            (k == "Escort" || k == "SupplyFleet")
                                && f.order_get(o, Prop::OrderTarget).as_uid() == Some(ship)
                        };
                        if f.flag(sub, Prop::IsOperational) && !docked_here && follows {
                            self.waitforsubordinates = true;
                            break;
                        }
                    }
                    let this_zone = f.uid_of(cx.ent, Prop::Zone);
                    let gate_path = self.intersector
                        && !f.is_class(this_zone, Class::Highway)
                        && (self.waitforsubordinates
                            || !self.p().waitforatgate.is_null()
                            || f.flag(ship, Prop::IsCapitalShip));
                    if gate_path {
                        // L952
                        if self.jumppath.len() < 2 {
                            self.dbg(cx, 953)?;
                            // L954-956: debug output without chance per entry
                            self.pc = Pc::Cleanup;
                            continue;
                        }
                        let first = self.jumppath[0];
                        let mut gate = Uid::NULL;
                        if f.is_class(first, Class::Gate) || f.is_class(first, Class::HighwayEntryGate) {
                            gate = first;
                        }
                        if !f.exists(gate) {
                            // L966-969: error output without chance
                            self.pc = Pc::Cleanup;
                            continue;
                        }
                        let gatedestination = f.uid_of(gate, Prop::Destination);
                        // L972-977
                        let mut nextpos = Val::List(vec![Val::Null, Val::Pos(Pos::default())]);
                        if self.p().waitforatgate.is_null() {
                            nextpos = Val::List(vec![
                                Val::opt_uid(self.positionspace),
                                self.position.map(Val::Pos).unwrap_or(Val::Null),
                            ]);
                            if self.jumppath.len() >= 3 {
                                nextpos = Val::List(vec![Val::uid(self.jumppath[2]), Val::Pos(Pos::default())]);
                            }
                        }
                        self.dbg(cx, 979)?;
                        self.nextsector = Some(cx.facts.uid_of(gatedestination, Prop::Sector)); // L981
                        cx.debug(983, 0.0)?; // chance="(...) * 0"
                        let params = vec![
                            ("gate", Val::uid(gate)),
                            ("recallsubordinates", Val::Bool(false)),
                            ("waitfor", Val::uid(self.p().waitforatgate)),
                            ("noboost", Val::Bool(self.p().noboost)),
                            ("nextpos", nextpos),
                            ("debugchance", Val::F32(self.p().debugchance)),
                        ];
                        cx.fx(984, Effect::RunScript { name: "move.gate", params: params.clone() })?;
                        self.pc = Pc::AfterGate;
                        return Ok(Yield::Call { script: "move.gate", params });
                    }
                    // L1018-1022: small-ship stagger wait
                    self.locblacklistgroup = if self.intersector && !self.blacklistoverride {
                        self.blacklistgroup.clone()
                    } else {
                        Val::Null
                    };
                    let max = if self.bigship {
                        0.0f32
                    } else {
                        // (not $bigship) * (5.1 - (this.ship.pilot.skill.piloting / 3))s: integer division,
                        // then a script float (f32) converted to time.
                        let skill = f.get(ship, Prop::PilotSkillPiloting).as_int().unwrap_or(0);
                        5.1f32 - (skill / 3) as f32
                    };
                    let d = cx.range(1022, NumKind::F64, 0.0, max as f64)?;
                    self.pc = Pc::AfterStagger;
                    return Ok(Yield::Wait { until: Some(cx.now() + d) });
                }
                Pc::AfterGate => {
                    // L995-1001 (L996-998: error output without chance)
                    self.nextsector = None;
                    // L1005-1015: arriving without a further gate, not waiting: the next long move does not
                    // abort the path (the move_to inside `do_if value="false"` never runs)
                    let waitop = cx.facts.flag(self.p().waitforatgate, Prop::IsOperational);
                    let this_zone = cx.facts.uid_of(cx.ent, Prop::Zone);
                    if self.jumppath.len() < 3
                        && !self.waitforsubordinates
                        && !waitop
                        && !cx.facts.is_class(this_zone, Class::Highway)
                    {
                        self.locabortpath = Some(false);
                    }
                    self.waitforsubordinates = false; // L1016
                    self.pc = Pc::LongTail;
                }
                Pc::AfterStagger => {
                    self.check_local_highways(cx, false)?; // L1024
                    if self.resetsignalled {
                        self.resetsignalled = false;
                        return Ok(self.exact_wait(cx, 0.001, Pc::AfterResetWait1));
                        // L1027
                    }
                    self.pc = Pc::LongMove;
                }
                Pc::AfterResetWait1 => self.pc = Pc::LongMove,
                Pc::LongMove => {
                    if self.p().debugchance > 0.0 {
                        return Err(unsupported(1030, "debug position output (debugchance > 0)"));
                    }
                    if self.locabortpath.is_none() {
                        self.locabortpath = Some(self.p().abortpath);
                    }
                    self.endzone = None; // L1047
                    self.dbg(cx, 1048)?;
                    let Some(pos) = self.position else {
                        return Err(unsupported(1072, "long-distance move without a position (highway destination)"));
                    };
                    let flags = vec![
                        ("uselocalhighways", Val::Bool(self.canuselocalhighways)),
                        ("useknownpath", Val::Bool(self.p().useknownpath)),
                        ("abortpath", Val::Bool(self.locabortpath.unwrap_or(true))),
                        ("forcesteering", Val::Bool(!self.bigship && self.p().forcesteering)),
                        ("forceposition", Val::Bool(false)),
                        ("travel", Val::Bool(!self.p().noboost)),
                        ("useblacklist", self.locblacklistgroup.clone()),
                        ("commandaction", Val::Bool(self.p().changecommandaction)),
                    ];
                    self.endzone = Some(self.move_to(cx, 1051, pos, flags)?);
                    self.pc = Pc::AfterLongMove;
                    self.blocked = Some(1051);
                    return Ok(Yield::MoveTo);
                }
                Pc::AfterLongMove => {
                    self.dbg(cx, 1065)?;
                    self.locabortpath = None;
                    self.locblacklistgroup = Val::Null;
                    if self.endzone.is_some_and(|z| z.is_null()) {
                        self.dbg(cx, 1097)?;
                        self.failedattempts += 1;
                        if self.failedattempts >= 3 {
                            self.dbg(cx, 1100)?;
                            self.pc = Pc::Cleanup;
                            continue;
                        }
                        self.dbg(cx, 1103)?;
                        self.position = None;
                        self.pc = Pc::Move;
                        continue;
                    }
                    self.pc = Pc::LongTail;
                }
                Pc::LongTail => {
                    // L1109-1115
                    if self.resetstrictblacklist {
                        self.strictblacklist = !self.strictblacklist;
                        self.resetstrictblacklist = false;
                    }
                    self.blacklistoverride = false;
                    self.intersector = false;
                    self.pc = Pc::Check;
                }
                Pc::Short => {
                    self.dbg(cx, 1118)?;
                    if self.destination_gone(f) {
                        self.dbg(cx, 1122)?;
                        self.pc = Pc::Cleanup;
                        continue;
                    }
                    let ps = self.positionspace.unwrap_or(Uid::NULL);
                    if let Some(ez) = self.endzone {
                        if f.exists(ez)
                            && !f.is_class(self.targetzone, Class::Highway)
                            && f.is_class(ps, Class::Sector)
                            && f.has_context(ez, ps)
                        {
                            self.dbg(cx, 1130)?;
                            self.targetzone = ez;
                        }
                        // else L1134: error output without chance
                    }
                    if self.targetzone.is_null() {
                        if cx.facts.is_class(ps, Class::Sector) {
                            let value = self.position.unwrap_or_default();
                            self.targetzone = cx.zone_query(1141, Query::ZoneAtPosition { sector: ps, value })?;
                        } else {
                            cx.debug(1144, 0.0)?;
                        }
                        if !self.targetzone.is_null() {
                            self.dbg(cx, 1147)?;
                        } else {
                            self.dbg(cx, 1152)?;
                        }
                    }
                    let ent = cx.ent;
                    if self.p().endintargetsector && self.in_target_sector(cx.facts, ent) {
                        self.dbg(cx, 1158)?;
                        self.pc = Pc::Finish;
                        continue;
                    }
                    if self.endintargetzone && self.targetzone == cx.facts.uid_of(ent, Prop::Zone) {
                        self.dbg(cx, 1162)?;
                        self.pc = Pc::Finish;
                        continue;
                    }
                    if !self.p().disablecollisionavoidance {
                        cx.fx(1168, Effect::ResetAvoidCollisions { object: ship })?;
                    }
                    cx.debug(1171, 0.0)?;
                    if !self.targetzone.is_null() {
                        if cx.facts.is_class(self.targetzone, Class::Highway) {
                            self.dbg(cx, 1176)?;
                            self.pc = Pc::Finish;
                            continue;
                        }
                        self.renew_reservation(cx, 1181, None)?;
                        cx.debug(1187, 0.0)?;
                        let tz = self.targetzone;
                        let value = self.position.unwrap_or_default();
                        let safesize = cx.facts.length(ship, Prop::SafeSize);
                        let mut sp = self.safe_pos(cx, 1188, tz, false, Some(value), ps, safesize, None, false)?;
                        if self.p().precise {
                            sp = self.create_position(cx, 1194, tz, value, ps)?;
                        }
                        let far = |c: f32| !(-50000.0..=50000.0).contains(&c);
                        if far(sp.x) || far(sp.y) || far(sp.z) {
                            cx.debug(1197, 0.0)?;
                        } else {
                            self.dbg(cx, 1200)?;
                        }
                        self.safepos = Some(sp);
                        if self.p().relativemovement {
                            return Err(unsupported(1205, "relativemovement"));
                        }
                        self.position = Some(self.create_position(cx, 1211, tz, value, ps)?);
                        self.positionspace = Some(tz);
                        self.dbg(cx, 1214)?;
                    } else {
                        cx.debug(1217, 0.0)?;
                        self.safepos = self.position;
                    }
                    cx.debug(1223, 0.0)?;
                    if !self.p().rotation && self.p().lookat {
                        return Err(unsupported(1225, "lookat orientation"));
                    }
                    self.check_local_highways(cx, false)?; // L1234
                    if self.resetsignalled {
                        self.resetsignalled = false;
                        return Ok(self.exact_wait(cx, 0.001, Pc::AfterResetWait2));
                        // L1237
                    }
                    self.pc = Pc::ShortMove;
                }
                Pc::AfterResetWait2 => self.pc = Pc::ShortMove,
                Pc::ShortMove => {
                    self.dbg(cx, 1239)?;
                    self.dbg(cx, 1240)?;
                    if self.p().rotation {
                        return Err(unsupported(1251, "move with a target rotation"));
                    }
                    let ps = self.positionspace.unwrap_or(Uid::NULL);
                    let sp = self.safepos.unwrap_or_default();
                    if cx.facts.distance_to_pos(ship, ps, sp) > cx.facts.length(ship, Prop::Size) {
                        let rel = self.p().relativemovement;
                        let flags = vec![
                            ("uselocalhighways", Val::Bool(self.canuselocalhighways)),
                            ("abortpath", Val::Bool(self.p().abortpath)),
                            ("forcesteering", Val::Bool(rel || (!self.bigship && self.p().forcesteering))),
                            ("forceposition", Val::Bool(false)),
                            ("forcerotation", Val::Bool(false)),
                            ("finishonapproach", Val::Bool(false)),
                            ("travel", Val::Bool(!self.p().noboost && !rel)),
                            ("relativemovement", Val::Bool(rel)),
                            ("commandaction", Val::Bool(self.p().changecommandaction)),
                        ];
                        self.locendzone = Some(self.move_to(cx, 1244, sp, flags)?);
                        self.pc = Pc::AfterShortMove;
                        return Ok(Yield::MoveTo);
                    }
                    self.pc = Pc::AfterShortMove;
                }
                Pc::AfterShortMove => {
                    if self.locendzone.is_some_and(|z| z.is_null()) {
                        self.locendzone = None;
                        self.dbg(cx, 1260)?;
                        self.failedattempts += 1;
                        if self.failedattempts >= 3 {
                            self.dbg(cx, 1263)?;
                            self.pc = Pc::Cleanup;
                            continue;
                        }
                        self.dbg(cx, 1266)?;
                        self.position = None;
                        self.pc = Pc::Move;
                        continue;
                    }
                    self.locendzone = None;
                    // L1273-1284: diagnostics without chance
                    self.pc = Pc::Finish;
                }
                Pc::Finish => {
                    self.last_label = Label::Finish; // L1286
                    let dest = self.p().destination;
                    let ent = cx.ent;
                    let container = f.uid_of(dest, Prop::Container);
                    let dsec = f.uid_of(dest, Prop::Sector);
                    let shiplike = f.is_class(dest, Class::Ship) || f.is_class(container, Class::Ship);
                    if shiplike && (!self.p().endintargetsector || dsec.is_null() || !f.has_context(ent, dsec)) {
                        if self.p().endintargetsector && !dsec.is_null() && !f.has_context(ent, dsec) {
                            self.dbg(cx, 1292)?;
                            self.endzone = None;
                            self.pc = Pc::Check;
                            continue;
                        }
                        let locdist = match self.pos_input {
                            Some(p) => {
                                f.get_args(ship, Prop::BboxDistanceToPos, &[Val::uid(dest), Val::Pos(p)]).as_f32()
                            }
                            None => f.pair(ship, Prop::BboxDistanceTo, dest).as_f32(),
                        }
                        .unwrap_or(0.0);
                        if f.uid_of(ent, Prop::Zone) != f.uid_of(dest, Prop::Zone) && locdist > TEN_KM {
                            self.dbg(cx, 1303)?;
                            self.endzone = None;
                            self.pc = Pc::Check;
                            continue;
                        }
                        self.dbg(cx, 1310)?;
                    }
                    if self.p().stayatrelativemovement {
                        return Err(unsupported(1315, "stayatrelativemovement"));
                    }
                    self.returnvalue = true;
                    self.pc = Pc::Cleanup;
                }
                Pc::Cleanup => {
                    self.last_label = Label::Cleanup; // L1326
                    if let Some(r) = &self.reservedspace {
                        if !r.zone.is_null() {
                            let (index, zone) = (r.index.clone(), r.zone);
                            cx.fx(1329, Effect::CancelSpaceReservation { component: ship, index, zone })?;
                        }
                    }
                    cx.fx(1331, Effect::ResetAvoidCollisions { object: ship })?;
                    cx.fx(1332, Effect::DefaultFlightControlModel { object: ship })?;
                    cx.fx(1333, Effect::StopBoost { object: ship })?;
                    if self.p().recallsubordinates
                        && !matches!(cx.facts.get(cx.ent, Prop::RecallingSubordinates), Val::Null)
                    {
                        return Err(unsupported(1336, "cancel the orders of recalled subordinates"));
                    }
                    // L1355-1363: assert only (debugchance > 0), no draw
                    if !self.returnvalue {
                        // L1366-1369: diagnostics without chance
                        let f = cx.facts;
                        let o = f.order_of(ship);
                        if f.order_running(o) && f.is_class(self.p().destination, Class::Ship) {
                            self.dbg(cx, 1371)?;
                        }
                    }
                    self.pc = Pc::Done;
                    let rv: RetVals = vec![("v".into(), Val::Bool(self.returnvalue))];
                    return Ok(Yield::Return(rv));
                }
                Pc::Done => return Err(Stop::Mismatch(format!("{SCRIPT} resumed after it ended"))),
            }
        }
    }

    /// L403-605: the three input cases; returns where to continue (Move, Finish or Cleanup).
    fn input_case(&mut self, cx: &mut Cx<'_>) -> R<Pc> {
        let ship = self.ship;
        let ent = cx.ent;
        let dest = self.p().destination;
        let f = cx.facts;
        if f.is_class(dest, Class::Zone) {
            // INPUT CASE 1
            if f.is_class(dest, Class::Highway) {
                self.dbg(cx, 406)?;
                if !self.canuselocalhighways && cx.facts.flag(dest, Prop::IsLocalHighway) {
                    // L410: error output without chance
                    return Ok(Pc::Cleanup);
                }
                let f = cx.facts;
                self.targetsector = f.uid_of(f.uid_of(dest, Prop::Destination), Prop::Sector);
                self.targetzone = dest;
                self.endintargetzone = true;
                self.position = None;
                self.positionspace = Some(dest);
                return Ok(Pc::Move);
            }
            self.targetsector = f.uid_of(dest, Prop::Sector);
            if f.flag(dest, Prop::IsTempZone) {
                self.targetzone = Uid::NULL;
                self.endintargetzone = false;
                if self.positionspace.is_none() {
                    let ts = self.targetsector;
                    let p = match self.position {
                        Some(p) => self.create_position(cx, 438, ts, p, dest)?,
                        None => self.create_position(cx, 441, ts, Pos::default(), dest)?,
                    };
                    self.position = Some(p);
                }
                self.positionspace = Some(self.targetsector);
                self.dbg(cx, 445)?;
            } else {
                self.targetzone = dest;
                self.positionspace = Some(dest);
            }
            return Ok(Pc::Move);
        }
        if f.is_class(dest, Class::Sector) || f.is_class(dest, Class::Cluster) {
            // INPUT CASE 2
            if self.pos_input.is_some() && f.is_class(dest, Class::Sector) {
                self.targetsector = dest;
                self.targetzone = Uid::NULL;
                self.endintargetzone = false;
                self.positionspace = Some(dest);
                self.position = self.pos_input;
                return Ok(Pc::Move);
            }
            if f.has_context(ship, dest) {
                self.dbg(cx, 473)?;
                return Ok(Pc::Finish);
            }
            if self.pos_input.is_some() && f.is_class(dest, Class::Cluster) {
                self.position = Some(Pos::default()); // L481; L482 error output without chance
            }
            let mut endsector = dest;
            if cx.facts.is_class(dest, Class::Cluster) {
                self.dbg(cx, 487)?;
                endsector = cx.zone_query(488, Query::FindSector { space: dest })?;
            }
            if endsector.is_null() || !cx.facts.is_class(endsector, Class::Sector) {
                // L492: error output without chance
                return Ok(Pc::Cleanup);
            }
            self.dbg(cx, 495)?;
            let line = if self.canuselocalhighways { 497 } else { 503 };
            let path = match cx.query(
                line,
                Query::GlobalPath { start: ship, end: endsector, uselocalhighways: self.canuselocalhighways },
            )? {
                Answer::Path(p) => p,
                other => return Err(Stop::Mismatch(format!("line {line}: expected a path, got {other:?}"))),
            };
            self.dbg(cx, 508)?;
            for c in path {
                self.dbg(cx, 510)?;
                let f = cx.facts;
                if !f.has_context(c, dest) {
                    continue;
                }
                if f.is_class(c, Class::Highway) {
                    continue;
                }
                if f.is_class(c, Class::Zone) && f.flag(c, Prop::HasPriority) {
                    self.targetzone = c;
                    self.dbg(cx, 517)?;
                    break;
                }
                if f.is_class(c, Class::Object) {
                    self.targetzone = f.uid_of(c, Prop::Zone);
                    self.dbg(cx, 522)?;
                    break;
                }
            }
            let f = cx.facts;
            if !self.targetzone.is_null() {
                self.targetsector = f.uid_of(self.targetzone, Prop::Sector);
                self.positionspace = Some(self.targetzone);
            } else {
                self.targetsector = endsector;
                self.targetzone = Uid::NULL;
                self.endintargetzone = false;
                self.positionspace = Some(endsector);
            }
            return Ok(Pc::Move);
        }
        // INPUT CASE 3: an object (a position, if given, is relative to it)
        let dz = f.uid_of(dest, Prop::Zone);
        if f.is_class(dz, Class::Highway) {
            self.dbg(cx, 547)?;
            let f = cx.facts;
            if self.canuselocalhighways || f.uid_of(dz, Prop::Sector).is_null() {
                self.targetsector = Uid::NULL;
                self.targetzone = dz;
                if self.endintargetzone_input.is_none() {
                    self.endintargetzone_input = Some(self.endintargetzone);
                }
                self.endintargetzone = true;
                self.positionspace = Some(dz);
                self.position = None;
            } else {
                return Err(unsupported(561, "wait for an object to leave a local highway"));
            }
        } else {
            self.targetzone = dz;
            self.targetsector = f.uid_of(dest, Prop::Sector);
            self.positionspace = Some(self.targetsector);
            if f.is_class(dest, Class::Ship) && !f.flag(dest, Prop::Docked) && f.has_context(ent, self.targetsector) {
                self.dbg(cx, 584)?;
                self.positionspace = Some(dest);
            }
            let ps = self.positionspace.unwrap_or(Uid::NULL);
            let p = match self.pos_input {
                Some(p) => self.create_position(cx, 590, ps, p, dest)?,
                None => self.create_position(cx, 595, ps, Pos::default(), dest)?,
            };
            self.position = Some(p);
            if let Some(e) = self.endintargetzone_input {
                self.endintargetzone = e;
            }
        }
        let f = cx.facts;
        let escort = self.p().escort;
        if !self.p().pursuetargets
            && self.targetsector != f.uid_of(ent, Prop::Sector)
            && (escort.is_null()
                || f.pair(escort, Prop::BboxDistanceTo, dest).as_f32().unwrap_or(0.0) > self.p().pursuedistance)
        {
            self.dbg(cx, 603)?;
            return Ok(Pc::Cleanup);
        }
        Ok(Pc::Move)
    }

    /// L803-878: the jump path to the target sector. `Some(pc)` when the script leaves the block.
    fn inter_sector_path(&mut self, cx: &mut Cx<'_>) -> R<Option<Pc>> {
        let ship = self.ship;
        if self.resetstrictblacklist {
            self.strictblacklist = !self.strictblacklist;
            self.resetstrictblacklist = false;
        }
        self.blacklistoverride = false;
        self.intersector = true;
        let f = cx.facts;
        let commander = f.uid_of(ship, Prop::Commander);
        if self.strictblacklist && !commander.is_null() && self.targetsector == f.uid_of(commander, Prop::Sector) {
            self.strictblacklist = false;
            self.resetstrictblacklist = true;
        }
        if f.gate_distance(ship, self.p().destination) < 0 {
            // L820: error output without chance
            let cluster = f.uid_of(cx.ent, Prop::Cluster);
            let normal = f.flag(cluster, Prop::IsNormalZone) || f.is_class(cluster, Class::Cluster);
            if normal && f.flag(ship, Prop::IsJobShip) && !f.flag(ship, Prop::Docked) {
                return Err(unsupported(821, "job ship without a path: recovery warp"));
            }
            return Ok(Some(Pc::Cleanup));
        }
        let line = if self.canuselocalhighways { 845 } else { 851 };
        self.jumppath = self.jump_path(cx, line, true)?;
        if self.jumppath.len() < 2 {
            if self.strictblacklist {
                return Err(unsupported(859, "strict blacklist without a path"));
            }
            self.dbg(cx, 860)?;
            let line = if self.canuselocalhighways { 862 } else { 868 };
            self.jumppath = self.jump_path(cx, line, false)?;
            self.blacklistoverride = true;
        }
        Ok(None)
    }

    /// L880-926: recall subordinates before a long trip. Returns the L920 wait when it ran.
    fn recall_subordinates(&mut self, cx: &mut Cx<'_>) -> R<Option<Yield>> {
        let ship = self.ship;
        let f = cx.facts;
        let ps = self.positionspace.unwrap_or(Uid::NULL);
        // L880-891
        let mut loctargetzone = Uid::NULL;
        if f.is_class(ps, Class::Zone) {
            loctargetzone = ps;
        } else if f.is_class(ps, Class::Sector) {
            let value = self.position.unwrap_or_default();
            loctargetzone = cx.zone_query(887, Query::ZoneAtPosition { sector: ps, value })?;
        } else {
            let z = f.uid_of(ps, Prop::Zone);
            if !z.is_null() {
                loctargetzone = z;
            }
        }
        let f = cx.facts;
        let subs = f.get(ship, Prop::Subordinates).as_uid_list().unwrap_or_default();
        let o = f.order_of(ship);
        let critical = f.order_get(o, Prop::OrderState).as_key() == Some("orderstate.critical");
        let priority = f.order_get(o, Prop::HasPriority).as_bool().unwrap_or(false);
        let recalling = !matches!(f.get(cx.ent, Prop::RecallingSubordinates), Val::Null);
        if !(self.p().recallsubordinates && !subs.is_empty() && !critical && !priority && !recalling) {
            return Ok(None);
        }
        let player = f.flag(cx.ent, Prop::IsPlayerOwned);
        if player && !loctargetzone.is_null() {
            if loctargetzone == f.uid_of(cx.ent, Prop::Zone) {
                return Ok(None);
            }
            return Err(unsupported(894, "adjacent zones of a player-owned ship"));
        }
        let recall = subs.iter().any(|&sub| {
            let allowed = f.pair(ship, Prop::DockingAllowed, sub).as_bool().unwrap_or(false);
            f.flag(sub, Prop::IsOperational) && allowed && (!f.flag(sub, Prop::Docked) || !f.has_context(sub, ship))
        });
        if !recall {
            return Ok(None);
        }
        let dest = self.p().destination;
        let (locdest, locpos) = if f.is_class(dest, Class::Cluster) {
            (Val::opt_uid(self.positionspace), self.position.map(Val::Pos).unwrap_or(Val::Null))
        } else {
            (Val::uid(dest), Val::Pos(self.pos_input.unwrap_or_default()))
        };
        self.dbg(cx, 913)?;
        cx.fx(
            914,
            Effect::CreateOrder {
                id: "RecallSubordinates",
                object: ship,
                immediate: true,
                params: vec![
                    ("destination", Val::List(vec![locdest, locpos])),
                    ("activepatrol", Val::Bool(self.p().activepatrol)),
                    ("debugchance", Val::F32(self.p().debugchance)),
                ],
            },
        )?;
        Ok(Some(self.exact_wait(cx, 1.0, Pc::AfterRecallWait)))
    }

    /// Whether global handler `i` consumes the event (`consume="false"` lets it go on to lower frames).
    pub fn consumes(i: usize) -> bool {
        Self::HANDLERS[i] != "inline2"
    }

    /// Conditions of global handler `i` (`None` = not ported: the engine verdict decides). They may
    /// query the engine (count_gravidar_contacts) and set variables.
    pub fn matches(&mut self, cx: &mut Cx<'_>, i: usize, ev: &Event) -> R<Option<bool>> {
        let ship = self.ship;
        self.ensure_params(cx.facts)?;
        let p = self.p().clone();
        let f = cx.facts;
        Ok(Some(match Self::HANDLERS[i] {
            // L97-100
            "inline1" => {
                (ev.name == "event_object_target_invalid" && ev.object == ship)
                    || (ev.name == "event_object_signalled"
                        && ev.object == ship
                        && ev.param == Val::Str("move.generic_reinitialize".into()))
            }
            // L110
            "inline2" => ev.name == "event_object_destroyed" && !p.destination.is_null() && ev.object == p.destination,
            // L119-158
            "inline3" => {
                let listener = if p.activepatrol || f.exists(p.stopondetect) { ship } else { Uid::NULL };
                if ev.name != "event_gravidar_has_scanned" || listener.is_null() || ev.object != listener {
                    return Ok(Some(false));
                }
                let this_zone = f.uid_of(cx.ent, Prop::Zone);
                if f.uid_of(cx.ent, Prop::Sector).is_null() || f.is_class(this_zone, Class::Highway) {
                    return Ok(Some(false));
                }
                let o = f.order_of(ship);
                if o.is_null() || f.order_get(o, Prop::OrderState).as_key() == Some("orderstate.critical") {
                    return Ok(Some(false));
                }
                // check_any of L124-154
                let sod = p.stopondetect;
                let mut any = false;
                if f.exists(sod)
                    && (!self.endintargetzone || this_zone == f.uid_of(sod, Prop::Zone))
                    && f.can_see(ship, sod)
                {
                    self.attacktarget = sod;
                    self.stop_flag = true;
                    any = true;
                }
                if !any && p.activepatrol {
                    let anchored = p.radius.is_some() && p.radiusanchorpos.is_some() && !p.radiusanchorspace.is_null();
                    if !p.enforceradius || !anchored {
                        let q = Query::CountGravidarContacts {
                            object: ship,
                            classes: p.targetclasses.clone(),
                            maybeattackedby: ship,
                            within: None,
                        };
                        self.attacktarget = component(cx.query(133, q)?, 133)?;
                        any = !self.attacktarget.is_null();
                    }
                    if !any && anchored {
                        let q = Query::CountGravidarContacts {
                            object: ship,
                            classes: p.targetclasses.clone(),
                            maybeattackedby: ship,
                            within: Some((
                                p.radiusanchorpos.unwrap_or_default(),
                                p.radiusanchorspace,
                                p.radius.unwrap_or(0.0),
                            )),
                        };
                        self.attacktarget = component(cx.query(142, q)?, 142)?;
                        any = !self.attacktarget.is_null();
                    }
                }
                if !any {
                    return Ok(Some(false));
                }
                // check_any of L155-157
                let f = cx.facts;
                self.stop_flag || {
                    let k = f.order_kind(o);
                    k != "Flee"
                        && (k != "Attack"
                            || (f.order_get(o, Prop::OrderAllowOtherTargets).as_bool().unwrap_or(false)
                                && f.order_get(o, Prop::OrderPrimaryTarget).as_uid() != Some(self.attacktarget)))
                        && k != "Board"
                }
            }
            // L185-200
            "inline4" => {
                let ps = self.positionspace.unwrap_or(Uid::NULL);
                let d = p.destination;
                !d.is_null()
                    && ev.object == d
                    && ((ev.name == "event_object_changed_zone"
                        && (d != ps || f.is_class(ev.param.as_uid().unwrap_or(Uid::NULL), Class::Highway)))
                        || (ev.name == "event_object_approaching_waypoint" && d != ps)
                        || (ev.name == "event_object_changed_sector" && d == ps))
            }
            // L216-226
            "inline5" => {
                let ps = self.positionspace.unwrap_or(Uid::NULL);
                let d = p.destination;
                let dsec = f.uid_of(d, Prop::Sector);
                !d.is_null()
                    && ev.object == d
                    && ((ev.name == "event_object_docked" && d == ps)
                        || (ev.name == "event_object_undocked"
                            && !dsec.is_null()
                            && f.has_context(cx.ent, dsec)
                            && d != ps))
            }
            // interrupt.changedsector.xml L7-10
            "SectorChangeHandler" => {
                (ev.name == "event_object_entered_anomaly" || ev.name == "event_object_entered_gate")
                    && ev.object == ship
            }
            // interrupt.lostreservation.xml L8-10
            "LostReservationHandler" => {
                ev.name == "event_space_reservation_expired"
                    && ev.object == ship
                    && self
                        .reservedspace
                        .as_ref()
                        .is_some_and(|r| !r.zone.is_null() && ev.param.as_uid() == Some(r.zone) && ev.param2 == r.index)
            }
            // interrupt.tide.xml L8: reject unrelated events before the opaque conditions.
            // Treating this handler as opaque for every event would disable the delivery RNG
            // comparison even when no opaque condition can have run.
            "TideHandler" => {
                if ev.name == "event_object_signalled"
                    && ev.object == ship
                    && ev.param == Val::Str("tide_phasechange".into())
                {
                    return Ok(None);
                }
                false
            }
            _ => return Ok(None),
        }))
    }

    /// Actions of a matched global handler.
    pub fn handler_actions(&mut self, cx: &mut Cx<'_>, i: usize, ev: &Event) -> R<Outcome> {
        let ship = self.ship;
        match Self::HANDLERS[i] {
            "inline1" => {
                self.dbg(cx, 103)?;
                self.endzone = None;
                Ok(Outcome::AbortCalled { label: Some("check") })
            }
            "inline2" => {
                self.dbg(cx, 113)?;
                Ok(Outcome::AbortCalled { label: Some("cleanup") })
            }
            "inline3" => {
                if self.stop_flag {
                    self.dbg(cx, 161)?;
                    self.stop_flag = false;
                    return Ok(Outcome::AbortCalled { label: Some("finish") });
                }
                cx.debug(165, 0.0)?;
                let f = cx.facts;
                let t = self.attacktarget;
                let tsec = f.uid_of(t, Prop::Sector);
                if !tsec.is_null() && tsec == f.uid_of(cx.ent, Prop::Sector) && f.can_see(ship, t) {
                    self.dbg(cx, 167)?;
                    cx.debug(168, 0.0)?;
                    let p = self.p().clone();
                    let opt_len = |v: Option<f32>| v.map(Val::F32).unwrap_or(Val::Null);
                    let opt_pos = |v: Option<Pos>| v.map(Val::Pos).unwrap_or(Val::Null);
                    cx.fx(
                        169,
                        Effect::CreateOrder {
                            id: "Attack",
                            object: ship,
                            immediate: true,
                            params: vec![
                                ("primarytarget", Val::uid(t)),
                                ("escort", Val::uid(p.escort)),
                                ("pursuetargets", Val::Bool(false)),
                                ("targetclasses", Val::List(p.targetclasses.clone())),
                                ("radius", if p.enforceradius { opt_len(p.radius) } else { Val::Null }),
                                (
                                    "radiusanchorpos",
                                    if p.enforceradius { opt_pos(p.radiusanchorpos) } else { Val::Null },
                                ),
                                (
                                    "radiusanchorspace",
                                    if p.enforceradius { Val::uid(p.radiusanchorspace) } else { Val::Null },
                                ),
                                ("internalorder", Val::Bool(true)),
                                ("debugchance", Val::F32(p.debugchance)),
                            ],
                        },
                    )?;
                }
                Ok(Outcome::Nothing)
            }
            "inline4" => {
                self.dbg(cx, 204)?;
                self.endzone = None;
                let p = self.p().clone();
                let f = cx.facts;
                let near = !p.escort.is_null()
                    && f.is_class(p.destination, Class::Object)
                    && f.pair(p.escort, Prop::BboxDistanceTo, p.destination).as_f32().unwrap_or(f32::INFINITY)
                        <= p.pursuedistance;
                if p.pursuetargets || near {
                    Ok(Outcome::AbortCalled { label: Some("check") })
                } else {
                    Ok(Outcome::AbortCalled { label: Some("cleanup") })
                }
            }
            "inline5" => {
                self.dbg(cx, if ev.name == "event_object_docked" { 230 } else { 233 })?;
                Ok(Outcome::AbortCalled { label: Some("check") })
            }
            "SectorChangeHandler" => {
                // interrupt.changedsector.xml L16: no reset when entering the gate move.gate was sent to
                let p2 = ev.param2.as_uid().unwrap_or(Uid::NULL);
                let newsector = cx.facts.uid_of(p2, Prop::Sector);
                if self.nextsector.is_some_and(|s| !s.is_null() && s == newsector) {
                    return Ok(Outcome::Nothing);
                }
                let dc = self.p().debugchance;
                cx.roll(18, dc)?;
                if cx.facts.flag(ship, Prop::IsPlayerOwned) {
                    let p1 = ev.param.as_uid().unwrap_or(Uid::NULL);
                    let cluster = cx.facts.uid_of(newsector, Prop::Cluster);
                    cx.fx(22, Effect::SetKnown { object: p1 })?;
                    cx.fx(23, Effect::SetKnown { object: p2 })?;
                    cx.fx(24, Effect::SetKnown { object: newsector })?;
                    cx.fx(25, Effect::SetKnown { object: cluster })?;
                }
                if dc != 0.0 {
                    cx.roll(54, dc)?;
                }
                Ok(Outcome::AbortCalled { label: Some("start") })
            }
            "LostReservationHandler" => {
                if let Some(r) = self.reservedspace.as_mut() {
                    r.zone = Uid::NULL;
                    r.index = Val::Null;
                }
                Ok(Outcome::Nothing)
            }
            h => Err(Stop::Mismatch(format!("{SCRIPT}: actions of opaque handler {h} requested"))),
        }
    }

    /// Conditions of the blocking action's `<interrupt>`: L618-621 (wait L616) and L1053-1058 (move_to
    /// L1051). The condition-level debug text at L1056 draws only with X4's debug channel on.
    pub fn block_matches(&mut self, cx: &mut Cx<'_>, ev: &Event) -> R<Option<bool>> {
        let ship = self.ship;
        match self.blocked {
            Some(616) => {
                let d = self.p().destination;
                Ok(Some(
                    ev.name == "event_object_changed_zone"
                        && ev.object == d
                        && !cx.facts.is_class(ev.param.as_uid().unwrap_or(Uid::NULL), Class::Highway),
                ))
            }
            Some(1051) => {
                let ez = self.endzone.unwrap_or(Uid::NULL);
                if ev.name != "event_object_changed_zone" || ev.object != ship || ev.param.as_uid() != Some(ez) {
                    return Ok(Some(false));
                }
                let channel = cx.facts.get(Uid::NULL, Prop::DebugChannel).as_bool().unwrap_or(false);
                let dc = self.p().debugchance;
                let _ = cx.rng.debug_condition(channel, dc);
                Ok(Some(!cx.facts.flag(ship, Prop::IsZoneChangePending) && !self.p().traveltoend))
            }
            _ => Ok(Some(false)),
        }
    }

    /// Actions of the blocking interrupt (both end the blocking action).
    pub fn block_interrupt(&mut self, cx: &mut Cx<'_>, ev: &Event) -> R<Outcome> {
        match self.blocked {
            Some(616) => {
                self.dbg(cx, 623)?;
                Ok(Outcome::BlockDone)
            }
            Some(1051) => {
                self.dbg(cx, 1061)?;
                Ok(Outcome::BlockDone)
            }
            _ => Err(Stop::Mismatch(format!("{SCRIPT}: blocking interrupt {} while not blocked", ev.name))),
        }
    }

    /// `<on_abort>` L1379-1407.
    pub fn on_abort(&mut self, cx: &mut Cx<'_>) -> R<()> {
        let ship = self.ship;
        if self.p.is_none() {
            return Ok(());
        }
        if cx.facts.flag(ship, Prop::IsOperational) {
            if let Some(r) = &self.reservedspace {
                if !r.zone.is_null() {
                    let (index, zone) = (r.index.clone(), r.zone);
                    cx.fx(1382, Effect::CancelSpaceReservation { component: ship, index, zone })?;
                }
            }
            cx.fx(1384, Effect::ResetAvoidCollisions { object: ship })?;
            cx.fx(1385, Effect::DefaultFlightControlModel { object: ship })?;
            cx.fx(1386, Effect::StopBoost { object: ship })?;
            let next = cx.facts.get(ship, Prop::NextOrder).as_order().unwrap_or_default();
            if cx.facts.order_kind(next) != "Flee" {
                cx.fx(1388, Effect::StopMoving { object: ship })?;
            }
        }
        if self.p().recallsubordinates && !matches!(cx.facts.get(cx.ent, Prop::RecallingSubordinates), Val::Null) {
            return Err(unsupported(1393, "cancel the orders of recalled subordinates (on_abort)"));
        }
        Ok(())
    }
}

fn component(a: Answer, line: u32) -> R<Uid> {
    match a {
        Answer::Component(c) | Answer::Zone(c) => Ok(c),
        other => Err(Stop::Mismatch(format!("line {line}: expected a component, got {other:?}"))),
    }
}
