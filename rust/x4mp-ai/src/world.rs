//! What a behaviour can see and do.
//!
//! - [`Facts`]: pure reads of world state (the properties the script reads inside expressions:
//!   classes, parents, order state, job flags, ...). In the ECS they come from x4mp-sim columns and the
//!   frame snapshot; in replay from `fact` records. Facts are inputs and are never compared.
//! - [`Query`]: engine services invoked by script *actions* (`find_zone`, `get_global_path`,
//!   `create_position`, `get_safe_pos`). Their arguments are observable at action level, so replay
//!   compares them; their results (and the RNG steps the engine makes inside them) are inputs.
//! - [`Effect`]: externally visible actions (`move_to`, `create_order`, `signal_objects`, ...), compared.
//! - RNG: script-level draws (`chance=`, random ranges, `<wait min max>`) are made by the behaviour on
//!   the entity [`Stream`] in the script's order; [`Io::roll`] / [`Io::rnd`] record them.

use crate::rng::{NumKind, Stream};
use crate::trace::Record;
use crate::val::{OrderId, Pos, Time, Uid, Val};

/// Engine class (`isrealclass`) with the inheritance that `isclass` uses (RTTI: `U::Highway : U::Zone`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Class {
    Galaxy,
    Cluster,
    Sector,
    Zone,
    Highway,
    Ship,
    ShipXS,
    ShipS,
    ShipM,
    ShipL,
    ShipXL,
    Station,
    BuildStorage,
    Spacesuit,
    /// Abstract `class.defensible` (ships, stations, build storages).
    Defensible,
    Other,
}

impl Class {
    pub fn parse(k: &str) -> Class {
        match k {
            "galaxy" => Class::Galaxy,
            "cluster" => Class::Cluster,
            "sector" => Class::Sector,
            "zone" => Class::Zone,
            "highway" => Class::Highway,
            "ship" => Class::Ship,
            "ship_xs" => Class::ShipXS,
            "ship_s" => Class::ShipS,
            "ship_m" => Class::ShipM,
            "ship_l" => Class::ShipL,
            "ship_xl" => Class::ShipXL,
            "station" => Class::Station,
            "buildstorage" => Class::BuildStorage,
            "spacesuit" => Class::Spacesuit,
            "defensible" => Class::Defensible,
            _ => Class::Other,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Class::Galaxy => "galaxy",
            Class::Cluster => "cluster",
            Class::Sector => "sector",
            Class::Zone => "zone",
            Class::Highway => "highway",
            Class::Ship => "ship",
            Class::ShipXS => "ship_xs",
            Class::ShipS => "ship_s",
            Class::ShipM => "ship_m",
            Class::ShipL => "ship_l",
            Class::ShipXL => "ship_xl",
            Class::Station => "station",
            Class::BuildStorage => "buildstorage",
            Class::Spacesuit => "spacesuit",
            Class::Defensible => "defensible",
            Class::Other => "other",
        }
    }
    /// `isclass.{base}` for a component whose real class is `self`.
    pub fn is_a(self, base: Class) -> bool {
        use Class::*;
        self == base
            || matches!(
                (self, base),
                (Highway, Zone)
                    | (ShipXS | ShipS | ShipM | ShipL | ShipXL | Spacesuit, Ship)
                    | (ShipXS | ShipS | ShipM | ShipL | ShipXL | Ship | Spacesuit | Station | BuildStorage, Defensible)
            )
    }
}

