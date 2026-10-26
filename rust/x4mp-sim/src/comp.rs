//! Components: the [`Component`] trait and the core per-entity state with the engine's exact
//! field types (no precision change: f32 where the engine stores f32, f64 where it stores
//! f64). Offsets refer to Windows build 23660954 (`x4-re/coord/reports/entity.md`,
//! `motion.md`, and reports/REWRITE-ECS-CORE.md for the ones verified by this agent).

use std::fmt;

use crate::hash::{Fnv64, StableHash};
use crate::id::UniverseId;
use crate::impl_fields;
use crate::rng::EngineLcg;
use crate::shadow::{Comparator, Compare};

/// Per-entity data stored in a dense column of every sector world.
///
/// `Default` is the value an entity gets when it is spawned without this component.
/// Implement `StableHash` and `Compare` with [`crate::impl_fields!`].
pub trait Component: Clone + Default + fmt::Debug + Send + Sync + StableHash + Compare + 'static {
    /// Column name in reports, comparisons and statistics.
    const NAME: &'static str;
}

/// 4x4 float matrix as the engine stores transforms: row 0 = position (x, y, z, w),
/// rows 1..3 = rotation basis (findings.md, entity agent).
#[repr(C)]
#[derive(Clone, Copy, PartialEq, Default)]
pub struct Mat4f(pub [[f32; 4]; 4]);

impl Mat4f {
    /// The engine's identity pose: position row zero (w = 0), basis rows from the constants
    /// at RVA 0x22c01d0..0x22c01ff (as initialised by GetObjectPositionInSector).
    pub const IDENTITY: Mat4f =
        Mat4f([[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]);

    pub fn position(&self) -> [f32; 3] {
        [self.0[0][0], self.0[0][1], self.0[0][2]]
    }

    pub fn from_flat(v: [f32; 16]) -> Mat4f {
        let mut m = [[0.0f32; 4]; 4];
        for (i, x) in v.iter().enumerate() {
            m[i / 4][i % 4] = *x;
        }
        Mat4f(m)
    }

    pub fn to_flat(&self) -> [f32; 16] {
        let mut v = [0.0f32; 16];
        for (i, x) in v.iter_mut().enumerate() {
            *x = self.0[i / 4][i % 4];
        }
        v
    }
}

impl fmt::Debug for Mat4f {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Mat4f{:?}", self.0)
    }
}

impl StableHash for Mat4f {
    fn stable_hash(&self, h: &mut Fnv64) {
        self.0.stable_hash(h);
    }
}

impl Compare for Mat4f {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        self.0.compare(&actual.0, c);
    }
}

/// Linear and angular velocity, 4 floats each (pathing state slot +0x40 / +0x50,
/// `MovementControllerInterface` slot 7 output "lin4 + ang4").
#[repr(C)]
#[derive(Clone, Copy, PartialEq, Default, Debug)]
pub struct Vel {
    pub linear: [f32; 4],
    pub angular: [f32; 4],
}
impl_fields!(Vel { linear, angular });

/// Engine class id (`GetClassID`, primary vtable slot 571; 0..0x78, 0x79 = invalid).
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct ClassId(pub u8);
impl_fields!(newtype ClassId);

impl ClassId {
    pub const CLUSTER: ClassId = ClassId(0x0f);
    pub const COMPONENT: ClassId = ClassId(0x16);
    pub const DOCKINGBAY: ClassId = ClassId(0x28);
    pub const GALAXY: ClassId = ClassId(0x2f);
    pub const GATE: ClassId = ClassId(0x30);
    pub const HIGHWAY: ClassId = ClassId(0x33);
    pub const OBJECT: ClassId = ClassId(0x49);
    pub const SECTOR: ClassId = ClassId(0x58);
    pub const STATION: ClassId = ClassId(0x62);
    pub const ZONE: ClassId = ClassId(0x6d);
    pub const SHIP: ClassId = ClassId(0x75);

