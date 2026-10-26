//! Identifiers used by the economy model.
//!
//! `UniverseId`, `SectorIx` and `OrderKey` are the ecs-core types of `x4mp_sim::id`
//! (rewrite-coord/interfaces.md v0.5); the economy adds ware indices, trade ids and stamps.

pub use x4mp_sim::id::{OrderKey, SectorIx, UniverseId};

/// Dense index into a [`crate::ware::WareTable`] (the engine keys wares by `WareClass*`;
/// captures carry the ware id string and the table assigns the index).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct WareIx(pub u16);

/// Engine trade offer / trade order id (`U::Trade` derives from `U::OrderIDBase`, an
/// `ExportableIDBase<OrderIDBase, 3, u64>`; Lua and FFI pass it as `TradeID`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct TradeId(pub u64);

/// Engine class ids used by the economy (`U::Component` vfunc573 `IsClass(id)`; table of
/// `{name, id}` at RVA 0x22c6790).
pub mod class {
    pub const BUILDSTORAGE: u8 = 0x08;
    pub const BUILDMODULE: u8 = 0x09;
    pub const CARGOBAY: u8 = 0x0c;
    pub const HABITATION: u8 = 0x31;
    pub const PROCESSINGMODULE: u8 = 0x4e;
    pub const PRODUCTION: u8 = 0x4f;
    pub const SECTOR: u8 = 0x58;
    pub const STATION: u8 = 0x62;
    pub const STORAGE: u8 = 0x63;
    pub const WELFAREMODULE: u8 = 0x6c;
    pub const ZONE: u8 = 0x6d;
    pub const CONTAINER: u8 = 0x6f;
    pub const MODULE: u8 = 0x73;
    pub const SHIP: u8 = 0x75;
}

/// Global position of an economy effect in the original sequential execution: the barrier
/// applies messages sorted by this stamp, so the result does not depend on which worker
/// produced a message or when it arrived.
///
/// `phase` orders engine phases inside a frame (walk, MD, AI, ...), `rank` orders actors
/// inside a phase in the original order (tree order for the walk, wake order for the AI
/// driver), `seq` orders the effects of one actor.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct Stamp {
    pub frame: u64,
    pub phase: u8,
    pub rank: u64,
    pub seq: u32,
}

x4mp_sim::impl_fields!(newtype WareIx);
x4mp_sim::impl_fields!(newtype TradeId);