/// Fact keys. Subject is a component, an order (`order_get`) or [`Uid::NULL`] for globals.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Prop {
    // any component
    Exists,
    Class,
    Sector,
    Cluster,
    Zone,
    Size,
    IsNormalZone,
    Destination,
    IsOperational,
    PrimaryPurpose,
    IsOnlineObject,
    CanBeAttacked,
    CoverOwner,
    Pilot,
    /// List of components `subject.cansee.{x}` holds for (replay form of a pair predicate).
    CanSee,
    /// List of components `subject.mayattack.{x}` holds for.
    MayAttack,
    /// `subject.relationto.{x}` as an alternating list `[c:x; F:value; ...]`.
    RelationTo,
    /// `subject.distanceto.{x}` as an alternating list `[c:x; F:metres; ...]`.
    DistanceTo,
    /// `@subject.dock` is set (docked).
    Docked,
    Container,
    IsModular,
    CanHaveAttackableModules,
    IsLaserTower,
    IsIndestructible,
    /// `subject.boardingoperations.count`.
    BoardingOperations,
    /// `subject.dps.all` (f32).
    DpsAll,
    /// `subject.hull` (f64 hit points).
    Hull,
    /// `subject.shield` (f64 hit points).
    Shield,
    /// `@this.assignedcontrolled.commander.pilot.$attacktarget` (a foreign blackboard read).
    CommanderAttackTarget,
    // the context's entity (the pilot): its controlled ship
    Ship,
    // the controlled ship (this.ship == this.assignedcontrolled == this.controlled for a pilot)
    JobMainZone,
    IsCommandeered,
    IsJobShip,
    JobExpired,
    IsPlayerOwned,
    TrueOwner,
    IsCapitalShip,
    SafeSize,
    MaxRadarRange,
    Order,
    DefaultOrder,
    Commander,
    WareBasket,
    /// `this.ship.distanceto.[$destination, $targetpos]` for the current move target (geometry probe).
    DistanceToTarget,
    // orders
    OrderKind,
    OrderRunning,
    OrderSpace,
    // globals
    SpaceReservationMaxDuration,
    SpaceReservationSpam,
    PlayerOccupiedShip,
    /// `player.ismadscoreenabled`.
    MadScoreEnabled,
}

impl Prop {
    pub fn name(self) -> &'static str {
        use Prop::*;
        match self {
            Exists => "exists",
            Class => "class",
            Sector => "sector",
            Cluster => "cluster",
            Zone => "zone",
            Size => "size",
            IsNormalZone => "isnormalzone",
            Destination => "destination",
            IsOperational => "isoperational",
            PrimaryPurpose => "primarypurpose",
            IsOnlineObject => "isonlineobject",
            CanBeAttacked => "canbeattacked",
            CoverOwner => "coverowner",
            Pilot => "pilot",
            CanSee => "cansee",
            MayAttack => "mayattack",
            RelationTo => "relationto",
            DistanceTo => "distanceto",
            Docked => "dock",
            Container => "container",
            IsModular => "ismodular",
            CanHaveAttackableModules => "canhaveattackablemodules",
            IsLaserTower => "islasertower",
            IsIndestructible => "isindestructible",
            BoardingOperations => "boardingoperations.count",
            DpsAll => "dps.all",
            Hull => "hull",
            Shield => "shield",
            CommanderAttackTarget => "commander.pilot.$attacktarget",
            Ship => "ship",
            JobMainZone => "jobmainzone",
            IsCommandeered => "iscommandeered",
            IsJobShip => "isjobship",
            JobExpired => "jobexpired",
            IsPlayerOwned => "isplayerowned",
            TrueOwner => "trueowner",
            IsCapitalShip => "iscapitalship",
            SafeSize => "safesize",
            MaxRadarRange => "maxradarrange",
            Order => "order",
            DefaultOrder => "defaultorder",
            Commander => "commander",
            WareBasket => "warebasket",
            DistanceToTarget => "distancetotarget",
            OrderKind => "id",
            OrderRunning => "isrunning",
            OrderSpace => "$space",
            SpaceReservationMaxDuration => "param.spacereservation.maxduration",
            SpaceReservationSpam => "global.$spacereservationspam",
            PlayerOccupiedShip => "player.occupiedship",
            MadScoreEnabled => "player.ismadscoreenabled",
        }
    }
    pub fn parse(s: &str) -> Option<Prop> {
        use Prop::*;
        const ALL: [Prop; 51] = [
            Exists,
            Class,
            Sector,
            Cluster,
            Zone,
            Size,
            IsNormalZone,
            Destination,
            IsOperational,
            PrimaryPurpose,
            IsOnlineObject,
            CanBeAttacked,
            CoverOwner,
            Pilot,
            CanSee,
            MayAttack,
            RelationTo,
            DistanceTo,
            Docked,
            Container,
            IsModular,
            CanHaveAttackableModules,
            IsLaserTower,
            IsIndestructible,
            BoardingOperations,
            DpsAll,
            Hull,
            Shield,
            CommanderAttackTarget,
            Ship,
            JobMainZone,
            IsCommandeered,
            IsJobShip,
            JobExpired,
            IsPlayerOwned,
            TrueOwner,
            IsCapitalShip,
            SafeSize,
            MaxRadarRange,
            Order,
            DefaultOrder,
            Commander,
            WareBasket,
            DistanceToTarget,
            OrderKind,
            OrderRunning,
            OrderSpace,
            SpaceReservationMaxDuration,
            SpaceReservationSpam,
            PlayerOccupiedShip,
            MadScoreEnabled,
        ];
        ALL.iter().copied().find(|p| p.name() == s)
    }
}