    /// Engine class name (string table 0x22c6790), `None` above 0x78.
    pub fn name(self) -> Option<&'static str> {
        CLASS_NAMES.get(usize::from(self.0)).copied()
    }

    pub fn from_name(name: &str) -> Option<ClassId> {
        CLASS_NAMES.iter().position(|n| *n == name).map(|i| ClassId(i as u8))
    }

    /// ship_xs .. ship_xl and spacesuit (the concrete ship classes).
    pub fn is_ship(self) -> bool {
        (0x5a..=0x5e).contains(&self.0) || self.0 == 0x60
    }
}

impl fmt::Debug for ClassId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.name() {
            Some(n) => write!(f, "{n}({:#x})", self.0),
            None => write!(f, "class({:#x})", self.0),
        }
    }
}

/// Engine class names by id, read from the `{name, id}` table at RVA 0x22c6790 (data).
pub const CLASS_NAMES: [&str; 0x79] = [
    "accessory",
    "adsign",
    "adsignobject",
    "anomaly",
    "asteroid",
    "attachment",
    "bomb",
    "bomblauncher",
    "buildstorage",
    "buildmodule",
    "buildprocessor",
    "bullet",
    "cargobay",
    "celestialbody",
    "checkpoint",
    "cluster",
    "cockpit",
    "collectableshieldrestore",
    "collectableammo",
    "collectabledata",
    "collectableblueprints",
    "collectablewares",
    "component",
    "computer",
    "connectionmodule",
    "controlroom",
    "countermeasure",
    "crate",
    "crate_l",
    "crate_m",
    "crate_s",
    "crystal",
    "cutsceneanchor",
    "datavault",
    "defencemodule",
    "defensible",
    "destructible",
    "detector",
    "dismantleprocessor",
    "dockarea",
    "dockingbay",
    "drop",
    "effectobject",
    "engine",
    "entity",
    "forceemitter",
    "fogvolume",
    "galaxy",
    "gate",
    "habitation",
    "hackerprobe",
    "highway",
    "highwayblocker",
    "highwaybooster",
    "highwayentrygate",
    "highwayexitgate",
    "highwayscene",
    "highwaytrigger",
    "holomap",
    "influenceobject",
    "landmarkobject",
    "lensflare",
    "lock",
    "lockbox",
    "mine",
    "miningnode",
    "missile",
    "missilelauncher",
    "missileturret",
    "moon",
    "navbeacon",
    "navcontext",
    "npc",
    "object",
    "pier",
    "planet",
    "player",
    "positional",
    "processingmodule",
    "production",
    "radar",
    "recyclable",
    "region",
    "resourceprobe",
    "room",
    "satellite",
    "scanner",
    "scene",
    "sector",
    "shieldgenerator",
    "ship_xs",
    "ship_s",
    "ship_m",
    "ship_l",
    "ship_xl",
    "signalleak",
    "spacesuit",
    "stardust",
    "station",
    "storage",
    "sun",
    "switchable",
    "targetpoint",
    "textdisplay",
    "turret",
    "uielement",
    "ventureplatform",
    "weapon",
    "welfaremodule",
    "zone",
    "collectable",
    "container",
    "controllable",
    "explosive",
    "launcher",
    "module",
    "nonplayer",
    "ship",
    "space",
    "triggerobject",
    "walkablemodule",
];

/// Attention level, `Component+0x1ec` (raw engine int: 1 unknown, 2 ingalaxy, 3 incluster,
/// 4 sectorvisible, 5 insector, 6 adjacentzone, 7 visible, 8 inzone, 9 nearby, 10 adjacentroom,
/// 11 inroom).
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Default, Debug)]
pub struct Attention(pub i32);
impl_fields!(newtype Attention);

/// Position in the component tree: parent component (`+0x70`, its id at `+0x08`) and depth
/// below the sector (the sector itself is 0, its zones 1, ...).
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct Tree {
    pub parent: UniverseId,
    pub depth: u16,
}
impl_fields!(Tree { parent, depth });

/// Raw engine state bits as captured: bit 0 registered (`+0xd1`), bits 1..2 death state
/// (`+0xd3`: 1 dying, 2 dead), bit 3 `+0x68` mode != 0.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct EngineFlags(pub u8);
impl_fields!(newtype EngineFlags);

