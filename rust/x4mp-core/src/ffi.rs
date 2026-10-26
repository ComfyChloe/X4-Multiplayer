//! Typed bindings to X4's exported C functions (the game's Lua FFI API), resolved at
//! runtime and pinned to build 23660954.
//!
//! Signatures come from the game's own `ffi.cdef` declarations (FFI-INVENTORY.md). The
//! C ABI is the platform default (`extern "C"`: SysV on Linux, Microsoft x64 on Windows);
//! `#[repr(C)]` structs are returned and passed by value exactly as LuaJIT does.
//!
//! Binding is all-or-nothing **per function**: missing, moved, different code, or a
//! compiled-out stub leaves that function unbound and every [`Capability`] that needs it
//! disabled. No call is ever made through an unverified pointer.

use std::ffi::{c_char, c_int, CStr};
use std::fmt::Write as _;

use crate::pins::{self, ExportPin, Platform};
use crate::resolve::SymbolSource;
use crate::sha256;

/// X4 component id (`typedef uint64_t UniverseID`). 0 means "none".
#[repr(transparent)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct UniverseId(pub u64);

impl UniverseId {
    pub const NONE: UniverseId = UniverseId(0);
    pub fn is_none(self) -> bool {
        self.0 == 0
    }
    fn some(self) -> Option<UniverseId> {
        (!self.is_none()).then_some(self)
    }
}

/// `typedef struct { float x, y, z, yaw, pitch, roll; } UIPosRot;`
///
/// Position in metres. Angle units are **asymmetric** per the mod's native use:
/// `GetObjectPositionInSector` returns radians, `SetObjectSectorPos` takes degrees
/// (static analysis: degrees -> radians conversion at its entry). Confidence: medium;
/// the first native run must confirm (FFI-INVENTORY.md).
#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct UiPosRot {
    pub x: f32,
    pub y: f32,
    pub z: f32,
    pub yaw: f32,
    pub pitch: f32,
    pub roll: f32,
}

impl UiPosRot {
    pub fn is_finite(&self) -> bool {
        [self.x, self.y, self.z, self.yaw, self.pitch, self.roll].iter().all(|v| v.is_finite())
    }
}

/// `typedef struct { const char* factionID; const char* factionName; const char* factionIcon; } FactionDetails;`
#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub struct FactionDetailsRaw {
    pub faction_id: *const c_char,
    pub faction_name: *const c_char,
    pub faction_icon: *const c_char,
}

/// `typedef struct { const char* name; float hull; float shield; int speed; bool hasShield; } ComponentDetails;`
#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub struct ComponentDetailsRaw {
    pub name: *const c_char,
    pub hull: f32,
    pub shield: f32,
    pub speed: c_int,
    pub has_shield: bool,
}

/// `typedef struct { int major; int minor; } GameVersion;`
#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct GameVersion {
    pub major: c_int,
    pub minor: c_int,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct FactionDetails {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct ComponentDetails {
    pub name: String,
    pub hull: f32,
    pub shield: f32,
    /// Scalar speed in m/s as the target monitor shows it (integer).
    pub speed: i32,
    pub has_shield: bool,
}

/// Groups of exports that are only useful together.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u32)]
pub enum Capability {
    Clock = 1 << 0,
    Player = 1 << 1,
    PoseRead = 1 << 2,
    PoseWrite = 1 << 3,
    Identity = 1 << 4,
    Owner = 1 << 5,
    Details = 1 << 6,
    Version = 1 << 7,
    Offset = 1 << 8,
    PlayerShip = 1 << 9,
}

impl Capability {
    pub const ALL: [Capability; 10] = [
        Capability::Clock,
        Capability::PlayerShip,
        Capability::Player,
        Capability::PoseRead,
        Capability::PoseWrite,
        Capability::Identity,
        Capability::Owner,
        Capability::Details,
        Capability::Version,
        Capability::Offset,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Capability::Clock => "clock",
            Capability::PlayerShip => "player_ship",
            Capability::Player => "player",
            Capability::PoseRead => "pose_read",
            Capability::PoseWrite => "pose_write",
            Capability::Identity => "identity",
            Capability::Owner => "owner",
            Capability::Details => "details",
            Capability::Version => "version",
            Capability::Offset => "offset",
        }
    }

    /// Exports required for this capability.
    pub fn needs(self) -> &'static [&'static CStr] {
        match self {
            Capability::Clock => &[c"GetCurrentGameTime", c"IsGamePaused"],
            Capability::PlayerShip => &[c"GetPlayerOccupiedShipID", c"GetPlayerControlledShipID"],
            Capability::Player => {
                &[c"IsPlayerValid", c"GetPlayerID", c"GetPlayerObjectID", c"GetPlayerContainerID", c"GetPlayerZoneID"]
            }
            Capability::PoseRead => &[c"GetObjectPositionInSector", c"GetContextByClass"],
            Capability::PoseWrite => &[c"SetObjectSectorPos", c"GetContextByClass"],
            Capability::Identity => &[
                c"GetComponentName",
                c"GetObjectIDCode",
                c"ConvertStringTo64Bit",
                c"GetComponentClass",
                c"IsComponentClass",
                c"IsValidComponent",
                c"IsComponentOperational",
                c"IsComponentWrecked",
            ],
            Capability::Owner => &[c"GetOwnerDetails", c"GetPlayerFactionName"],
            Capability::Details => &[c"GetComponentDetails"],
            Capability::Version => &[c"GetGameVersion"],
            Capability::Offset => &[c"GetPositionalOffset"],
        }
    }
}