/// Pure world reads. Implementors override the typed methods for speed; the defaults go through
/// [`Facts::get`] (used by the replay world).
pub trait Facts {
    fn now(&self) -> Time;
    fn get(&self, subject: Uid, p: Prop) -> Val;
    fn order_get(&self, order: OrderId, p: Prop) -> Val;
    /// `a.hascontext.{space}`: `space` is `a` or one of its ancestors (default: the zone, sector and
    /// cluster facts of `a` and of its ship).
    fn has_context(&self, a: Uid, space: Uid) -> bool {
        if a.is_null() || space.is_null() {
            return false;
        }
        if a == space {
            return true;
        }
        let ship = self.uid_of(a, Prop::Ship);
        [a, ship].iter().filter(|c| !c.is_null()).any(|&c| {
            c == space
                || self.uid_of(c, Prop::Zone) == space
                || self.uid_of(c, Prop::Sector) == space
                || self.uid_of(c, Prop::Cluster) == space
        })
    }
    /// `viewer.cansee.{target}` (default: the [`Prop::CanSee`] list).
    fn can_see(&self, viewer: Uid, target: Uid) -> bool {
        self.get(viewer, Prop::CanSee).as_uid_list().is_some_and(|l| l.contains(&target))
    }
    /// A pair fact stored as an alternating list `[c:b; value; ...]` on `a`.
    fn pair(&self, a: Uid, p: Prop, b: Uid) -> Val {
        if let Val::List(l) = self.get(a, p) {
            for kv in l.chunks(2) {
                if kv.len() == 2 && kv[0].as_uid() == Some(b) {
                    return kv[1].clone();
                }
            }
        }
        Val::Null
    }
    /// `a.relationto.{b}` (f32).
    fn relation_to(&self, a: Uid, b: Uid) -> f32 {
        self.pair(a, Prop::RelationTo, b).as_f32().unwrap_or(0.0)
    }
    /// `a.distanceto.{b}` (length, f32).
    fn distance_to(&self, a: Uid, b: Uid) -> f32 {
        self.pair(a, Prop::DistanceTo, b).as_f32().unwrap_or(0.0)
    }
    /// `a.mayattack.{b}` (default: the [`Prop::MayAttack`] list).
    fn may_attack(&self, a: Uid, b: Uid) -> bool {
        self.get(a, Prop::MayAttack).as_uid_list().is_some_and(|l| l.contains(&b))
    }