impl EngineFlags {
    pub fn registered(self) -> bool {
        self.0 & 1 != 0
    }
    pub fn death_state(self) -> u8 {
        (self.0 >> 1) & 3
    }
    pub fn alt_mode(self) -> bool {
        self.0 & 8 != 0
    }
}

/// Which movement controller (`Positional+0x200`) produced [`Transform`]/[`Velocity`].
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
#[repr(u8)]
pub enum CtrlKind {
    /// Not positional, or no controller (placed by its connection).
    #[default]
    None = 0,
    /// `U::NavigationController`: pose and velocity from the current pathing state slot.
    Navigation = 1,
    /// `U::StaticMovementController`: stored transform at controller+0x10.
    Static = 2,
    /// `U::PlayerController` (pose not captured in v1).
    Player = 3,
    /// Any other controller (pose not captured in v1; see [`Controller::vtable`]).
    Other = 4,
}

impl CtrlKind {
    pub fn from_u8(v: u8) -> CtrlKind {
        match v {
            1 => CtrlKind::Navigation,
            2 => CtrlKind::Static,
            3 => CtrlKind::Player,
            4 => CtrlKind::Other,
            _ => CtrlKind::None,
        }
    }
}

/// Movement controller identity: kind and primary vtable RVA (0 when none).
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct Controller {
    pub kind: CtrlKind,
    pub vtable: u32,
}

impl StableHash for Controller {
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u8(self.kind as u8);
        h.write_u32(self.vtable);
    }
}

impl Compare for Controller {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        c.field("kind", |c| c.exact(&self.kind, &actual.kind));
        c.field("vtable", |c| c.exact(&self.vtable, &actual.vtable));
    }
}

/// Local pose as the engine stores it for the entity's controller kind: the current pathing
/// slot mat4 (navigation; relative to [`PoseRef`] or the parent when the reference is 0), the
/// stored static transform, or identity when not captured.
#[derive(Clone, Copy, PartialEq, Default, Debug)]
pub struct Transform(pub Mat4f);
impl_fields!(newtype Transform);

/// Linear/angular velocity from the current pathing slot (zero for static controllers).
#[derive(Clone, Copy, PartialEq, Default, Debug)]
pub struct Velocity(pub Vel);
impl_fields!(newtype Velocity);

/// Reference component of the pathing state slot (`+0xb8`); 0 = relative to the parent.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct PoseRef(pub UniverseId);
impl_fields!(newtype PoseRef);

/// Hull points, `Destructible+0x280` (absolute, double). Max hull is a virtual
/// (vfunc 921) and not captured in v1 (`max` NaN = unknown).
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Hull {
    pub current: f64,
    pub max: f64,
}
impl_fields!(Hull { current, max });

impl Default for Hull {
    fn default() -> Hull {
        Hull { current: 0.0, max: f64::NAN }
    }
}

/// Shield state (lazy in the engine; not captured in v1).
#[derive(Clone, Copy, PartialEq, Default, Debug)]
pub struct Shield {
    pub current: f64,
    pub max: f64,
}
impl_fields!(Shield { current, max });

/// Index into a capture's/universe's faction dictionary. `NONE` = no owner.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct FactionIx(pub u16);
impl_fields!(newtype FactionIx);

impl FactionIx {
    pub const NONE: FactionIx = FactionIx(0xffff);
}

impl Default for FactionIx {
    fn default() -> FactionIx {
        FactionIx::NONE
    }
}

/// True owner (`Object+0x348`) and cover owner (`+0x350`). Apparent owner = cover if set.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct Owner {
    pub owner: FactionIx,
    pub cover: FactionIx,
}
impl_fields!(Owner { owner, cover });

impl Owner {
    pub fn apparent(self) -> FactionIx {
        if self.cover == FactionIx::NONE {
            self.owner
        } else {
            self.cover
        }
    }
}