/// Set of enabled capabilities.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Capabilities(pub u32);

impl Capabilities {
    pub const NONE: Capabilities = Capabilities(0);
    pub fn of(list: &[Capability]) -> Self {
        Capabilities(list.iter().fold(0, |acc, c| acc | *c as u32))
    }
    pub fn has(self, c: Capability) -> bool {
        self.0 & c as u32 != 0
    }
    pub fn contains(self, other: Capabilities) -> bool {
        self.0 & other.0 == other.0
    }
    pub fn names(self) -> String {
        let names: Vec<&str> = Capability::ALL.iter().filter(|c| self.has(**c)).map(|c| c.name()).collect();
        if names.is_empty() {
            "none".into()
        } else {
            names.join(",")
        }
    }
}

/// Caller's promise that the running executable is the pinned build of `platform`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VerifiedBuild {
    platform: Platform,
    steam_build: u32,
}

impl VerifiedBuild {
    /// # Safety
    /// Only a host that has verified the executable file (size and SHA-256 of the pinned
    /// build in [`crate::pins`]) for `platform` may create this.
    pub unsafe fn assert_verified(platform: Platform, steam_build: u32) -> Self {
        VerifiedBuild { platform, steam_build }
    }
    pub fn platform(&self) -> Platform {
        self.platform
    }
}

/// Outcome of binding one export.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BindStatus {
    Bound,
    /// Not in the pin table for this core (programming error in a custom table).
    NotPinned,
    WrongBuild,
    Missing,
    AddressMismatch {
        found_offset: u64,
    },
    CodeUnreadable,
    CodeMismatch,
    /// The code is a bare `ret`: a compiled-out stub.
    Stub,
}