    fn exists(&self, c: Uid) -> bool {
        !c.is_null() && self.get(c, Prop::Exists).as_bool().unwrap_or(false)
    }
    fn class(&self, c: Uid) -> Option<Class> {
        if c.is_null() {
            return None;
        }
        self.get(c, Prop::Class).as_key().map(Class::parse)
    }
    fn is_class(&self, c: Uid, base: Class) -> bool {
        self.class(c).is_some_and(|k| k.is_a(base))
    }
    fn uid_of(&self, c: Uid, p: Prop) -> Uid {
        if c.is_null() {
            return Uid::NULL;
        }
        self.get(c, p).as_uid().unwrap_or(Uid::NULL)
    }
    fn flag(&self, c: Uid, p: Prop) -> bool {
        self.get(c, p).as_bool().unwrap_or(false)
    }
    fn length(&self, c: Uid, p: Prop) -> f32 {
        self.get(c, p).as_f32().unwrap_or(0.0)
    }
    fn order_of(&self, ship: Uid) -> OrderId {
        self.get(ship, Prop::Order).as_order().unwrap_or(OrderId::NULL)
    }
    fn default_order_of(&self, ship: Uid) -> OrderId {
        self.get(ship, Prop::DefaultOrder).as_order().unwrap_or(OrderId::NULL)
    }
    /// `order.id` (`'Patrol'`, `'MoveDie'`, ...); empty for the null order.
    fn order_kind(&self, o: OrderId) -> String {
        if o.is_null() {
            return String::new();
        }
        match self.order_get(o, Prop::OrderKind) {
            Val::Str(s) => s,
            Val::Key(s) => s,
            _ => String::new(),
        }
    }
    fn order_running(&self, o: OrderId) -> bool {
        !o.is_null() && self.order_get(o, Prop::OrderRunning).as_bool().unwrap_or(false)
    }
}

/// Engine queries issued by actions (compared arguments, captured results).
#[derive(Clone, Debug, PartialEq)]
pub enum Query {
    /// `<find_zone name= space= [normalzone=true] [knownto=]>` with an optional
    /// `<match_distance object= min=>` or `<match class="class.highway" negate="true"/>`.
    FindZone { space: Uid, normalzone: bool, knownto: Val, min_distance: Option<(Uid, f32)>, not_highway: bool },
    /// `<get_global_path component= multiple="true" uselocalhighways="false"><start object=/><end object=/>`.
    GlobalPath { start: Uid, end: Uid },
    /// `<create_position name= space= max=>` (random position in a space).
    CreatePosition { space: Uid, max: f32 },
    /// `<create_position name= space= value= object=>` (position relative to `object`, expressed in `space`).
    CreatePositionIn { space: Uid, value: Pos, object: Uid },
    /// `<get_safe_pos result= radius= ignored= value= zone= allowyaxis= ignoredangerousregions=>`.
    SafePos { value: Pos, zone: Uid, radius: f32, ignored: Uid, allowyaxis: bool, ignoredangerousregions: bool },
    /// `<find_gravidar_contact multiple="true" docked="false" object= class= masstraffic="false"
    /// checkoperational="false" maybeattackedby= excluded= append="true">` with `<match class=
    /// "class.buildstorage" negate="true"/>` and `<match state="componentstate.wreck" negate="true"/>`
    /// (lib.target.selection L81/L89).
    GravidarContacts { object: Uid, classes: Vec<Val>, maybeattackedby: Uid, excluded: Vec<Val> },
    /// `<get_safe_pos result= value= sector=|zone= ignoredangerousregions="true" radius=>` with
    /// `<spacereservation component= duration= index= safezone=><orientation look_away refobject=/>`.
    SafePosReserve { value: Pos, space: Uid, in_sector: bool, radius: f32, component: Uid, duration: f64 },
}

/// Answers to [`Query`].
#[derive(Clone, Debug, PartialEq)]
pub enum Answer {
    Zone(Uid),
    Path(Vec<Uid>),
    Pos(Pos),
    Reserved { pos: Pos, index: Val, zone: Uid },
}

