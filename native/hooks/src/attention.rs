//! Shared attention: every object gets the maximum attention level that the engine would
//! compute for any player of the session, not only for the local one.
//!
//! The engine computes attention in vtable slot 703 (`ComputeAttention`, +0x15f8) from the
//! local player entity (`Universe+0x230`) and the local render cameras (`Universe+0x2c0`).
//! This module wraps the verified slot-703 pointers of every class family whose result
//! depends on the player, calls the original and raises (never lowers) the result with the
//! same rules applied to up to `MAX_SOURCES` remote player proxies. Transitions still go
//! through the engine's `SetAttention`; `Component+0x1ec` is never written.
//!
//! It also wraps `U::Sector::Move` (slot 674) to replace the low-attention periodic-pass
//! jitter, which the engine draws from the MoveWorker RNG stream, with a value seeded from
//! the session seed and the sector's static position.
//!
//! Windows X4 9.00 build 23660954 only. All addresses are RVAs of that build.

use super::*;

pub(crate) const MAX_SOURCES: usize = 8;

const GET_TRANSFORM_RVA: usize = 0x3db8a0;
const DISTANCE_SQ_RVA: usize = 0x3e0c40;
const SET_ATTENTION_RVA: usize = 0x3d5660;
const UNIVERSE_RVA: usize = 0x3d198f8;
const HIGHWAY_ZONE_OF_RVA: usize = 0x5f88c0;
const DEFAULTS_RVA: usize = 0x85e170;
const ROOM_ROOT_RVA: usize = 0x75b5f0;
const CONNECTION_QUERY_RVA: usize = 0x4a76a0;
const CONNECTION_FLAG_RVA: usize = 0x391be0e;
const HIGHWAY_CHECKER_RVA: usize = 0x31bda30;
const POSITIONAL_ATTENTION_RVA: usize = 0x753350;
const DISTANCE_SQ_TO_RVA: usize = 0x3e1930;
const DOCK_DEFAULTS_RVA: usize = 0x5a03b0;
const DOCK_CHECKER_RVA: usize = 0x31bc518;
const PREFERRED_BASE: u64 = 0x1_4000_0000;

const ZONE_COMPUTE_RVA: usize = 0x843b20;
const SHIP_COMPUTE_RVA: usize = 0x7d5110;
const SECTOR_COMPUTE_RVA: usize = 0x78d6a0;
const CLUSTER_COMPUTE_RVA: usize = 0x3bb7f0;
const CONTROLLABLE_COMPUTE_RVA: usize = 0x4640e0;
const OBJECT_COMPUTE_RVA: usize = 0x6d20b0;
const GATE_COMPUTE_RVA: usize = 0x5cd4f0;
const ROOM_COMPUTE_RVA: usize = 0x76e520;
const WALKABLE_COMPUTE_RVA: usize = 0x80e590;
const SECTOR_MOVE_RVA: usize = 0x784230;

// Component layout (entity report; ComputeAttention decompilations).
const ID: usize = 0x08;
const DEFAULTS_SOURCE: usize = 0x60;
const PARENT: usize = 0x70;
const CHILD_LISTS: [usize; 2] = [0xa8, 0xa0];
const CONNECTIONS: usize = 0xc0;
const REGISTERED: usize = 0xd1;
const DYING: usize = 0xd3;
const ATTENTION: usize = 0x1ec;
const SECTOR_PASS_ENABLED: usize = 0x442;
const SECTOR_NEXT_PASS: usize = 0x448;
const PLAYER_ENTITY: usize = 0x230;
const CAMERAS: usize = 0x2c0;
const CAMERA_COUNT: usize = 0x3c0;
const CAMERA_ANCHOR: usize = 0x770;
const CAMERA_ENABLED: usize = 0x870;
const TLS_GAME_TIME: usize = 0x300;
const TLS_RNG: usize = 0x318;
const ZONE_DEFAULTS_BOX: usize = 0x628;
const SHIP_DOCK_STATE: usize = 0x3e0;
const DOCK_INHERITS_ATTENTION: usize = 0x603;

// Engine class ids passed to IsClass (vtable +0x11e8).
const CLUSTER: u32 = 0x0f;
const SECTOR: u32 = 0x58;
const ZONE: u32 = 0x6d;
const SHIP: u32 = 0x75;

// Virtual-function byte offsets used exactly as the engine uses them.
const VF_TYPE_CHECK: usize = 0x18;
const VF_IS_CLASS: usize = 0x11e8;
const VF_ACTIVE: usize = 0x1580;
const VF_IS_HIGHWAY: usize = 0x1d08;
const VF_ZONE_ADJACENT: usize = 0x1db0;
const VF_ZONE_BOX_LEVELS: usize = 0x1da0;
const VF_IS_DOCKED: usize = 0x1f50;
const VF_ATTENTION_RADIUS: usize = 0x1ea8;
const VF_WALKABLE_RANGE_GATE: usize = 0x1838;

const ZONE_BOX_MARGIN: f32 = 50000.0;
const WALKABLE_RANGE_SQ: f32 = 1.0e6;
const PCG_MULTIPLIER: u64 = 0x5851_f42d_4c95_7f2d;
const PCG_INCREMENT: u64 = 0x1405_7b7e_f767_814f;

#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub(crate) enum Kind {
    Zone,
    Ship,
    Sector,
    Cluster,
    Controllable,
    Object,
    Gate,
    Room,
    Walkable,
    SectorMove,
}

#[derive(Copy, Clone)]
pub(crate) struct Hook {
    pub slot: usize,
    pub original: usize,
    pub kind: Kind,
}

const fn hook(slot: usize, kind: Kind) -> Hook {
    let original = match kind {
        Kind::Zone => ZONE_COMPUTE_RVA,
        Kind::Ship => SHIP_COMPUTE_RVA,
        Kind::Sector => SECTOR_COMPUTE_RVA,
        Kind::Cluster => CLUSTER_COMPUTE_RVA,
        Kind::Controllable => CONTROLLABLE_COMPUTE_RVA,
        Kind::Object => OBJECT_COMPUTE_RVA,
        Kind::Gate => GATE_COMPUTE_RVA,
        Kind::Room => ROOM_COMPUTE_RVA,
        Kind::Walkable => WALKABLE_COMPUTE_RVA,
        Kind::SectorMove => SECTOR_MOVE_RVA,
    };
    Hook { slot, original, kind }
}