impl BindStatus {
    pub fn reason(&self) -> String {
        match self {
            BindStatus::Bound => "bound".into(),
            BindStatus::NotPinned => "not_pinned".into(),
            BindStatus::WrongBuild => "wrong_build".into(),
            BindStatus::Missing => "missing".into(),
            BindStatus::AddressMismatch { found_offset } => format!("address_mismatch:0x{found_offset:x}"),
            BindStatus::CodeUnreadable => "code_unreadable".into(),
            BindStatus::CodeMismatch => "code_mismatch".into(),
            BindStatus::Stub => "stub".into(),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct BindReport {
    pub entries: Vec<(&'static CStr, BindStatus)>,
    pub capabilities: Capabilities,
}

impl BindReport {
    pub fn bound(&self) -> usize {
        self.entries.iter().filter(|(_, s)| *s == BindStatus::Bound).count()
    }
    pub fn status(&self, name: &CStr) -> Option<&BindStatus> {
        self.entries.iter().find(|(n, _)| *n == name).map(|(_, s)| s)
    }
    /// One summary line plus one line per unbound export.
    pub fn lines(&self, platform: Platform) -> Vec<String> {
        let mut out = vec![format!(
            "X4MP_CORE BIND platform={} build={} bound={}/{} capabilities={}",
            platform.name(),
            pins::STEAM_BUILD,
            self.bound(),
            self.entries.len(),
            self.capabilities.names()
        )];
        for (name, status) in &self.entries {
            if *status != BindStatus::Bound {
                out.push(format!("X4MP_CORE UNBOUND name={} reason={}", name.to_string_lossy(), status.reason()));
            }
        }
        out
    }
}

fn is_stub(head: &[u8]) -> bool {
    head.first() == Some(&0xc3) || head.starts_with(&[0xc2, 0x00, 0x00])
}

/// Check one export against its pin. Returns the verified address.
pub fn check(source: &dyn SymbolSource, build: &VerifiedBuild, pin: &ExportPin) -> Result<usize, BindStatus> {
    let platform = source.platform();
    if build.platform != platform || build.steam_build != pins::STEAM_BUILD {
        return Err(BindStatus::WrongBuild);
    }
    let address = source.lookup(pin.name).ok_or(BindStatus::Missing)?;
    let offset = (address as u64).wrapping_sub(source.image_base() as u64);
    if offset != pin.offset(platform) {
        return Err(BindStatus::AddressMismatch { found_offset: offset });
    }
    let head = source.read(address, pins::HEAD_LEN).ok_or(BindStatus::CodeUnreadable)?;
    if head.len() != pins::HEAD_LEN {
        return Err(BindStatus::CodeUnreadable);
    }
    if is_stub(&head) {
        return Err(BindStatus::Stub);
    }
    let mut hasher = sha256::Sha256::new();
    hasher.update(&head);
    if sha256::hex(&hasher.finish()) != pin.head(platform) {
        return Err(BindStatus::CodeMismatch);
    }
    Ok(address)
}

macro_rules! api {
    ($( $name:ident : fn($($arg:ty),*) $(-> $ret:ty)? ;)*) => {
        /// Bound export pointers; `None` = not bound (see [`BindReport`]).
        #[allow(non_snake_case)]
        #[derive(Clone, Copy, Default)]
        pub struct Api {
            $( pub $name: Option<unsafe extern "C" fn($($arg),*) $(-> $ret)?>, )*
            capabilities: Capabilities,
        }

        impl Api {
            /// Names of all bindable exports, in binding order.
            pub const NAMES: &'static [&'static CStr] = &[$( {
                // SAFETY: literal with exactly one trailing NUL.
                unsafe { CStr::from_bytes_with_nul_unchecked(concat!(stringify!($name), "\0").as_bytes()) }
            }, )*];

            /// Bind against an explicit pin table (tests use fake tables).
            pub fn bind_with(source: &dyn SymbolSource, build: &VerifiedBuild, table: &[ExportPin]) -> (Api, BindReport) {
                let mut api = Api::default();
                let mut report = BindReport::default();
                $(
                    let name = unsafe { CStr::from_bytes_with_nul_unchecked(concat!(stringify!($name), "\0").as_bytes()) };
                    let result = match pins::find(table, name) {
                        None => Err(BindStatus::NotPinned),
                        Some(pin) => check(source, build, &pin),
                    };
                    match result {
                        Ok(address) => {
                            // SAFETY: the address is the pinned export of the verified build
                            // and its code head matches; the type is its game-declared signature.
                            api.$name = Some(unsafe {
                                std::mem::transmute::<usize, unsafe extern "C" fn($($arg),*) $(-> $ret)?>(address)
                            });
                            report.entries.push((name, BindStatus::Bound));
                        }
                        Err(status) => report.entries.push((name, status)),
                    }
                )*
                let bound: Vec<&CStr> = report.entries.iter().filter(|(_, s)| *s == BindStatus::Bound).map(|(n, _)| *n).collect();
                let mut caps = 0;
                for capability in Capability::ALL {
                    if capability.needs().iter().all(|need| bound.contains(need)) {
                        caps |= capability as u32;
                    }
                }
                api.capabilities = Capabilities(caps);
                report.capabilities = api.capabilities;
                (api, report)
            }
        }
    };
}

api! {
    GetCurrentGameTime: fn() -> f64;
    IsGamePaused: fn() -> bool;
    IsPlayerValid: fn() -> bool;
    GetPlayerID: fn() -> UniverseId;
    GetPlayerOccupiedShipID: fn() -> UniverseId;
    GetPlayerControlledShipID: fn() -> UniverseId;
    GetPlayerObjectID: fn() -> UniverseId;
    GetPlayerContainerID: fn() -> UniverseId;
    GetPlayerZoneID: fn() -> UniverseId;
    GetObjectPositionInSector: fn(UniverseId) -> UiPosRot;
    SetObjectSectorPos: fn(UniverseId, UniverseId, UiPosRot);
    GetPositionalOffset: fn(UniverseId, UniverseId) -> UiPosRot;
    GetContextByClass: fn(UniverseId, *const c_char, bool) -> UniverseId;
    GetComponentName: fn(UniverseId) -> *const c_char;
    GetObjectIDCode: fn(UniverseId) -> *const c_char;
    ConvertStringTo64Bit: fn(*const c_char) -> u64;
    GetComponentClass: fn(UniverseId) -> *const c_char;
    IsComponentClass: fn(UniverseId, *const c_char) -> bool;
    IsValidComponent: fn(UniverseId) -> bool;
    IsComponentOperational: fn(UniverseId) -> bool;
    IsComponentWrecked: fn(UniverseId) -> bool;
    GetComponentDetails: fn(UniverseId, *const c_char) -> ComponentDetailsRaw;
    GetOwnerDetails: fn(UniverseId) -> FactionDetailsRaw;
    GetPlayerFactionName: fn(bool) -> *const c_char;
    GetGameVersion: fn() -> GameVersion;
}

impl Api {
    /// Bind against the pinned build table.
    pub fn bind(source: &dyn SymbolSource, build: &VerifiedBuild) -> (Api, BindReport) {
        Api::bind_with(source, build, pins::EXPORTS)
    }
    pub fn capabilities(&self) -> Capabilities {
        self.capabilities
    }
}

/// Longest engine string copied (names, id codes, classes, faction ids).
pub const MAX_STRING: usize = 256;

/// Copy a NUL-terminated engine string (at most [`MAX_STRING`] bytes).
///
/// # Safety
/// `p` is null or points to a readable NUL-terminated string owned by the engine.
pub unsafe fn copy_cstr(p: *const c_char) -> Option<String> {
    if p.is_null() {
        return None;
    }
    let mut len = 0;
    while len < MAX_STRING && *p.add(len) != 0 {
        len += 1;
    }
    let bytes = std::slice::from_raw_parts(p as *const u8, len);
    Some(String::from_utf8_lossy(bytes).into_owned())
}

/// A capability or export the call needed is not available.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Unavailable(pub &'static str);

/// Engine access, valid only on the engine thread for the duration of one frame.
/// Created by the host through [`Engine::enter`]; subsystems receive `&Engine`.
pub struct Engine<'a> {
    api: &'a Api,
    writes: bool,
}

macro_rules! call {
    ($self:ident . $name:ident ( $($arg:expr),* )) => {{
        let f = $self.api.$name.ok_or(Unavailable(stringify!($name)))?;
        // SAFETY: bound to the verified pinned export with its declared signature, and
        // `Engine` only exists inside the frame callback on the engine thread.
        unsafe { f($($arg),*) }
    }};
}

impl<'a> Engine<'a> {
    /// # Safety
    /// Call only on X4's MoveWorker thread inside a per-frame callback after slot 3 (or
    /// from the UI `onUpdate` on that thread), and drop the `Engine` before returning.
    pub unsafe fn enter(api: &'a Api, writes: bool) -> Self {
        Engine { api, writes }
    }

    pub fn capabilities(&self) -> Capabilities {
        self.api.capabilities
    }
    pub fn writes_allowed(&self) -> bool {
        self.writes
    }

    /// TLS game time of the calling thread (the time base of pose reads).
    pub fn game_time(&self) -> Result<f64, Unavailable> {
        Ok(call!(self.GetCurrentGameTime()))
    }
    pub fn paused(&self) -> Result<bool, Unavailable> {
        Ok(call!(self.IsGamePaused()))
    }
    pub fn player_valid(&self) -> Result<bool, Unavailable> {
        Ok(call!(self.IsPlayerValid()))
    }
    pub fn player_entity(&self) -> Result<Option<UniverseId>, Unavailable> {
        Ok(call!(self.GetPlayerID()).some())
    }
    /// Ship the player is aboard (piloting or not).
    pub fn player_occupied_ship(&self) -> Result<Option<UniverseId>, Unavailable> {
        Ok(call!(self.GetPlayerOccupiedShipID()).some())
    }
    /// Ship the player is piloting.
    pub fn player_controlled_ship(&self) -> Result<Option<UniverseId>, Unavailable> {
        Ok(call!(self.GetPlayerControlledShipID()).some())
    }
    pub fn player_object(&self) -> Result<Option<UniverseId>, Unavailable> {
        Ok(call!(self.GetPlayerObjectID()).some())
    }
    pub fn player_container(&self) -> Result<Option<UniverseId>, Unavailable> {
        Ok(call!(self.GetPlayerContainerID()).some())
    }
    pub fn player_zone(&self) -> Result<Option<UniverseId>, Unavailable> {
        Ok(call!(self.GetPlayerZoneID()).some())
    }

    /// Nearest ancestor (or self with `include_self`) of class `class`, e.g. `c"sector"`.
    pub fn context(&self, id: UniverseId, class: &CStr, include_self: bool) -> Result<Option<UniverseId>, Unavailable> {
        Ok(call!(self.GetContextByClass(id, class.as_ptr(), include_self)).some())
    }
    pub fn sector_of(&self, id: UniverseId) -> Result<Option<UniverseId>, Unavailable> {
        self.context(id, c"sector", false)
    }
    /// Pose in the coordinates of the object's sector; angles in radians (see [`UiPosRot`]).
    pub fn pose_in_sector(&self, id: UniverseId) -> Result<UiPosRot, Unavailable> {
        Ok(call!(self.GetObjectPositionInSector(id)))
    }
    pub fn offset_relative(&self, id: UniverseId, space: UniverseId) -> Result<UiPosRot, Unavailable> {
        Ok(call!(self.GetPositionalOffset(id, space)))
    }
    /// Write a sector pose; `pose` angles in radians, converted to the degrees the export
    /// takes. Refused unless the host enabled writes.
    pub fn set_pose_in_sector(&self, id: UniverseId, sector: UniverseId, pose: UiPosRot) -> Result<(), Unavailable> {
        if !self.writes {
            return Err(Unavailable("writes_disabled"));
        }
        let deg = 180.0 / std::f32::consts::PI;
        let degrees = UiPosRot { yaw: pose.yaw * deg, pitch: pose.pitch * deg, roll: pose.roll * deg, ..pose };
        call!(self.SetObjectSectorPos(id, sector, degrees));
        Ok(())
    }

    pub fn component_name(&self, id: UniverseId) -> Result<Option<String>, Unavailable> {
        let p = call!(self.GetComponentName(id));
        // SAFETY: engine-owned string, copied immediately.
        Ok(unsafe { copy_cstr(p) })
    }
    pub fn id_code(&self, id: UniverseId) -> Result<Option<String>, Unavailable> {
        let p = call!(self.GetObjectIDCode(id));
        // SAFETY: engine-owned string, copied immediately.
        Ok(unsafe { copy_cstr(p) })
    }
    pub fn component_class(&self, id: UniverseId) -> Result<Option<String>, Unavailable> {
        let p = call!(self.GetComponentClass(id));
        // SAFETY: engine-owned string, copied immediately.
        Ok(unsafe { copy_cstr(p) })
    }
    /// `ConvertStringTo64Bit("ID: 1234")`-style lookup of a Lua/MD id string.
    pub fn convert_id(&self, text: &CStr) -> Result<Option<UniverseId>, Unavailable> {
        Ok(UniverseId(call!(self.ConvertStringTo64Bit(text.as_ptr()))).some())
    }
    pub fn is_class(&self, id: UniverseId, class: &CStr) -> Result<bool, Unavailable> {
        Ok(call!(self.IsComponentClass(id, class.as_ptr())))
    }
    pub fn is_valid(&self, id: UniverseId) -> Result<bool, Unavailable> {
        Ok(call!(self.IsValidComponent(id)))
    }
    pub fn is_operational(&self, id: UniverseId) -> Result<bool, Unavailable> {
        Ok(call!(self.IsComponentOperational(id)))
    }
    pub fn is_wrecked(&self, id: UniverseId) -> Result<bool, Unavailable> {
        Ok(call!(self.IsComponentWrecked(id)))
    }
    /// Target-monitor details; `speed` is the only exported (scalar) velocity value.
    pub fn details(&self, id: UniverseId) -> Result<ComponentDetails, Unavailable> {
        let raw = call!(self.GetComponentDetails(id, c"".as_ptr()));
        Ok(ComponentDetails {
            name: unsafe { copy_cstr(raw.name) }.unwrap_or_default(),
            hull: raw.hull,
            shield: raw.shield,
            speed: raw.speed,
            has_shield: raw.has_shield,
        })
    }
    pub fn owner(&self, id: UniverseId) -> Result<FactionDetails, Unavailable> {
        let raw = call!(self.GetOwnerDetails(id));
        Ok(FactionDetails {
            id: unsafe { copy_cstr(raw.faction_id) }.unwrap_or_default(),
            name: unsafe { copy_cstr(raw.faction_name) }.unwrap_or_default(),
        })
    }
    pub fn player_faction_name(&self, raw_name: bool) -> Result<Option<String>, Unavailable> {
        let p = call!(self.GetPlayerFactionName(raw_name));
        // SAFETY: engine-owned string, copied immediately.
        Ok(unsafe { copy_cstr(p) })
    }
    pub fn version(&self) -> Result<GameVersion, Unavailable> {
        Ok(call!(self.GetGameVersion()))
    }
}

/// Human-readable id for logs.
pub fn fmt_id(id: Option<UniverseId>) -> String {
    let mut s = String::new();
    match id {
        Some(UniverseId(v)) => {
            let _ = write!(s, "0x{v:x}");
        }
        None => s.push('0'),
    }
    s
}

#[cfg(test)]
pub(crate) mod fake {
    //! A fake engine: fake export functions, a fake symbol table and pin table built
    //! from their real addresses and code bytes. Shared by tests of other modules.
    use super::*;
    use std::cell::RefCell;
    use std::collections::HashMap;

    #[derive(Debug, Clone, Default)]
    pub struct World {
        pub game_time: f64,
        pub paused: bool,
        pub player_valid: bool,
        pub occupied: u64,
        pub controlled: u64,
        pub sector_of: HashMap<u64, u64>,
        pub poses: HashMap<u64, UiPosRot>,
        pub writes: Vec<(u64, u64, UiPosRot)>,
        pub calls: u64,
        pub speed: i32,
    }

    thread_local! {
        pub static WORLD: RefCell<World> = RefCell::new(World::default());
    }

    pub fn with<R>(f: impl FnOnce(&mut World) -> R) -> R {
        WORLD.with(|w| {
            let mut w = w.borrow_mut();
            w.calls += 1;
            f(&mut w)
        })
    }

    extern "C" fn get_current_game_time() -> f64 {
        with(|w| w.game_time)
    }
    extern "C" fn is_game_paused() -> bool {
        with(|w| w.paused)
    }
    extern "C" fn is_player_valid() -> bool {
        with(|w| w.player_valid)
    }
    extern "C" fn get_player_occupied_ship_id() -> UniverseId {
        with(|w| UniverseId(w.occupied))
    }
    extern "C" fn get_player_controlled_ship_id() -> UniverseId {
        with(|w| UniverseId(w.controlled))
    }
    extern "C" fn get_object_position_in_sector(id: UniverseId) -> UiPosRot {
        with(|w| w.poses.get(&id.0).copied().unwrap_or_default())
    }
    extern "C" fn set_object_sector_pos(id: UniverseId, sector: UniverseId, pose: UiPosRot) {
        with(|w| w.writes.push((id.0, sector.0, pose)))
    }
    extern "C" fn get_context_by_class(id: UniverseId, class: *const c_char, _include_self: bool) -> UniverseId {
        let class = unsafe { CStr::from_ptr(class) };
        assert_eq!(class, c"sector");
        with(|w| UniverseId(w.sector_of.get(&id.0).copied().unwrap_or(0)))
    }
    extern "C" fn get_object_id_code(id: UniverseId) -> *const c_char {
        static CODE: &CStr = c"ABC-123";
        if id.0 == 0 {
            std::ptr::null()
        } else {
            CODE.as_ptr()
        }
    }
    extern "C" fn get_component_details(_id: UniverseId, _connection: *const c_char) -> ComponentDetailsRaw {
        ComponentDetailsRaw {
            name: c"Fake Ship".as_ptr(),
            hull: 100.0,
            shield: 50.0,
            speed: with(|w| w.speed),
            has_shield: true,
        }
    }
    extern "C" fn get_game_version() -> GameVersion {
        GameVersion { major: 9, minor: 0 }
    }

    /// Fake process: exports at their real (test binary) addresses.
    pub struct Symbols {
        pub platform: Platform,
        pub base: usize,
        pub table: HashMap<&'static CStr, usize>,
        /// Code bytes the fake "process" returns per address (override for tampering).
        pub code: HashMap<usize, Vec<u8>>,
    }

    pub fn functions() -> Vec<(&'static CStr, usize)> {
        vec![
            (c"GetCurrentGameTime", get_current_game_time as *const () as usize),
            (c"IsGamePaused", is_game_paused as *const () as usize),
            (c"IsPlayerValid", is_player_valid as *const () as usize),
            (c"GetPlayerOccupiedShipID", get_player_occupied_ship_id as *const () as usize),
            (c"GetPlayerControlledShipID", get_player_controlled_ship_id as *const () as usize),
            (c"GetObjectPositionInSector", get_object_position_in_sector as *const () as usize),
            (c"SetObjectSectorPos", set_object_sector_pos as *const () as usize),
            (c"GetContextByClass", get_context_by_class as *const () as usize),
            (c"GetObjectIDCode", get_object_id_code as *const () as usize),
            (c"GetComponentDetails", get_component_details as *const () as usize),
            (c"GetGameVersion", get_game_version as *const () as usize),
        ]
    }

    /// Distinct synthetic code heads per function (the fakes' real bytes may be shared
    /// thunks, and a fake head can never look like a `ret` stub).
    pub fn head_for(address: usize) -> Vec<u8> {
        let mut head = vec![0x55u8; pins::HEAD_LEN];
        head[1..9].copy_from_slice(&(address as u64).to_le_bytes());
        head
    }

    pub fn digest(bytes: &[u8]) -> String {
        let mut h = sha256::Sha256::new();
        h.update(bytes);
        sha256::hex(&h.finish())
    }

    impl Symbols {
        /// Linux: base 0 (absolute addresses). Windows: a base just below the fakes so
        /// every RVA fits in `u32`.
        pub fn new(platform: Platform) -> Self {
            let functions = functions();
            let lowest = functions.iter().map(|(_, a)| *a).min().unwrap();
            let base = match platform {
                Platform::Linux => 0,
                Platform::Windows => (lowest & !0xfff) - 0x1000,
            };
            let code = functions.iter().map(|(_, a)| (*a, head_for(*a))).collect();
            Symbols { platform, base, table: functions.into_iter().collect(), code }
        }

        /// Pin table matching this fake process exactly.
        pub fn pins(&self) -> Vec<ExportPin> {
            self.table
                .iter()
                .map(|(name, address)| {
                    let offset = (*address - self.base) as u64;
                    let head: &'static str = Box::leak(digest(&self.code[address]).into_boxed_str());
                    ExportPin { name, linux_va: offset, linux_head: head, win_rva: offset as u32, win_head: head }
                })
                .collect()
        }
    }

    impl SymbolSource for Symbols {
        fn platform(&self) -> Platform {
            self.platform
        }
        fn image_base(&self) -> usize {
            self.base
        }
        fn lookup(&self, name: &CStr) -> Option<usize> {
            self.table.get(name).copied()
        }
        fn read(&self, address: usize, len: usize) -> Option<Vec<u8>> {
            self.code.get(&address).filter(|c| c.len() >= len).map(|c| c[..len].to_vec())
        }
    }

    pub fn build(platform: Platform) -> VerifiedBuild {
        unsafe { VerifiedBuild::assert_verified(platform, pins::STEAM_BUILD) }
    }

    /// A bound fake API for the given platform (base 0 for Linux).
    pub fn api() -> (Api, BindReport) {
        let symbols = Symbols::new(Platform::Linux);
        let pins = symbols.pins();
        Api::bind_with(&symbols, &build(Platform::Linux), &pins)
    }

    pub fn reset(world: World) {
        WORLD.with(|w| *w.borrow_mut() = world);
    }
}

#[cfg(test)]
mod tests {
    use super::fake::*;
    use super::*;

    #[test]
    fn struct_layouts_match_the_game_cdefs() {
        assert_eq!(std::mem::size_of::<UiPosRot>(), 24);
        assert_eq!(std::mem::size_of::<FactionDetailsRaw>(), 24);
        assert_eq!(std::mem::size_of::<ComponentDetailsRaw>(), 24);
        assert_eq!(std::mem::offset_of!(ComponentDetailsRaw, speed), 16);
        assert_eq!(std::mem::offset_of!(ComponentDetailsRaw, has_shield), 20);
        assert_eq!(std::mem::size_of::<GameVersion>(), 8);
        assert_eq!(std::mem::size_of::<UniverseId>(), 8);
    }

    #[test]
    fn every_bindable_export_is_pinned_and_vice_versa() {
        for name in Api::NAMES {
            assert!(pins::find(pins::EXPORTS, name).is_some(), "{name:?} not pinned");
        }
        for pin in pins::EXPORTS {
            assert!(Api::NAMES.contains(&pin.name), "{:?} pinned but not bindable", pin.name);
        }
        for capability in Capability::ALL {
            for need in capability.needs() {
                assert!(Api::NAMES.contains(need), "{need:?}");
            }
        }
    }

    #[test]
    fn fake_engine_binds_what_exists_and_disables_the_rest() {
        let (api, report) = api();
        assert_eq!(report.status(c"IsGamePaused"), Some(&BindStatus::Bound));
        // Present in the fake process.
        assert!(api.capabilities().has(Capability::Clock));
        assert!(api.capabilities().has(Capability::PoseRead));
        assert!(api.capabilities().has(Capability::PoseWrite));
        assert!(api.capabilities().has(Capability::Details));
        // GetPlayerID etc. are absent from the fake pin table: Player is disabled, no crash.
        assert_eq!(report.status(c"GetPlayerID"), Some(&BindStatus::NotPinned));
        assert!(!api.capabilities().has(Capability::Player));
        assert!(api.GetPlayerID.is_none());
        let engine = unsafe { Engine::enter(&api, false) };
        assert_eq!(engine.player_entity(), Err(Unavailable("GetPlayerID")));
        reset(World { game_time: 12.5, occupied: 7, ..Default::default() });
        assert_eq!(engine.game_time(), Ok(12.5));
        assert_eq!(engine.player_occupied_ship(), Ok(Some(UniverseId(7))));
        assert_eq!(engine.id_code(UniverseId(7)), Ok(Some("ABC-123".into())));
        assert_eq!(engine.id_code(UniverseId(0)), Ok(None));
        assert_eq!(engine.version(), Ok(GameVersion { major: 9, minor: 0 }));
        let lines = report.lines(Platform::Linux);
        assert!(lines[0].starts_with("X4MP_CORE BIND platform=linux build=23660954 bound=11/25 "), "{}", lines[0]);
        assert!(lines.iter().any(|l| l == "X4MP_CORE UNBOUND name=GetPlayerID reason=not_pinned"));
    }

    #[test]
    fn missing_symbol_disables_only_its_capabilities() {
        let mut symbols = Symbols::new(Platform::Linux);
        let pins = symbols.pins();
        symbols.table.remove(c"GetObjectPositionInSector");
        let (api, report) = Api::bind_with(&symbols, &build(Platform::Linux), &pins);
        assert_eq!(report.status(c"GetObjectPositionInSector"), Some(&BindStatus::Missing));
        assert!(!api.capabilities().has(Capability::PoseRead));
        assert!(api.capabilities().has(Capability::PoseWrite));
        assert!(api.capabilities().has(Capability::Clock));
        let engine = unsafe { Engine::enter(&api, true) };
        assert_eq!(engine.pose_in_sector(UniverseId(1)), Err(Unavailable("GetObjectPositionInSector")));
    }

    #[test]
    fn moved_tampered_unreadable_or_stub_code_is_refused() {
        let symbols = Symbols::new(Platform::Windows);
        let build = build(Platform::Windows);
        let good = pins::find(&symbols.pins(), c"IsGamePaused").unwrap();
        assert!(check(&symbols, &build, &good).is_ok());

        let moved = ExportPin { win_rva: good.win_rva + 16, ..good };
        assert!(matches!(check(&symbols, &build, &moved), Err(BindStatus::AddressMismatch { .. })));

        let address = symbols.table[c"IsGamePaused"];
        let mut tampered = Symbols::new(Platform::Windows);
        tampered.code.get_mut(&address).unwrap()[15] ^= 1;
        assert_eq!(check(&tampered, &build, &good), Err(BindStatus::CodeMismatch));

        let mut stub = Symbols::new(Platform::Windows);
        stub.code.insert(address, {
            let mut c = vec![0xc3];
            c.resize(pins::HEAD_LEN, 0xcc);
            c
        });
        let stub_pin = ExportPin { win_head: Box::leak(digest(&stub.code[&address]).into_boxed_str()), ..good };
        assert_eq!(check(&stub, &build, &stub_pin), Err(BindStatus::Stub));

        let mut unreadable = Symbols::new(Platform::Windows);
        unreadable.code.remove(&address);
        assert_eq!(check(&unreadable, &build, &good), Err(BindStatus::CodeUnreadable));
    }

    #[test]
    fn wrong_platform_or_build_binds_nothing() {
        let symbols = Symbols::new(Platform::Linux);
        let pins = symbols.pins();
        let other = build(Platform::Windows);
        let (api, report) = Api::bind_with(&symbols, &other, &pins);
        assert_eq!(report.bound(), 0);
        assert_eq!(api.capabilities(), Capabilities::NONE);
        let old = unsafe { VerifiedBuild::assert_verified(Platform::Linux, 1) };
        assert_eq!(Api::bind_with(&symbols, &old, &pins).1.bound(), 0);
    }

    #[test]
    fn real_pin_table_never_matches_a_foreign_process() {
        // The fake functions are not at the pinned X4 addresses: nothing binds.
        let symbols = Symbols::new(Platform::Linux);
        let (api, report) = Api::bind(&symbols, &build(Platform::Linux));
        assert_eq!(report.bound(), 0);
        assert_eq!(api.capabilities(), Capabilities::NONE);
    }

    #[test]
    fn pose_writes_need_the_host_flag_and_convert_to_degrees() {
        let (api, _) = api();
        reset(World::default());
        let pose = UiPosRot { x: 1.0, y: 2.0, z: 3.0, yaw: std::f32::consts::PI, pitch: 0.0, roll: 0.0 };
        let read_only = unsafe { Engine::enter(&api, false) };
        assert_eq!(
            read_only.set_pose_in_sector(UniverseId(1), UniverseId(2), pose),
            Err(Unavailable("writes_disabled"))
        );
        assert!(with(|w| w.writes.is_empty()));
        let writer = unsafe { Engine::enter(&api, true) };
        writer.set_pose_in_sector(UniverseId(1), UniverseId(2), pose).unwrap();
        let (id, sector, written) = with(|w| w.writes[0]);
        assert_eq!((id, sector), (1, 2));
        assert!((written.yaw - 180.0).abs() < 1e-3 && written.x == 1.0);
    }

    #[test]
    fn strings_are_bounded() {
        let long = std::ffi::CString::new(vec![b'a'; MAX_STRING * 2]).unwrap();
        assert_eq!(unsafe { copy_cstr(long.as_ptr()) }.unwrap().len(), MAX_STRING);
        assert_eq!(unsafe { copy_cstr(std::ptr::null()) }, None);
    }
}