impl Query {
    pub fn kind(&self) -> &'static str {
        match self {
            Query::FindZone { .. } => "find_zone",
            Query::GlobalPath { .. } => "get_global_path",
            Query::CreatePosition { .. } => "create_position",
            Query::CreatePositionIn { .. } => "create_position_in",
            Query::SafePos { .. } => "get_safe_pos",
            Query::SafePosReserve { .. } => "get_safe_pos_reserve",
            Query::GravidarContacts { .. } => "find_gravidar_contact",
        }
    }

    /// Record form of the arguments (the `->` results are filled by the producer).
    pub fn to_record(&self, ent: Uid) -> Record {
        let r = Record::new("q", ent, self.kind());
        match self {
            Query::FindZone { space, normalzone, knownto, min_distance, not_highway } => {
                let mut r = r.arg("space", Val::uid(*space)).arg("normalzone", Val::Bool(*normalzone));
                if !matches!(knownto, Val::Null) {
                    r = r.arg("knownto", knownto.clone());
                }
                if let Some((o, d)) = min_distance {
                    r = r.arg("mindist_object", Val::uid(*o)).arg("mindist", Val::F32(*d));
                }
                if *not_highway {
                    r = r.arg("not_highway", Val::Bool(true));
                }
                r
            }
            Query::GlobalPath { start, end } => r.arg("start", Val::uid(*start)).arg("end", Val::uid(*end)),
            Query::CreatePosition { space, max } => r.arg("space", Val::uid(*space)).arg("max", Val::F32(*max)),
            Query::CreatePositionIn { space, value, object } => {
                r.arg("space", Val::uid(*space)).arg("value", Val::Pos(*value)).arg("object", Val::uid(*object))
            }
            Query::SafePos { value, zone, radius, ignored, allowyaxis, ignoredangerousregions } => r
                .arg("value", Val::Pos(*value))
                .arg("zone", Val::uid(*zone))
                .arg("radius", Val::F32(*radius))
                .arg("ignored", Val::uid(*ignored))
                .arg("allowyaxis", Val::Bool(*allowyaxis))
                .arg("ignoredangerousregions", Val::Bool(*ignoredangerousregions)),
            Query::GravidarContacts { object, classes, maybeattackedby, excluded } => r
                .arg("object", Val::uid(*object))
                .arg("class", Val::List(classes.clone()))
                .arg("maybeattackedby", Val::uid(*maybeattackedby))
                .arg("excluded", Val::List(excluded.clone())),
            Query::SafePosReserve { value, space, in_sector, radius, component, duration } => r
                .arg("value", Val::Pos(*value))
                .arg(if *in_sector { "sector" } else { "zone" }, Val::uid(*space))
                .arg("radius", Val::F32(*radius))
                .arg("component", Val::uid(*component))
                .arg("duration", Val::F64(*duration)),
        }
    }

    /// Decode the answer from a record's results.
    pub fn answer_from(&self, r: &Record) -> Result<Answer, String> {
        let need = |k: &str| r.result(k).ok_or_else(|| format!("line {}: {} answer needs {k}", r.line, r.name));
        match self {
            Query::FindZone { .. } => {
                Ok(Answer::Zone(need("zone")?.as_uid().ok_or_else(|| format!("line {}: zone must be c:", r.line))?))
            }
            Query::GravidarContacts { .. } => Ok(Answer::Path(
                need("contacts")?
                    .as_uid_list()
                    .ok_or_else(|| format!("line {}: contacts must be a list of c:", r.line))?,
            )),
            Query::GlobalPath { .. } => Ok(Answer::Path(
                need("path")?.as_uid_list().ok_or_else(|| format!("line {}: path must be a list of c:", r.line))?,
            )),
            Query::CreatePosition { .. } | Query::CreatePositionIn { .. } | Query::SafePos { .. } => {
                Ok(Answer::Pos(need("pos")?.as_pos().ok_or_else(|| format!("line {}: pos must be p:", r.line))?))
            }
            Query::SafePosReserve { .. } => Ok(Answer::Reserved {
                pos: need("pos")?.as_pos().ok_or_else(|| format!("line {}: pos must be p:", r.line))?,
                index: need("index")?.clone(),
                zone: need("zone")?.as_uid().ok_or_else(|| format!("line {}: zone must be c:", r.line))?,
            }),
        }
    }
}