/// Every primary vtable slot in the build that holds one of the wrapped functions
/// (exhaustive `.rdata` scan, `x4-re/scripts/parity/refs.py`).
pub(crate) const HOOKS: [Hook; 52] = [
    hook(0x2b0a7d8, Kind::Zone), // Highway
    hook(0x2b67678, Kind::Zone), // Zone
    hook(0x2b86090, Kind::Ship), // Ship
    hook(0x2b9b080, Kind::Ship), // ShipExtraLarge
    hook(0x2b7ed30, Kind::Ship), // ShipExtraSmall
    hook(0x2b9d5f0, Kind::Ship), // ShipLarge
    hook(0x2b83a38, Kind::Ship), // ShipMedium
    hook(0x2b814b0, Kind::Ship), // ShipSmall
    hook(0x2b79fa8, Kind::Ship), // SpaceSuit
    hook(0x2b20040, Kind::Sector),
    hook(0x2aa3a90, Kind::Cluster),
    hook(0x2aae010, Kind::Controllable), // BuildStorage
    hook(0x2a907e8, Kind::Controllable), // Container
    hook(0x2a8e480, Kind::Controllable), // Controllable
    hook(0x2ae35b8, Kind::Controllable), // Defensible
    hook(0x2b77ad0, Kind::Controllable), // Station
    hook(0x2a948b0, Kind::Object), // CollectableData
    hook(0x2a96b20, Kind::Object), // CollectableWares
    hook(0x2a98f00, Kind::Object), // CollectableBlueprints
    hook(0x2a9b2c8, Kind::Object), // CollectableShieldRestore
    hook(0x2a9d5e8, Kind::Object), // CollectableAmmo
    hook(0x2a9f8f0, Kind::Object), // Collectable
    hook(0x2aa5b50, Kind::Object), // Checkpoint
    hook(0x2ab4740, Kind::Object), // Asteroid
    hook(0x2ab8a30, Kind::Object), // Anomaly
    hook(0x2ace088, Kind::Object), // Recyclable
    hook(0x2ad25f0, Kind::Object), // Explosive
    hook(0x2ad9080, Kind::Object), // Drop
    hook(0x2ae66c8, Kind::Object), // DataVault
    hook(0x2af72d8, Kind::Object), // TriggerObject
    hook(0x2afb480, Kind::Object), // Mine
    hook(0x2aff6b0, Kind::Object), // AdSignObject
    hook(0x2b061a0, Kind::Object), // Missile
    hook(0x2b085a8, Kind::Object), // LandmarkObject
    hook(0x2b23e78, Kind::Object), // Satellite
    hook(0x2b29ea8, Kind::Object), // ResourceProbe
    hook(0x2b37c50, Kind::Object), // Object
    hook(0x2b3e540, Kind::Object), // NavBeacon
    hook(0x2b5f0b0, Kind::Object), // Lockbox
    hook(0x2b65210, Kind::Object), // Bomb
    hook(0x2ba4838, Kind::Object), // InfluenceObject
    hook(0x2c31538, Kind::Object), // HighwayExitGate
    hook(0x2c35648, Kind::Object), // HighwayEntryGate
    hook(0x2b15148, Kind::Gate),
    hook(0x2aa1c20, Kind::Room), // Cockpit
    hook(0x2a8c3f0, Kind::Room), // ControlRoom
    hook(0x2adb298, Kind::Room), // DockingBay
    hook(0x2b28070, Kind::Room), // Room
    hook(0x2aa9fd0, Kind::Walkable), // BuildModule
    hook(0x2add3b8, Kind::Walkable), // DockArea
    hook(0x2b6bd38, Kind::Walkable), // WalkableModule
    hook(0x2b1ff58, Kind::SectorMove), // Sector::Move
];

/// Relocation-free engine code verified in memory before any hook is installed. The
/// executable SHA-256 is checked separately; these hashes catch in-memory modification.
const CODE_SITES: [(usize, usize, &str); 15] = [
    (0x5a03b0, 193, "bc6e69a5c6d8aacdc9c59bf110a371588ed7d63a722244c46f2df303e332c575"),
    (0x78d6a0, 457, "a721c29d049b49806697cb70cde233fe1c51e34b53ee13d7cab6daccfc5c1ded"),
    (0x3bb7f0, 149, "0ea6b0eeacb40fec7cd0e8ae0afd7fd480c399a63e95ed8bc03b7ccbadce9324"),
    (0x4640e0, 118, "09455658525a263330642a0bbe5ae9688f31548f3e1d6b9cdef07453d0f3588c"),
    (0x6d20b0, 1134, "9ab119383352cb4b29bc05993a4663eda0f6511aa5b9edaa8a8b950a957b8966"),
    (0x5cd4f0, 948, "a0cc4ca5084fa44ca03384836806ea141cda1cd887e0e178f7ab396dc1292761"),
    (0x76e520, 384, "61ccd72154b8d25d246bb3832ae056b500c6c40ed942ed9e44db05b8841df146"),
    (0x80e590, 521, "4c05a45b34784429976b691519fca6929d80a458de0955c54a3a89c878fb2893"),
    (0x753350, 295, "a95dac8c3bf705530eb7892fac20ee67ed612d1d5e0fc5bb60f9eb84864a82d5"),
    (0x784230, 738, "f09165f450b152e0f9d4aa10e0375599321552303a81e30106fa73172145c8d4"),
    (0x5f88c0, 593, "4f2bb3547dcabeeeb3e09c74537f6a9240027b78c78f3e1df804448d73bc1ad9"),
    (0x85e170, 193, "3b2bbe0002c8a5283621dac59492576f09b40408d65a4e753f8cb648831db6c6"),
    (0x75b5f0, 388, "c5003016ebb044f24bc33276d42569ca67c792248f9a8e11f415cb2611061658"),
    (0x4a76a0, 295, "4d01bfc7c99eaaccf9be672ddaad51a275a940258a37c6cb20033d98910357fa"),
    (0x3e1930, 64, "66f7c60411aa13762cdc2d05c17c5fedee03c32231a30a689134782a26b67087"),
];

static INSTALLED: [AtomicBool; HOOKS.len()] = [const { AtomicBool::new(false) }; HOOKS.len()];
static ENABLED: AtomicBool = AtomicBool::new(false);
static SOURCES: [AtomicU64; MAX_SOURCES] = [const { AtomicU64::new(0) }; MAX_SOURCES];
static RAISES: AtomicU64 = AtomicU64::new(0);
static REFRESHES: AtomicU64 = AtomicU64::new(0);
static JITTER_FIXED: AtomicU64 = AtomicU64::new(0);
static JITTER_SKIPPED: AtomicU64 = AtomicU64::new(0);

/// Zone/sector/cluster IDs last seen for the local player (slot 0) and every remote source.
#[derive(Copy, Clone, Default, PartialEq, Eq, Debug)]
pub(crate) struct Region {
    zone: u64,
    sector: u64,
    cluster: u64,
}
static mut REGIONS: [Region; MAX_SOURCES + 1] = [Region {
    zone: 0,
    sector: 0,
    cluster: 0,
}; MAX_SOURCES + 1];

type Compute = unsafe extern "C" fn(*mut u8) -> i32;
type SectorMove = unsafe extern "C" fn(*mut u8);
type SetAttention = unsafe extern "C" fn(*mut u8, i32);
type IsClass = unsafe extern "C" fn(*mut u8, u32) -> bool;
type Predicate = unsafe extern "C" fn(*mut u8) -> bool;
type Relation = unsafe extern "C" fn(*mut u8, *mut u8) -> bool;
type Active = unsafe extern "C" fn(*mut u8, i32, c_int) -> bool;
type Radius = unsafe extern "C" fn(*mut u8) -> f32;
type GetTransform = unsafe extern "C" fn(*mut u8, *mut f32, f64, *mut u8, c_int) -> *mut c_void;
type GetDistanceSq =
    unsafe extern "C" fn(*mut u8, *const f32, *mut u8, f64, c_int, bool, f32) -> f32;
type DistanceSqTo = unsafe extern "C" fn(*mut u8, *mut u8, *mut u8, f64, c_int) -> f32;
type Lookup = unsafe extern "C" fn(*mut u8, *mut *mut u8, u8) -> *mut *mut u8;
type Defaults = unsafe extern "C" fn(*mut u8) -> *mut u8;
type ConnectionQuery = unsafe extern "C" fn(*mut u64, *mut u8, u32, u8, *mut c_void) -> *mut u64;

// ---------------------------------------------------------------- pure helpers (tested)

pub(crate) fn raise(base: i32, remote: i32) -> i32 {
    base.max(remote)
}

/// Level a zone adjacent to a player's zone receives: 7 inside the zone-defaults box
/// (+50 km margin, Chebyshev distance of the player position in zone space), else 6.
pub(crate) fn adjacent_zone_level(position: [f32; 3], box_extent: f32) -> i32 {
    let chebyshev = position[0].abs().max(position[1].abs()).max(position[2].abs());
    if box_extent + ZONE_BOX_MARGIN > chebyshev {
        7
    } else {
        6
    }
}

/// One step of the engine's per-thread generator (`Random::FloatState` and siblings).
pub(crate) fn pcg_step(state: u64) -> u64 {
    state
        .wrapping_mul(PCG_MULTIPLIER)
        .wrapping_add(PCG_INCREMENT)
        .rotate_right(30)
}