/// Order-state placeholder (`Controllable+0x4b0..0x4c0` order vector length and `+0x4e0`
/// current index). The ai-orders agent refines this.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct OrderState {
    pub queue_len: u32,
    pub current: u32,
}
impl_fields!(OrderState { queue_len, current });

/// Per-entity RNG stream state (engine generator). Seed with [`crate::rng::entity_seed`].
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct RngState(pub u64);
impl_fields!(newtype RngState);

impl RngState {
    pub fn lcg(&mut self) -> &mut EngineLcg {
        EngineLcg::from_state_mut(&mut self.0)
    }
}

/// Base classes of the engine object's C++ class (capture column `class_flags`): 1 positional,
/// 2 destructible, 4 object, 8 controllable, 16 defensible, 32 ship, 64 station, 128 space,
/// 256 container.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct ClassFlags(pub u16);
impl_fields!(newtype ClassFlags);

impl ClassFlags {
    pub const POSITIONAL: u16 = 1;
    pub const DESTRUCTIBLE: u16 = 2;
    pub const OBJECT: u16 = 4;
    pub const CONTROLLABLE: u16 = 8;
    pub const DEFENSIBLE: u16 = 16;
    pub const SHIP: u16 = 32;
    pub const STATION: u16 = 64;
    pub const SPACE: u16 = 128;
    pub const CONTAINER: u16 = 256;

    pub fn has(self, bit: u16) -> bool {
        self.0 & bit != 0
    }
}

/// Primary vtable RVA of the engine object (identifies its C++ class; optional component,
/// filled by the mirror when registered).
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct VtableRva(pub u32);
impl_fields!(newtype VtableRva);

/// NavigationController cached mat4 (`controller+0x60`; optional component, filled by the
/// mirror when registered).
#[derive(Clone, Copy, PartialEq, Default, Debug)]
pub struct NavCache(pub Mat4f);
impl_fields!(newtype NavCache);

macro_rules! component_names {
    ($($t:ty => $n:literal),* $(,)?) => {$(
        impl Component for $t {
            const NAME: &'static str = $n;
        }
    )*};
}
component_names!(
    ClassId => "class",
    Attention => "attention",
    Tree => "tree",
    EngineFlags => "flags",
    Controller => "controller",
    Transform => "transform",
    Velocity => "velocity",
    PoseRef => "pose_ref",
    Hull => "hull",
    Shield => "shield",
    Owner => "owner",
    OrderState => "orders",
    RngState => "rng",
    ClassFlags => "class_flags",
    VtableRva => "vtable",
    NavCache => "nav_cache",
);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn class_names_match_engine_table() {
        assert_eq!(ClassId::SECTOR.name(), Some("sector"));
        assert_eq!(ClassId::ZONE.name(), Some("zone"));
        assert_eq!(ClassId(0x5a).name(), Some("ship_xs"));
        assert_eq!(ClassId(0x72).name(), Some("launcher"));
        assert_eq!(ClassId(0x78).name(), Some("walkablemodule"));
        assert_eq!(ClassId(0x79).name(), None);
        assert_eq!(ClassId::from_name("station"), Some(ClassId::STATION));
        assert!(ClassId(0x5c).is_ship() && !ClassId::STATION.is_ship());
    }

    #[test]
    fn mat_flat_roundtrip_and_identity_position() {
        let v: [f32; 16] = std::array::from_fn(|i| i as f32);
        assert_eq!(Mat4f::from_flat(v).to_flat(), v);
        assert_eq!(Mat4f::from_flat(v).position(), [0.0, 1.0, 2.0]);
        assert_eq!(Mat4f::IDENTITY.position(), [0.0, 0.0, 0.0]);
    }

    #[test]
    fn owner_apparent_and_flags() {
        assert_eq!(Owner { owner: FactionIx(1), cover: FactionIx::NONE }.apparent(), FactionIx(1));
        assert_eq!(Owner { owner: FactionIx(1), cover: FactionIx(2) }.apparent(), FactionIx(2));
        let f = EngineFlags(1 | (2 << 1) | 8);
        assert!(f.registered() && f.alt_mode());
        assert_eq!(f.death_state(), 2);
    }
}