/// Externally visible actions.
#[derive(Clone, Debug, PartialEq)]
pub enum Effect {
    SetCommand {
        command: &'static str,
    },
    SetCommandAction {
        action: &'static str,
    },
    EditOrderParam {
        order: OrderId,
        param: &'static str,
        value: Val,
    },
    SyncpointReached {
        order: OrderId,
    },
    OrderFailed {
        order: OrderId,
        text: &'static str,
    },
    CancelOrder {
        order: OrderId,
    },
    CreateOrder {
        id: &'static str,
        object: Uid,
        immediate: bool,
        params: Vec<(&'static str, Val)>,
    },
    SignalObjects {
        object: Uid,
        param: Val,
        param2: Val,
        param3: Val,
    },
    CancelSpaceReservation {
        component: Uid,
        index: Val,
        zone: Uid,
    },
    MoveTo {
        object: Uid,
        destination: Uid,
        pos: Pos,
        travel: bool,
    },
    RunScript {
        name: &'static str,
        params: Vec<(&'static str, Val)>,
    },
    StopMoving {
        object: Uid,
    },
    DefaultFlightControlModel {
        object: Uid,
    },
    /// `<set_known object= known="true"/>`.
    SetKnown {
        object: Uid,
    },
}

impl Effect {
    pub fn kind(&self) -> &'static str {
        match self {
            Effect::SetCommand { .. } => "set_command",
            Effect::SetCommandAction { .. } => "set_command_action",
            Effect::EditOrderParam { .. } => "edit_order_param",
            Effect::SyncpointReached { .. } => "set_order_syncpoint_reached",
            Effect::OrderFailed { .. } => "set_order_failed",
            Effect::CancelOrder { .. } => "cancel_order",
            Effect::CreateOrder { .. } => "create_order",
            Effect::SignalObjects { .. } => "signal_objects",
            Effect::CancelSpaceReservation { .. } => "cancel_space_reservation",
            Effect::MoveTo { .. } => "move_to",
            Effect::RunScript { .. } => "run_script",
            Effect::StopMoving { .. } => "stop_moving",
            Effect::DefaultFlightControlModel { .. } => "set_to_default_flight_control_model",
            Effect::SetKnown { .. } => "set_known",
        }
    }

    pub fn to_record(&self, ent: Uid) -> Record {
        let r = Record::new("fx", ent, self.kind());
        match self {
            Effect::SetCommand { command } => r.arg("command", Val::key(command)),
            Effect::SetCommandAction { action } => r.arg("commandaction", Val::key(action)),
            Effect::EditOrderParam { order, param, value } => {
                r.arg("order", Val::order(*order)).arg("param", Val::Str((*param).into())).arg("value", value.clone())
            }
            Effect::SyncpointReached { order } | Effect::CancelOrder { order } => r.arg("order", Val::order(*order)),
            Effect::OrderFailed { order, text } => r.arg("order", Val::order(*order)).arg("text", Val::key(text)),
            Effect::CreateOrder { id, object, immediate, params } => {
                let mut r = r
                    .arg("id", Val::Str((*id).into()))
                    .arg("object", Val::uid(*object))
                    .arg("immediate", Val::Bool(*immediate));
                for (k, v) in params {
                    r = r.arg(&format!("param.{k}"), v.clone());
                }
                r
            }
            Effect::SignalObjects { object, param, param2, param3 } => r
                .arg("object", Val::uid(*object))
                .arg("param", param.clone())
                .arg("param2", param2.clone())
                .arg("param3", param3.clone()),
            Effect::CancelSpaceReservation { component, index, zone } => {
                r.arg("component", Val::uid(*component)).arg("index", index.clone()).arg("zone", Val::uid(*zone))
            }
            Effect::MoveTo { object, destination, pos, travel } => r
                .arg("object", Val::uid(*object))
                .arg("destination", Val::uid(*destination))
                .arg("pos", Val::Pos(*pos))
                .arg("travel", Val::Bool(*travel)),
            Effect::RunScript { name, params } => {
                let mut r = r.arg("name", Val::Str((*name).into()));
                for (k, v) in params {
                    r = r.arg(&format!("param.{k}"), v.clone());
                }
                r
            }
            Effect::StopMoving { object } | Effect::DefaultFlightControlModel { object } => {
                r.arg("object", Val::uid(*object))
            }
            Effect::SetKnown { object } => r.arg("object", Val::uid(*object)).arg("known", Val::Bool(true)),
        }
    }
}

/// Why a behaviour stopped early.
#[derive(Clone, Debug, PartialEq)]
pub enum Stop {
    /// Replay found a difference (expected vs actual, trace line).
    Mismatch(String),
    /// The behaviour reached a branch that is not ported (the original must run this context).
    Unsupported { script: &'static str, line: u32, what: &'static str },
}

pub type R<T> = Result<T, Stop>;

/// The behaviour's outlet: records draws and answers queries/effects. Replay implements it from a
/// trace; the ECS system implements it with native services and an outbox.
pub trait Io {
    /// An action with a `chance` attribute was reached at XML `line`; one draw was made.
    fn roll(&mut self, ent: Uid, line: u32, chance: f32, pass: bool) -> R<()>;
    /// A random range was evaluated at XML `line` (`<wait min max>`, `<set_value min max>`).
    fn rnd(&mut self, ent: Uid, line: u32, v: f64) -> R<()>;
    fn query(&mut self, ent: Uid, line: u32, q: &Query, rng: &mut Stream) -> R<Answer>;
    fn effect(&mut self, ent: Uid, line: u32, e: &Effect, rng: &mut Stream) -> R<()>;
}

/// Execution context of one behaviour step.
pub struct Cx<'a> {
    pub ent: Uid,
    pub facts: &'a dyn Facts,
    pub io: &'a mut dyn Io,
    pub rng: &'a mut Stream,
}