/// Deterministic replacement for the `[0,1)` low-attention Sector::Move jitter.
pub(crate) fn sector_jitter(session_seed: u64, identity: &[u32], previous_due: f64) -> f64 {
    let mut hash = Fnv::new();
    hash.update(&session_seed.to_le_bytes());
    hash.update(b"SectorMoveJitter");
    for value in identity {
        hash.update(&value.to_le_bytes());
    }
    hash.update(&previous_due.to_bits().to_le_bytes());
    // splitmix64 finalizer: FNV alone diffuses the trailing time bytes poorly.
    let mut mixed = hash.0;
    mixed = (mixed ^ (mixed >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    mixed = (mixed ^ (mixed >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    mixed ^= mixed >> 31;
    (mixed >> 32) as f64 / 4_294_967_296.0
}

/// Decides whether a Sector::Move call drew exactly the low-attention jitter and, if so,
/// returns the restored RNG state and the deterministic next-pass time.
pub(crate) fn fixed_sector_pass(
    rng_before: u64,
    rng_after: u64,
    now: f64,
    due_after: f64,
    level: i32,
    jitter: f64,
) -> Option<(u64, f64)> {
    if level >= 5 || rng_after != pcg_step(rng_before) {
        return None;
    }
    let drawn = due_after - now - 5.0;
    if !(0.0..1.0).contains(&drawn) {
        return None;
    }
    Some((rng_before, now + 5.0 + jitter))
}

/// IDs whose attention must be recomputed when one tracked player moves between regions.
pub(crate) fn changed_scopes(old: Region, new: Region) -> ([u64; 2], [u64; 2], [u64; 2]) {
    if old == new {
        return ([0; 2], [0; 2], [0; 2]);
    }
    let pair = |a: u64, b: u64| if a == b { [a, 0] } else { [a, b] };
    (
        pair(old.cluster, new.cluster),
        pair(old.sector, new.sector),
        pair(old.zone, new.zone),
    )
}

pub(crate) fn parse_sources(rows: &[&[u8]]) -> Option<[u64; MAX_SOURCES]> {
    if rows.len() > MAX_SOURCES {
        return None;
    }
    let mut ids = [0u64; MAX_SOURCES];
    let mut count = 0;
    for row in rows {
        let id = parse_id(row)?;
        if id == 0 || ids[..count].contains(&id) {
            return None;
        }
        ids[count] = id;
        count += 1;
    }
    Some(ids)
}

// ---------------------------------------------------------------- engine access

#[inline]
unsafe fn vfunc<T: Copy>(object: *mut u8, offset: usize) -> T {
    let vtable = ptr::read(object as *const *const u8);
    ptr::read(vtable.add(offset) as *const T)
}

#[inline]
unsafe fn parent(object: *mut u8) -> *mut u8 {
    ptr::read(object.add(PARENT) as *const *mut u8)
}

pub(crate) unsafe fn is_class(object: *mut u8, class: u32) -> bool {
    !object.is_null() && vfunc::<IsClass>(object, VF_IS_CLASS)(object, class)
}

/// First component of `class` in the parent chain starting at `object` itself.
pub(crate) unsafe fn ancestor(object: *mut u8, class: u32) -> *mut u8 {
    let mut current = object;
    for _ in 0..32 {
        if current.is_null() {
            return ptr::null_mut();
        }
        if is_class(current, class) {
            return current;
        }
        current = parent(current);
    }
    ptr::null_mut()
}

/// True when `object` is `source` or one of its ancestors (the engine's player chain test).
unsafe fn chain_contains(source: *mut u8, object: *mut u8) -> bool {
    let mut current = source;
    for _ in 0..32 {
        if current.is_null() {
            return false;
        }
        if current == object {
            return true;
        }
        current = parent(current);
    }
    false
}

unsafe fn game_time() -> Option<f64> {
    let tls = thread_block();
    if tls.is_null() {
        None
    } else {
        Some(ptr::read(tls.add(TLS_GAME_TIME) as *const f64))
    }
}

unsafe fn position_in(source: *mut u8, space: *mut u8, time: f64) -> [f32; 16] {
    let mut transform = [0.0f32; 16];
    transform[4] = 1.0;
    transform[9] = 1.0;
    transform[14] = 1.0;
    let get: GetTransform = core::mem::transmute(IMAGE + GET_TRANSFORM_RVA);
    get(source, transform.as_mut_ptr(), time, space, 0);
    transform
}

unsafe fn set_attention(object: *mut u8) {
    if !object.is_null() {
        let set: SetAttention = core::mem::transmute(IMAGE + SET_ATTENTION_RVA);
        set(object, 1);
    }
}

unsafe fn registry() -> *mut c_void {
    let registry = ptr::read((IMAGE + REGISTRY_RVA) as *const *mut c_void);
    if registry.is_null() || registry != REGISTERED_REGISTRY {
        ptr::null_mut()
    } else {
        registry
    }
}

unsafe fn resolve(id: u64) -> *mut u8 {
    let registry = registry();
    if id == 0 || registry.is_null() {
        return ptr::null_mut();
    }
    let find: unsafe extern "C" fn(*mut c_void, u64, u32) -> *mut u8 =
        core::mem::transmute(IMAGE + REGISTRY_FIND_RVA);
    find(registry, id, 4)
}

unsafe fn component_id(object: *mut u8) -> u64 {
    if object.is_null() {
        0
    } else {
        ptr::read(object.add(ID) as *const u64)
    }
}

unsafe fn universe() -> *mut u8 {
    if IMAGE == 0 {
        ptr::null_mut()
    } else {
        ptr::read((IMAGE + UNIVERSE_RVA) as *const *mut u8)
    }
}

/// Owner-thread, armed, loaded context; the same guards the observer uses.
unsafe fn engine_ready() -> bool {
    ENABLED.load(Ordering::Acquire)
        && IMAGE != 0
        && GetCurrentThreadId() == OWNER_THREAD
        && !REGISTERED_TABLE.is_null()
        && ptr::read((IMAGE + VBI_RVA) as *const *mut *mut c_void) == REGISTERED_TABLE
        && ptr::read((REGISTERED_TABLE as *mut u8).add(0x130) as *const c_int) == 0
}

/// Resolves the registered remote sources through the generation-checked registry.
unsafe fn sources(out: &mut [*mut u8; MAX_SOURCES]) -> usize {
    if !engine_ready() {
        return 0;
    }
    let mut count = 0;
    for slot in SOURCES.iter() {
        let id = slot.load(Ordering::Acquire);
        if id == 0 {
            continue;
        }
        let object = resolve(id);
        if object.is_null() || ptr::read(object.add(REGISTERED)) == 0 || ptr::read(object.add(DYING)) != 0
        {
            continue;
        }
        out[count] = object;
        count += 1;
    }
    count
}

// ---------------------------------------------------------------- per-family remote rules

/// Zone::vfunc703 0x843b20, player part, applied to one source.
unsafe fn zone_level(zone: *mut u8, source: *mut u8, time: f64) -> i32 {
    let zone_cluster = ancestor(parent(zone), CLUSTER);
    let source_zone = ancestor(source, ZONE);
    if zone_cluster.is_null() || source_zone.is_null() {
        return 0;
    }
    if ancestor(parent(source_zone), CLUSTER) != zone_cluster {
        return 0;
    }
    if source_zone == zone {
        return 8;
    }
    if vfunc::<Predicate>(source_zone, VF_IS_HIGHWAY)(source_zone) {
        let mut out = ptr::null_mut();
        let lookup: Lookup = core::mem::transmute(IMAGE + HIGHWAY_ZONE_OF_RVA);
        if *lookup(source_zone, &mut out, 1) == zone {
            return 7;
        }
    }
    if !vfunc::<Relation>(source_zone, VF_ZONE_ADJACENT)(source_zone, zone) {
        return 0;
    }
    if !vfunc::<Predicate>(zone, VF_ZONE_BOX_LEVELS)(zone) {
        return 7;
    }
    let position = position_in(source, zone, time);
    let defaults: Defaults = core::mem::transmute(IMAGE + DEFAULTS_RVA);
    let values = defaults(ptr::read(zone.add(DEFAULTS_SOURCE) as *const *mut u8));
    adjacent_zone_level(
        [position[0], position[1], position[2]],
        ptr::read(values.add(ZONE_DEFAULTS_BOX) as *const f32),
    )
}

/// Highway::vfunc947 0x5fe9c0 without its local-player test: the sector at either end.
unsafe fn highway_reaches(highway: *mut u8, sector: *mut u8) -> bool {
    let connections = ptr::read(highway.add(CONNECTIONS) as *const *mut u8);
    if connections.is_null() {
        return false;
    }
    let query: ConnectionQuery = core::mem::transmute(IMAGE + CONNECTION_QUERY_RVA);
    let flag = ptr::read((IMAGE + CONNECTION_FLAG_RVA) as *const u8);
    for kind in [2u32, 4u32] {
        let mut state = [0u64; 5];
        query(
            state.as_mut_ptr(),
            connections,
            kind,
            flag,
            (IMAGE + HIGHWAY_CHECKER_RVA) as *mut c_void,
        );
        if state[0] == 0 || state[1] == 0 {
            continue;
        }
        let link = ptr::read((state[1] as *const u8).add(0x20) as *const *mut u8);
        if link.is_null() {
            continue;
        }
        let end = ptr::read(link.add(0x18) as *const *mut u8);
        if !end.is_null() && ancestor(parent(end), SECTOR) == sector {
            return true;
        }
    }
    false
}

/// Sector::vfunc703 0x78d6a0: 5 in the sector, 4 when it is at the end of the source's highway.
unsafe fn sector_level(sector: *mut u8, source: *mut u8) -> i32 {
    if ancestor(source, SECTOR) == sector {
        return 5;
    }
    let zone = ancestor(source, ZONE);
    if !zone.is_null() && vfunc::<Predicate>(zone, VF_IS_HIGHWAY)(zone) && highway_reaches(zone, sector)
    {
        return 4;
    }
    0
}

/// Object::vfunc703 0x6d20b0: within the object's attention radius in an active zone.
unsafe fn proximity_level(object: *mut u8, source: *mut u8, time: f64) -> i32 {
    let zone = parent(object);
    if zone.is_null() || !is_class(zone, ZONE) {
        return 0;
    }
    let active: Active = vfunc(zone, VF_ACTIVE);
    if !active(zone, ptr::read(zone.add(ATTENTION) as *const i32), 0) {
        return 0;
    }
    let cluster = ancestor(zone, CLUSTER);
    if cluster.is_null() || cluster != ancestor(source, CLUSTER) {
        return 0;
    }
    let radius = vfunc::<Radius>(object, VF_ATTENTION_RADIUS)(object);
    let transform = position_in(source, zone, time);
    let distance: GetDistanceSq = core::mem::transmute(IMAGE + DISTANCE_SQ_RVA);
    if distance(object, transform.as_ptr(), zone, time, 2, true, 0.0) <= radius * radius {
        9
    } else {
        0
    }
}

/// First branch of Ship::ComputeAttention 0x7d5110: a ship linked (connection type 0xe) to a
/// docking bay whose defaults set +0x603 returns the bay's attention before any player test.
unsafe fn ship_inherits_attention(ship: *mut u8) -> bool {
    let state_object = ptr::read(ship.add(SHIP_DOCK_STATE) as *const *mut u8);
    if state_object.is_null()
        || !vfunc::<unsafe extern "C" fn(*mut u8, u32) -> bool>(state_object, VF_TYPE_CHECK)(
            state_object,
            0xc,
        )
    {
        return false;
    }
    let connections = ptr::read(ship.add(CONNECTIONS) as *const *mut u8);
    if connections.is_null() {
        return false;
    }
    let query: ConnectionQuery = core::mem::transmute(IMAGE + CONNECTION_QUERY_RVA);
    let mut state = [0u64; 5];
    query(
        state.as_mut_ptr(),
        connections,
        0xe,
        ptr::read((IMAGE + CONNECTION_FLAG_RVA) as *const u8),
        (IMAGE + DOCK_CHECKER_RVA) as *mut c_void,
    );
    if state[0] == 0 || state[1] == 0 {
        return false;
    }
    let link = ptr::read((state[1] as *const u8).add(0x20) as *const *mut u8);
    if link.is_null() {
        return false;
    }
    let bay = ptr::read(link.add(0x18) as *const *mut u8);
    if bay.is_null() {
        return false;
    }
    let defaults: Defaults = core::mem::transmute(IMAGE + DOCK_DEFAULTS_RVA);
    let values = defaults(ptr::read(bay.add(DEFAULTS_SOURCE) as *const *mut u8));
    ptr::read(values.add(DOCK_INHERITS_ATTENTION)) != 0
}

/// Ship::ComputeAttention 0x7d5110 / Controllable::vfunc703 0x4640e0.
unsafe fn controllable_level(object: *mut u8, source: *mut u8, time: f64) -> i32 {
    if !vfunc::<Predicate>(object, VF_IS_DOCKED)(object) && chain_contains(source, object) {
        return 9;
    }
    proximity_level(object, source, time)
}

/// Room::vfunc703 0x76e520: 11 inside the room, 10 inside its room root.
unsafe fn room_level(room: *mut u8, source: *mut u8) -> i32 {
    if chain_contains(source, room) {
        return 11;
    }
    let mut root = ptr::null_mut();
    let lookup: Lookup = core::mem::transmute(IMAGE + ROOM_ROOT_RVA);
    let root = *lookup(room, &mut root, 1);
    if !root.is_null() && root != room && chain_contains(source, root) {
        10
    } else {
        0
    }
}

unsafe fn remote_level(kind: Kind, object: *mut u8, source: *mut u8, time: f64) -> i32 {
    match kind {
        Kind::Zone => zone_level(object, source, time),
        Kind::Sector => sector_level(object, source),
        Kind::Cluster => {
            if ancestor(source, CLUSTER) == object {
                3
            } else {
                0
            }
        }
        Kind::Ship | Kind::Controllable => controllable_level(object, source, time),
        Kind::Object | Kind::Gate => proximity_level(object, source, time),
        Kind::Room => room_level(object, source),
        Kind::Walkable | Kind::SectorMove => 0,
    }
}

const fn ceiling(kind: Kind) -> i32 {
    match kind {
        Kind::Zone => 8,
        Kind::Sector => 5,
        Kind::Cluster => 3,
        Kind::Room => 11,
        _ => 9,
    }
}

#[inline(always)]
unsafe fn compute(kind: Kind, original: usize, object: *mut u8) -> i32 {
    let call: Compute = core::mem::transmute(IMAGE + original);
    let base = call(object);
    if base >= ceiling(kind) {
        return base;
    }
    let mut found = [ptr::null_mut(); MAX_SOURCES];
    let count = sources(&mut found);
    if count == 0 {
        return base;
    }
    let Some(time) = game_time() else {
        return base;
    };
    if kind == Kind::Ship && ship_inherits_attention(object) {
        return base;
    }
    let mut level = base;
    for source in &found[..count] {
        level = raise(level, remote_level(kind, object, *source, time));
        if level >= ceiling(kind) {
            break;
        }
    }
    if level != base {
        RAISES.fetch_add(1, Ordering::Relaxed);
    }
    level
}

macro_rules! wrapper {
    ($name:ident, $kind:expr, $original:expr) => {
        unsafe extern "C" fn $name(object: *mut u8) -> i32 {
            compute($kind, $original, object)
        }
    };
}
wrapper!(zone_attention, Kind::Zone, ZONE_COMPUTE_RVA);
wrapper!(ship_attention, Kind::Ship, SHIP_COMPUTE_RVA);
wrapper!(sector_attention, Kind::Sector, SECTOR_COMPUTE_RVA);
wrapper!(cluster_attention, Kind::Cluster, CLUSTER_COMPUTE_RVA);
wrapper!(controllable_attention, Kind::Controllable, CONTROLLABLE_COMPUTE_RVA);
wrapper!(object_attention, Kind::Object, OBJECT_COMPUTE_RVA);
wrapper!(gate_attention, Kind::Gate, GATE_COMPUTE_RVA);
wrapper!(room_attention, Kind::Room, ROOM_COMPUTE_RVA);

/// WalkableModule::vfunc703 0x80e590 lowers an inherited level above 8 to 7 when the local
/// player and cameras are at least 1 km away. Restore it when a remote source is near.
unsafe extern "C" fn walkable_attention(module: *mut u8) -> i32 {
    let call: Compute = core::mem::transmute(IMAGE + WALKABLE_COMPUTE_RVA);
    let base = call(module);
    if base != 7 {
        return base;
    }
    let mut found = [ptr::null_mut(); MAX_SOURCES];
    let count = sources(&mut found);
    let Some(time) = game_time() else {
        return base;
    };
    if count == 0 {
        return base;
    }
    let positional: Compute = core::mem::transmute(IMAGE + POSITIONAL_ATTENTION_RVA);
    let inherited = positional(module);
    if inherited <= 8 || !vfunc::<Predicate>(module, VF_WALKABLE_RANGE_GATE)(module) {
        return base;
    }
    let distance: DistanceSqTo = core::mem::transmute(IMAGE + DISTANCE_SQ_TO_RVA);
    for source in &found[..count] {
        if chain_contains(*source, module)
            || distance(module, *source, ptr::null_mut(), time, 2) < WALKABLE_RANGE_SQ
        {
            RAISES.fetch_add(1, Ordering::Relaxed);
            return inherited;
        }
    }
    base
}

/// Sector::Move 0x784230. At attention below 5 the periodic pass reschedules itself at
/// `now + 5 + FloatState(TLS+0x318, 1)`. Undo exactly that one draw and use a seeded value.
unsafe extern "C" fn sector_move(sector: *mut u8) {
    let original: SectorMove = core::mem::transmute(IMAGE + SECTOR_MOVE_RVA);
    let tls = thread_block();
    if !engine_ready() || tls.is_null() {
        original(sector);
        return;
    }
    let now = ptr::read(tls.add(TLS_GAME_TIME) as *const f64);
    let previous_due = ptr::read(sector.add(SECTOR_NEXT_PASS) as *const f64);
    let pending = ptr::read(sector.add(SECTOR_PASS_ENABLED)) != 0 && previous_due < now;
    let rng_before = ptr::read(tls.add(TLS_RNG) as *const u64);
    original(sector);
    if !pending {
        return;
    }
    let level = ptr::read(sector.add(ATTENTION) as *const i32);
    let due_after = ptr::read(sector.add(SECTOR_NEXT_PASS) as *const f64);
    let rng_after = ptr::read(tls.add(TLS_RNG) as *const u64);
    if level >= 5 {
        return;
    }
    let jitter = sector_jitter(
        ptr::read((IMAGE + SESSION_SEED_RVA) as *const u64),
        &sector_identity(sector, now),
        previous_due,
    );
    match fixed_sector_pass(rng_before, rng_after, now, due_after, level, jitter) {
        Some((rng, due)) => {
            ptr::write(tls.add(TLS_RNG) as *mut u64, rng);
            ptr::write(sector.add(SECTOR_NEXT_PASS) as *mut f64, due);
            JITTER_FIXED.fetch_add(1, Ordering::Relaxed);
        }
        None => {
            JITTER_SKIPPED.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// Static, instance-independent identity: the sector position in its cluster and the
/// cluster position in its parent. Component IDs are not used because they can differ.
unsafe fn sector_identity(sector: *mut u8, time: f64) -> [u32; 6] {
    let mut identity = [0u32; 6];
    let cluster = parent(sector);
    if cluster.is_null() {
        return identity;
    }
    let local = position_in(sector, cluster, time);
    let outer = parent(cluster);
    let cluster_position = if outer.is_null() {
        [0.0f32; 16]
    } else {
        position_in(cluster, outer, time)
    };
    for axis in 0..3 {
        identity[axis] = local[axis].to_bits();
        identity[axis + 3] = cluster_position[axis].to_bits();
    }
    identity
}

// ---------------------------------------------------------------- region refresh

unsafe fn region_of(object: *mut u8) -> Region {
    if object.is_null() {
        return Region::default();
    }
    Region {
        zone: component_id(ancestor(object, ZONE)),
        sector: component_id(ancestor(object, SECTOR)),
        cluster: component_id(ancestor(object, CLUSTER)),
    }
}

/// Calls `visit` for every direct child of `object` (both engine child lists) of `class`.
pub(crate) unsafe fn for_each_child(object: *mut u8, class: u32, visit: &mut dyn FnMut(*mut u8)) {
    for offset in CHILD_LISTS {
        let list = ptr::read(object.add(offset) as *const *mut u8);
        if list.is_null() || ptr::read(list.add(0x20) as *const u32) == 0 {
            continue;
        }
        let mut bucket = ptr::read(list.add(0x08) as *const *mut u8);
        let end = ptr::read(list.add(0x10) as *const *mut u8);
        let mut buckets = 0;
        while !bucket.is_null() && bucket < end && buckets < 4096 {
            let first = ptr::read(bucket as *const *mut *mut u8);
            let last = ptr::read(bucket.add(8) as *const *mut *mut u8);
            let mut entry = first;
            let mut entries = 0;
            while !entry.is_null() && entry < last && entries < 65536 {
                let child = ptr::read(entry);
                if !child.is_null() && is_class(child, class) {
                    visit(child);
                }
                entry = entry.add(1);
                entries += 1;
            }
            bucket = bucket.add(0x20);
            buckets += 1;
        }
    }
}

unsafe fn refresh(old: Region, new: Region) {
    let (clusters, sectors, zones) = changed_scopes(old, new);
    for id in clusters {
        let cluster = resolve(id);
        if !cluster.is_null() {
            set_attention(cluster);
            for_each_child(cluster, SECTOR, &mut |sector| set_attention(sector));
        }
    }
    for id in sectors {
        let sector = resolve(id);
        if !sector.is_null() {
            set_attention(sector);
            for_each_child(sector, ZONE, &mut |zone| set_attention(zone));
        }
    }
    for id in zones {
        set_attention(resolve(id));
    }
    REFRESHES.fetch_add(1, Ordering::Relaxed);
}

/// Called once per observer frame on the MoveWorker thread. Moving between zones, sectors
/// or clusters recomputes the affected space for the local player as well as for remote
/// sources, so both games trigger the same recomputations for the same movements.
pub(crate) unsafe fn frame_update() {
    if !engine_ready() {
        return;
    }
    let universe = universe();
    let local = if universe.is_null() {
        ptr::null_mut()
    } else {
        ptr::read(universe.add(PLAYER_ENTITY) as *const *mut u8)
    };
    let current = region_of(local);
    if REGIONS[0] != current {
        refresh(REGIONS[0], current);
        REGIONS[0] = current;
    }
    for index in 0..MAX_SOURCES {
        let id = SOURCES[index].load(Ordering::Acquire);
        let object = resolve(id);
        let usable = !object.is_null()
            && ptr::read(object.add(REGISTERED)) != 0
            && ptr::read(object.add(DYING)) == 0;
        let current = if usable { region_of(object) } else { Region::default() };
        if REGIONS[index + 1] != current {
            refresh(REGIONS[index + 1], current);
            REGIONS[index + 1] = current;
            if usable {
                set_attention(object);
            }
        }
    }
}

// ---------------------------------------------------------------- install / verify

fn replacement(kind: Kind) -> *const c_void {
    match kind {
        Kind::Zone => zone_attention as *const c_void,
        Kind::Ship => ship_attention as *const c_void,
        Kind::Sector => sector_attention as *const c_void,
        Kind::Cluster => cluster_attention as *const c_void,
        Kind::Controllable => controllable_attention as *const c_void,
        Kind::Object => object_attention as *const c_void,
        Kind::Gate => gate_attention as *const c_void,
        Kind::Room => room_attention as *const c_void,
        Kind::Walkable => walkable_attention as *const c_void,
        Kind::SectorMove => sector_move as *const c_void,
    }
}

/// In-memory verification of the wrapped code and of every slot value, relocation-aware.
pub(crate) unsafe fn verify_sites() -> bool {
    for &(rva, length, expected) in CODE_SITES.iter() {
        if !readable((IMAGE + rva) as *const c_void, length, false)
            || !sha256(slice::from_raw_parts((IMAGE + rva) as *const u8, length), expected)
        {
            return false;
        }
    }
    for hook in HOOKS.iter() {
        let slot = (IMAGE + hook.slot) as *const usize;
        if !readable(slot as *const c_void, 8, false) || ptr::read(slot) != IMAGE + hook.original {
            return false;
        }
    }
    readable((IMAGE + UNIVERSE_RVA) as *const c_void, 8, false)
        && readable((IMAGE + CONNECTION_FLAG_RVA) as *const c_void, 1, false)
        && readable((IMAGE + HIGHWAY_CHECKER_RVA) as *const c_void, 8, false)
        && readable((IMAGE + DOCK_CHECKER_RVA) as *const c_void, 8, false)
}

/// Undo the loader's relocation of a pointer-sized value for comparison with file bytes.
pub(crate) fn unrelocated(value: u64, image: usize) -> u64 {
    value.wrapping_sub(image as u64).wrapping_add(PREFERRED_BASE)
}

pub(crate) unsafe fn swap_slot(rva: usize, expected: *mut c_void, replacement: *mut c_void) -> bool {
    let slot = (IMAGE + rva) as *mut *mut c_void;
    if !readable(slot as *const c_void, 8, false) || ptr::read(slot) != expected {
        return false;
    }
    let mut old_protect = 0u32;
    if VirtualProtect(slot as *mut c_void, 8, PAGE_READWRITE, &mut old_protect) == 0 {
        return false;
    }
    let exchanged = (&*(slot as *const AtomicPtr<c_void>))
        .compare_exchange(expected, replacement, Ordering::SeqCst, Ordering::SeqCst)
        .is_ok();
    let restored = VirtualProtect(slot as *mut c_void, 8, old_protect, &mut old_protect) != 0;
    if exchanged && !restored {
        let _ = (&*(slot as *const AtomicPtr<c_void>)).compare_exchange(
            replacement,
            expected,
            Ordering::SeqCst,
            Ordering::SeqCst,
        );
        VirtualProtect(slot as *mut c_void, 8, old_protect, &mut old_protect);
    }
    exchanged && restored
}

fn clear_state() {
    for source in SOURCES.iter() {
        source.store(0, Ordering::Release);
    }
    unsafe {
        for index in 0..=MAX_SOURCES {
            REGIONS[index] = Region::default();
        }
    }
}

pub(crate) unsafe fn uninstall() {
    ENABLED.store(false, Ordering::Release);
    clear_state();
    for (index, hook) in HOOKS.iter().enumerate() {
        if INSTALLED[index].load(Ordering::Acquire)
            && swap_slot(
                hook.slot,
                replacement(hook.kind) as *mut c_void,
                (IMAGE + hook.original) as *mut c_void,
            )
        {
            INSTALLED[index].store(false, Ordering::Release);
        }
    }
}

pub(crate) unsafe fn install() -> bool {
    if INSTALLED.iter().any(|flag| flag.load(Ordering::Acquire)) {
        return false;
    }
    ENABLED.store(false, Ordering::Release);
    clear_state();
    for (index, hook) in HOOKS.iter().enumerate() {
        if !swap_slot(
            hook.slot,
            (IMAGE + hook.original) as *mut c_void,
            replacement(hook.kind) as *mut c_void,
        ) {
            uninstall();
            return false;
        }
        INSTALLED[index].store(true, Ordering::Release);
    }
    ENABLED.store(true, Ordering::Release);
    true
}

// ---------------------------------------------------------------- Lua interface

unsafe fn validated_source(id: u64) -> Result<*mut u8, &'static str> {
    if registry().is_null()
        || !readable(REGISTERED_REGISTRY as *const c_void, 0x158, false)
    {
        return Err("component registry unavailable");
    }
    let object = resolve(id);
    if !readable(object as *const c_void, 0x350, false)
        || ptr::read(object.add(REGISTERED)) == 0
        || ptr::read(object.add(DYING)) != 0
        || !is_class(object, SHIP)
    {
        return Err("remote proxy is not an operational ship");
    }
    Ok(object)
}

unsafe fn recompute_source(object: *mut u8) {
    if !object.is_null() {
        set_attention(ancestor(object, ZONE));
        set_attention(object);
    }
}

unsafe fn install_sources(lua: *mut LuaState, ids: [u64; MAX_SOURCES]) -> c_int {
    if CURRENT.is_null() || !(*CURRENT).armed || GetCurrentThreadId() != OWNER_THREAD || !context_valid()
    {
        return failure(lua, "shared attention unavailable on this thread");
    }
    let mut previous = [ptr::null_mut(); MAX_SOURCES];
    let previous_count = sources(&mut previous);
    let mut current = [ptr::null_mut(); MAX_SOURCES];
    for (index, id) in ids.iter().enumerate() {
        if *id != 0 {
            match validated_source(*id) {
                Ok(object) => current[index] = object,
                Err(reason) => return failure(lua, reason),
            }
        }
    }
    for (slot, id) in SOURCES.iter().zip(ids.iter()) {
        slot.store(*id, Ordering::Release);
    }
    for object in &previous[..previous_count] {
        recompute_source(*object);
    }
    for object in current {
        recompute_source(object);
    }
    (api().pushboolean.unwrap())(lua, 1);
    1
}

/// `set_remote_attention(id)`: single-proxy form kept for compatibility; `"0"` clears.
pub(crate) unsafe extern "C-unwind" fn set_remote_attention(lua: *mut LuaState) -> c_int {
    let functions = api();
    let mut length = 0usize;
    let text = (functions.checklstring.unwrap())(lua, 1, &mut length);
    let Some(id) = parse_id(slice::from_raw_parts(text as *const u8, length)) else {
        return failure(lua, "invalid remote proxy id");
    };
    let mut ids = [0u64; MAX_SOURCES];
    ids[0] = id;
    install_sources(lua, ids)
}

/// `set_remote_attention_sources({id, ...})`: up to MAX_SOURCES distinct decimal IDs.
pub(crate) unsafe extern "C-unwind" fn set_remote_attention_sources(lua: *mut LuaState) -> c_int {
    let functions = api();
    (functions.checktype.unwrap())(lua, 1, 5);
    let count = (functions.objlen.unwrap())(lua, 1);
    if count > MAX_SOURCES {
        return failure(lua, "too many remote attention sources");
    }
    let mut storage = [[0u8; 20]; MAX_SOURCES];
    let mut lengths = [0usize; MAX_SOURCES];
    for index in 0..count {
        (functions.rawgeti.unwrap())(lua, 1, index as c_int + 1);
        let mut length = 0usize;
        let text = (functions.checklstring.unwrap())(lua, -1, &mut length);
        if length > 20 {
            (functions.settop.unwrap())(lua, -2);
            return failure(lua, "invalid remote proxy id");
        }
        ptr::copy_nonoverlapping(text as *const u8, storage[index].as_mut_ptr(), length);
        lengths[index] = length;
        (functions.settop.unwrap())(lua, -2);
    }
    let mut rows: [&[u8]; MAX_SOURCES] = [&[]; MAX_SOURCES];
    for index in 0..count {
        rows[index] = &storage[index][..lengths[index]];
    }
    let Some(ids) = parse_sources(&rows[..count]) else {
        return failure(lua, "invalid or duplicate remote proxy id");
    };
    install_sources(lua, ids)
}

/// Read-only diagnostics: hook counters and local cameras anchored outside the local
/// player chain (a local-only attention input the remote game cannot reproduce).
pub(crate) unsafe extern "C-unwind" fn attention_status(lua: *mut LuaState) -> c_int {
    let functions = api();
    let mut found = [ptr::null_mut(); MAX_SOURCES];
    let resolved = sources(&mut found);
    let configured = SOURCES.iter().filter(|slot| slot.load(Ordering::Acquire) != 0).count();
    let (cameras, offchain) = if engine_ready() { camera_census() } else { (0, 0) };
    (functions.createtable.unwrap())(lua, 0, 9);
    field_number(lua, b"enabled\0", ENABLED.load(Ordering::Acquire) as u8 as f64);
    field_number(lua, b"sources\0", configured as f64);
    field_number(lua, b"resolved\0", resolved as f64);
    field_number(lua, b"raises\0", RAISES.load(Ordering::Relaxed) as f64);
    field_number(lua, b"refreshes\0", REFRESHES.load(Ordering::Relaxed) as f64);
    field_number(lua, b"jitter_fixed\0", JITTER_FIXED.load(Ordering::Relaxed) as f64);
    field_number(lua, b"jitter_skipped\0", JITTER_SKIPPED.load(Ordering::Relaxed) as f64);
    field_number(lua, b"cameras\0", cameras as f64);
    field_number(lua, b"camera_offchain\0", offchain as f64);
    1
}

/// Enabled cameras with an anchor (the engine's camera loop) and those whose anchor is not
/// the local player entity or one of its ancestors. No camera virtual is called.
unsafe fn camera_census() -> (usize, usize) {
    let universe = universe();
    if universe.is_null() {
        return (0, 0);
    }
    let count = ptr::read(universe.add(CAMERA_COUNT) as *const u64).min(32) as usize;
    let player = ptr::read(universe.add(PLAYER_ENTITY) as *const *mut u8);
    let (mut cameras, mut offchain) = (0, 0);
    for index in 0..count {
        let camera = ptr::read(universe.add(CAMERAS + index * 8) as *const *mut u8);
        if camera.is_null() || ptr::read(camera.add(CAMERA_ENABLED)) == 0 {
            continue;
        }
        let anchor = ptr::read(camera.add(CAMERA_ANCHOR) as *const *mut u8);
        if anchor.is_null() {
            continue;
        }
        cameras += 1;
        if player.is_null() || !chain_contains(player, anchor) {
            offchain += 1;
        }
    }
    (cameras, offchain)
}

/// `attention_level(id)`: read-only Component+0x1ec of any registered component (sectors,
/// stations, ships) for the STATEHASH attention bucket. Owner thread only.
pub(crate) unsafe extern "C-unwind" fn attention_level(lua: *mut LuaState) -> c_int {
    let functions = api();
    let mut length = 0usize;
    let text = (functions.checklstring.unwrap())(lua, 1, &mut length);
    if CURRENT.is_null() || !(*CURRENT).armed || GetCurrentThreadId() != OWNER_THREAD || !context_valid()
    {
        return failure(lua, "observer unavailable on this thread");
    }
    let Some(id) = parse_id(slice::from_raw_parts(text as *const u8, length)).filter(|id| *id != 0)
    else {
        return failure(lua, "invalid component id");
    };
    if registry().is_null() || !readable(REGISTERED_REGISTRY as *const c_void, 0x158, false) {
        return failure(lua, "component registry unavailable");
    }
    let object = resolve(id);
    if !readable(object as *const c_void, ATTENTION + 4, false) {
        return failure(lua, "component unavailable");
    }
    (functions.pushnumber.unwrap())(lua, ptr::read(object.add(ATTENTION) as *const i32) as f64);
    1
}

pub(crate) unsafe fn register(lua: *mut LuaState) {
    method(lua, b"set_remote_attention\0", Some(set_remote_attention));
    method(lua, b"attention_level\0", Some(attention_level));
    method(lua, b"set_remote_attention_sources\0", Some(set_remote_attention_sources));
    method(lua, b"attention_status\0", Some(attention_status));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hook_table_is_exhaustive_and_unique() {
        let mut slots: Vec<usize> = HOOKS.iter().map(|hook| hook.slot).collect();
        slots.sort_unstable();
        slots.dedup();
        assert_eq!(slots.len(), HOOKS.len());
        let count = |kind| HOOKS.iter().filter(|hook| hook.kind == kind).count();
        assert_eq!(
            [
                count(Kind::Zone),
                count(Kind::Ship),
                count(Kind::Sector),
                count(Kind::Cluster),
                count(Kind::Controllable),
                count(Kind::Object),
                count(Kind::Gate),
                count(Kind::Room),
                count(Kind::Walkable),
                count(Kind::SectorMove)
            ],
            [2, 7, 1, 1, 5, 27, 1, 4, 3, 1]
        );
        for hook in HOOKS.iter() {
            assert!(hook.slot % 8 == 0 && hook.slot < IMAGE_SIZE);
            assert!(CODE_SITES.iter().any(|site| site.0 == hook.original)
                || [ZONE_COMPUTE_RVA, SHIP_COMPUTE_RVA].contains(&hook.original));
        }
    }

    #[test]
    fn manifest_pins_every_code_site_and_slot() {
        let manifest = include_str!("../signatures.json");
        for &(rva, length, hash) in CODE_SITES.iter() {
            let row = format!("\"rva\": {rva}, \"length\": {length}, \"sha256\": \"{hash}\"");
            assert!(manifest.contains(&row), "missing manifest site {rva:#x}");
        }
        for hook in HOOKS.iter() {
            let row = format!("\"rva\": {}, \"length\": 8,", hook.slot);
            assert!(manifest.contains(&row), "missing manifest slot {:#x}", hook.slot);
        }
    }

    #[test]
    fn union_never_lowers_and_respects_ceilings() {
        for base in 1..=11 {
            for remote in 0..=11 {
                assert_eq!(raise(base, remote), base.max(remote));
                assert!(raise(base, remote) >= base);
            }
        }
        assert_eq!(ceiling(Kind::Zone), 8);
        assert_eq!(ceiling(Kind::Sector), 5);
        assert_eq!(ceiling(Kind::Cluster), 3);
        assert_eq!(ceiling(Kind::Ship), 9);
        assert_eq!(ceiling(Kind::Room), 11);
    }

    #[test]
    fn adjacent_zone_box_matches_engine_comparison() {
        assert_eq!(adjacent_zone_level([0.0, 0.0, 0.0], 0.0), 7);
        assert_eq!(adjacent_zone_level([49_999.0, -10.0, 3.0], 0.0), 7);
        assert_eq!(adjacent_zone_level([50_000.0, 0.0, 0.0], 0.0), 6);
        assert_eq!(adjacent_zone_level([0.0, -59_999.0, 0.0], 10_000.0), 7);
        assert_eq!(adjacent_zone_level([0.0, -60_000.0, 0.0], 10_000.0), 6);
        assert_eq!(adjacent_zone_level([0.0, 0.0, -60_000.0], 9_999.0), 6);
        assert_eq!(adjacent_zone_level([f32::NAN, 0.0, 0.0], 0.0), 7);
        assert_eq!(adjacent_zone_level([0.0, 0.0, 0.0], f32::NAN), 6);
    }

    #[test]
    fn pcg_step_matches_engine_formula() {
        let state = 0x0123_4567_89ab_cdefu64;
        let expected = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407)
            .rotate_right(30);
        assert_eq!(pcg_step(state), expected);
        assert_ne!(pcg_step(state), pcg_step(pcg_step(state)));
    }

    #[test]
    fn sector_jitter_is_seeded_bounded_and_stable() {
        let identity = [1u32, 2, 3, 4, 5, 6];
        let first = sector_jitter(42, &identity, 100.0);
        assert_eq!(first, sector_jitter(42, &identity, 100.0));
        assert!((0.0..1.0).contains(&first));
        assert_ne!(first, sector_jitter(43, &identity, 100.0));
        assert_ne!(first, sector_jitter(42, &[1, 2, 3, 4, 5, 7], 100.0));
        assert_ne!(first, sector_jitter(42, &identity, 105.5));
        let mut low = 0;
        for step in 0..1000 {
            let value = sector_jitter(7, &identity, step as f64);
            assert!((0.0..1.0).contains(&value));
            low += (value < 0.5) as i32;
        }
        assert!((400..600).contains(&low));
    }

    #[test]
    fn sector_pass_fix_requires_exactly_one_draw_at_low_attention() {
        let before = 0x1234_5678u64;
        let after = pcg_step(before);
        assert_eq!(
            fixed_sector_pass(before, after, 100.0, 105.25, 4, 0.5),
            Some((before, 105.5))
        );
        assert_eq!(fixed_sector_pass(before, after, 100.0, 105.25, 5, 0.5), None);
        assert_eq!(fixed_sector_pass(before, pcg_step(after), 100.0, 105.25, 4, 0.5), None);
        assert_eq!(fixed_sector_pass(before, before, 100.0, 105.25, 4, 0.5), None);
        assert_eq!(fixed_sector_pass(before, after, 100.0, 101.0, 4, 0.5), None);
        assert_eq!(fixed_sector_pass(before, after, 100.0, 106.0, 4, 0.5), None);
    }

    #[test]
    fn region_changes_select_old_and_new_scopes() {
        let a = Region { zone: 1, sector: 10, cluster: 100 };
        assert_eq!(changed_scopes(a, a), ([0; 2], [0; 2], [0; 2]));
        let zone_only = Region { zone: 2, ..a };
        assert_eq!(changed_scopes(a, zone_only), ([100, 0], [10, 0], [1, 2]));
        let sector = Region { zone: 3, sector: 11, cluster: 100 };
        assert_eq!(changed_scopes(a, sector), ([100, 0], [10, 11], [1, 3]));
        assert_eq!(
            changed_scopes(a, Region::default()),
            ([100, 0], [10, 0], [1, 0])
        );
    }

    #[test]
    fn source_lists_are_bounded_distinct_and_nonzero() {
        assert_eq!(parse_sources(&[]), Some([0; MAX_SOURCES]));
        let ids = parse_sources(&[b"5", b"18446744073709551615"]).unwrap();
        assert_eq!(&ids[..3], &[5, u64::MAX, 0]);
        assert_eq!(parse_sources(&[b"5", b"5"]), None);
        assert_eq!(parse_sources(&[b"0"]), None);
        assert_eq!(parse_sources(&[b"x"]), None);
        let many: Vec<&[u8]> = vec![b"1"; MAX_SOURCES + 1];
        assert_eq!(parse_sources(&many), None);
    }

    #[test]
    fn relocated_slot_values_compare_with_file_bytes() {
        assert_eq!(unrelocated(0x1_4084_3b20, 0x1_4000_0000), 0x1_4084_3b20);
        assert_eq!(unrelocated(0x7ff6_0084_3b20, 0x7ff6_0000_0000), 0x1_4084_3b20);
    }

    #[repr(C)]
    struct FakeComponent {
        vtable: *const *const c_void,
        class: u32,
    }

    unsafe extern "C" fn fake_is_class(object: *mut u8, class: u32) -> bool {
        (*(object as *const FakeComponent)).class == class
    }

    #[test]
    fn child_lists_visit_only_requested_class() {
        unsafe {
            let mut vtable = vec![ptr::null::<c_void>(); VF_IS_CLASS / 8 + 1];
            vtable[VF_IS_CLASS / 8] = fake_is_class as *const c_void;
            let mut zones: Vec<FakeComponent> = (0..3)
                .map(|_| FakeComponent { vtable: vtable.as_ptr(), class: ZONE })
                .collect();
            let mut other = FakeComponent { vtable: vtable.as_ptr(), class: SHIP };
            let mut first: Vec<*mut u8> = vec![
                &mut zones[0] as *mut _ as *mut u8,
                ptr::null_mut(),
                &mut other as *mut _ as *mut u8,
            ];
            let mut second: Vec<*mut u8> = vec![
                &mut zones[1] as *mut _ as *mut u8,
                &mut zones[2] as *mut _ as *mut u8,
            ];
            let mut buckets = vec![0usize; 8];
            buckets[0] = first.as_mut_ptr() as usize;
            buckets[1] = first.as_mut_ptr().add(first.len()) as usize;
            buckets[4] = second.as_mut_ptr() as usize;
            buckets[5] = second.as_mut_ptr().add(second.len()) as usize;
            let mut list = vec![0usize; 5];
            list[1] = buckets.as_mut_ptr() as usize;
            list[2] = buckets.as_mut_ptr().add(8) as usize;
            list[4] = 2;
            let mut parent = vec![0usize; 0x100 / 8];
            parent[0xa8 / 8] = list.as_mut_ptr() as usize;
            let mut seen = Vec::new();
            for_each_child(parent.as_mut_ptr() as *mut u8, ZONE, &mut |child| seen.push(child));
            assert_eq!(seen.len(), 3);
            list[4] = 0;
            seen.clear();
            for_each_child(parent.as_mut_ptr() as *mut u8, ZONE, &mut |child| seen.push(child));
            assert!(seen.is_empty());
        }
    }

    #[test]
    fn synthetic_vtable_lifecycle_restores_only_owned_slots() {
        unsafe {
            ENABLED.store(false, Ordering::Release);
            for flag in INSTALLED.iter() {
                flag.store(false, Ordering::Release);
            }
            let image = Box::leak(vec![0u8; IMAGE_SIZE].into_boxed_slice());
            IMAGE = image.as_mut_ptr() as usize;
            for hook in HOOKS.iter() {
                ptr::write((IMAGE + hook.slot) as *mut usize, IMAGE + hook.original);
            }
            assert!(install());
            assert!(ENABLED.load(Ordering::Acquire));
            for hook in HOOKS.iter() {
                assert_eq!(ptr::read((IMAGE + hook.slot) as *const *const c_void), replacement(hook.kind));
            }
            assert!(!install());
            SOURCES[0].store(77, Ordering::Release);
            uninstall();
            assert!(!ENABLED.load(Ordering::Acquire));
            assert_eq!(SOURCES[0].load(Ordering::Acquire), 0);
            for hook in HOOKS.iter() {
                assert_eq!(ptr::read((IMAGE + hook.slot) as *const usize), IMAGE + hook.original);
            }

            assert!(install());
            let foreign = 0x1234usize as *mut c_void;
            let first = HOOKS[0];
            assert!(swap_slot(first.slot, replacement(first.kind) as *mut c_void, foreign));
            uninstall();
            assert_eq!(ptr::read((IMAGE + first.slot) as *const *mut c_void), foreign);
            assert!(swap_slot(first.slot, foreign, (IMAGE + first.original) as *mut c_void));
            INSTALLED[0].store(false, Ordering::Release);

            let blocked = HOOKS[HOOKS.len() - 1];
            assert!(swap_slot(blocked.slot, (IMAGE + blocked.original) as *mut c_void, foreign));
            assert!(!install());
            assert!(!ENABLED.load(Ordering::Acquire));
            for hook in HOOKS[..HOOKS.len() - 1].iter() {
                assert_eq!(ptr::read((IMAGE + hook.slot) as *const usize), IMAGE + hook.original);
            }
            assert_eq!(ptr::read((IMAGE + blocked.slot) as *const *mut c_void), foreign);
            assert!(swap_slot(blocked.slot, foreign, (IMAGE + blocked.original) as *mut c_void));
            for flag in INSTALLED.iter() {
                flag.store(false, Ordering::Release);
            }
        }
    }

    #[test]
    fn wrong_build_or_foreign_thread_gives_no_remote_sources() {
        unsafe {
            let saved = IMAGE;
            IMAGE = 0;
            ENABLED.store(true, Ordering::Release);
            SOURCES[0].store(5, Ordering::Release);
            let mut found = [ptr::null_mut(); MAX_SOURCES];
            assert_eq!(sources(&mut found), 0);
            assert!(!verify_sites_safe());
            ENABLED.store(false, Ordering::Release);
            SOURCES[0].store(0, Ordering::Release);
            IMAGE = saved;
        }
    }

    unsafe fn verify_sites_safe() -> bool {
        IMAGE != 0 && verify_sites()
    }
}