impl Cx<'_> {
    pub fn now(&self) -> Time {
        self.facts.now()
    }

    /// An action carrying `chance=`: one draw, returns whether the action runs.
    pub fn roll(&mut self, line: u32, chance: f32) -> R<bool> {
        let pass = self.rng.chance(chance);
        self.io.roll(self.ent, line, chance, pass)?;
        Ok(pass)
    }

    /// `<debug_text ... chance="..."/>`: the roll is the only observable part.
    pub fn debug(&mut self, line: u32, chance: f32) -> R<()> {
        self.roll(line, chance).map(|_| ())
    }

    /// A floating-point random range (`min=`/`max=`, flat profile).
    pub fn range(&mut self, line: u32, kind: NumKind, min: f64, max: f64) -> R<f64> {
        let v = self.rng.range(kind, min, max, None);
        self.io.rnd(self.ent, line, v)?;
        Ok(v)
    }

    pub fn query(&mut self, line: u32, q: Query) -> R<Answer> {
        self.io.query(self.ent, line, &q, self.rng)
    }

    pub fn fx(&mut self, line: u32, e: Effect) -> R<()> {
        self.io.effect(self.ent, line, &e, self.rng)
    }

    pub fn zone_query(&mut self, line: u32, q: Query) -> R<Uid> {
        match self.query(line, q)? {
            Answer::Zone(z) => Ok(z),
            other => Err(Stop::Mismatch(format!("line {line}: expected a zone answer, got {other:?}"))),
        }
    }

    pub fn pos_query(&mut self, line: u32, q: Query) -> R<Pos> {
        match self.query(line, q)? {
            Answer::Pos(p) => Ok(p),
            other => Err(Stop::Mismatch(format!("line {line}: expected a position answer, got {other:?}"))),
        }
    }
}
