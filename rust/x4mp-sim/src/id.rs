//! Identifiers: the engine's `UniverseID`, our entity handles, sector indices, tree-order keys,
//! and the map between them.
//!
//! UniverseIDs are instance-specific (they depend on the global creation/destruction order,
//! `x4-re/coord/reports/entity.md` section 1), so they identify an entity within one engine
//! instance or one capture, never across instances. [`EntityId`] is our own handle: dense,
//! generation-checked, and stable when an entity migrates between sectors.

use std::collections::{HashMap, VecDeque};
use std::fmt;
use std::hash::{BuildHasherDefault, Hasher};

use crate::hash::{Fnv64, StableHash};

/// Raw engine component id (`typedef uint64_t UniverseID`). 0 means "none".
///
/// Layout (AutoIDMap, entity.md): bits 0..24 hold slot + 1, bits 25..40 the slot's 16-bit
/// generation.
#[repr(transparent)]
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct UniverseId(pub u64);

impl UniverseId {
    pub const NONE: UniverseId = UniverseId(0);
    const SLOT_MASK: u64 = 0x1ff_ffff;

    pub fn is_none(self) -> bool {
        self.0 == 0
    }

    /// AutoIDMap slot index, `None` for the null id.
    pub fn slot(self) -> Option<u32> {
        ((self.0 & Self::SLOT_MASK) as u32).checked_sub(1)
    }

    /// 16-bit slot generation.
    pub fn generation(self) -> u16 {
        ((self.0 >> 25) & 0xffff) as u16
    }
}

impl fmt::Debug for UniverseId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:#x}", self.0)
    }
}

impl fmt::Display for UniverseId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:#x}", self.0)
    }
}

impl StableHash for UniverseId {
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u64(self.0);
    }
}

/// Our entity handle: slot index plus generation. Stable across sector migration.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct EntityId {
    index: u32,
    gen: u32,
}

impl EntityId {
    pub fn index(self) -> u32 {
        self.index
    }
    pub fn generation(self) -> u32 {
        self.gen
    }
    pub fn to_bits(self) -> u64 {
        (u64::from(self.gen) << 32) | u64::from(self.index)
    }
    pub fn from_bits(bits: u64) -> EntityId {
        EntityId { index: bits as u32, gen: (bits >> 32) as u32 }
    }
}

impl fmt::Debug for EntityId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "e{}v{}", self.index, self.gen)
    }
}

impl StableHash for EntityId {
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u64(self.to_bits());
    }
}

/// Dense index of a sector world, in engine tree order (the order in which the universe walk
/// galaxy -> clusters -> sectors visits them). The barrier merges outboxes in this order.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct SectorIx(pub u32);

impl SectorIx {
    pub fn index(self) -> usize {
        self.0 as usize
    }
}

impl fmt::Debug for SectorIx {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "s{}", self.0)
    }
}

/// Position of an entity in its sector's tree order. Rows of a world are kept sorted by key;
/// equal keys keep insertion order. The mirror assigns capture order (0, 1, 2, ...); entities
/// spawned or migrated without an explicit key are appended (`max + 1`).
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Default, Debug)]
pub struct OrderKey(pub u64);

impl StableHash for OrderKey {
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u64(self.0);
    }
}

/// Where an entity lives: its sector and dense row.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Loc {
    pub sector: SectorIx,
    pub row: u32,
}

/// Multiplicative hasher for integer keys. Only used for lookups; no observable behaviour
/// depends on `HashMap` iteration order anywhere in this crate.
#[derive(Default, Clone, Copy)]
pub struct IdHasher(u64);

impl Hasher for IdHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for b in bytes {
            self.0 = (self.0.rotate_left(5) ^ u64::from(*b)).wrapping_mul(0x517c_c1b7_2722_0a95);
        }
    }
    fn write_u64(&mut self, v: u64) {
        self.0 = (self.0.rotate_left(5) ^ v).wrapping_mul(0x517c_c1b7_2722_0a95);
    }
    fn write_u32(&mut self, v: u32) {
        self.write_u64(u64::from(v));
    }
}

pub type IdBuild = BuildHasherDefault<IdHasher>;
pub type IdHashMap<K, V> = HashMap<K, V, IdBuild>;

#[derive(Clone, Copy, Debug)]
struct Slot {
    gen: u32,
    live: bool,
    loc: Loc,
    uid: UniverseId,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IdError {
    /// The UniverseId is already mapped to a live entity.
    DuplicateUid(UniverseId),
    /// The handle is stale or was never issued.
    Stale(EntityId),
}

impl fmt::Display for IdError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IdError::DuplicateUid(u) => write!(f, "duplicate UniverseId {u}"),
            IdError::Stale(e) => write!(f, "stale entity {e:?}"),
        }
    }
}

impl std::error::Error for IdError {}

/// UniverseId <-> EntityId <-> location.
///
/// Mutated only between frames or at the barrier (single-threaded); sector jobs read it.
/// Freed slots are reused FIFO with a bumped generation, so stale handles fail lookups.
#[derive(Default, Clone)]
pub struct IdMap {
    slots: Vec<Slot>,
    free: VecDeque<u32>,
    by_uid: IdHashMap<UniverseId, EntityId>,
    live: usize,
}

impl IdMap {
    pub fn new() -> IdMap {
        IdMap::default()
    }

    /// Issues a handle for `uid` at `loc`. `UniverseId::NONE` may be used for entities that
    /// have no engine counterpart (synthetic tests); those are not indexed by uid.
    pub fn alloc(&mut self, uid: UniverseId, loc: Loc) -> Result<EntityId, IdError> {
        if !uid.is_none() && self.by_uid.contains_key(&uid) {
            return Err(IdError::DuplicateUid(uid));
        }
        let id = match self.free.pop_front() {
            Some(index) => {
                let slot = &mut self.slots[index as usize];
                slot.live = true;
                slot.loc = loc;
                slot.uid = uid;
                EntityId { index, gen: slot.gen }
            }
            None => {
                let index = u32::try_from(self.slots.len()).expect("more than 2^32 entities");
                self.slots.push(Slot { gen: 0, live: true, loc, uid });
                EntityId { index, gen: 0 }
            }
        };
        if !uid.is_none() {
            self.by_uid.insert(uid, id);
        }
        self.live += 1;
        Ok(id)
    }

    /// Releases a handle; returns its UniverseId.
    pub fn free(&mut self, e: EntityId) -> Result<UniverseId, IdError> {
        let slot = self.slot_mut(e)?;
        slot.live = false;
        slot.gen = slot.gen.wrapping_add(1);
        let uid = slot.uid;
        slot.uid = UniverseId::NONE;
        if !uid.is_none() {
            self.by_uid.remove(&uid);
        }
        self.free.push_back(e.index);
        self.live -= 1;
        Ok(uid)
    }

    fn slot(&self, e: EntityId) -> Option<&Slot> {
        self.slots.get(e.index as usize).filter(|s| s.live && s.gen == e.gen)
    }

    fn slot_mut(&mut self, e: EntityId) -> Result<&mut Slot, IdError> {
        match self.slots.get_mut(e.index as usize) {
            Some(s) if s.live && s.gen == e.gen => Ok(s),
            _ => Err(IdError::Stale(e)),
        }
    }

    pub fn is_live(&self, e: EntityId) -> bool {
        self.slot(e).is_some()
    }

    pub fn loc(&self, e: EntityId) -> Option<Loc> {
        self.slot(e).map(|s| s.loc)
    }

    pub fn uid(&self, e: EntityId) -> Option<UniverseId> {
        self.slot(e).map(|s| s.uid)
    }

    pub fn by_uid(&self, uid: UniverseId) -> Option<EntityId> {
        self.by_uid.get(&uid).copied()
    }

    pub(crate) fn set_loc(&mut self, e: EntityId, loc: Loc) {
        if let Ok(slot) = self.slot_mut(e) {
            slot.loc = loc;
        }
    }

    /// Number of live entities.
    pub fn len(&self) -> usize {
        self.live
    }

    pub fn is_empty(&self) -> bool {
        self.live == 0
    }

    /// Exclusive upper bound of issued slot indices (for dense side tables).
    pub fn capacity_index(&self) -> usize {
        self.slots.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn loc(s: u32, row: u32) -> Loc {
        Loc { sector: SectorIx(s), row }
    }

    #[test]
    fn universe_id_layout() {
        // slot 0x1234 (stored as slot + 1), generation 7
        let id = UniverseId((7u64 << 25) | 0x1235);
        assert_eq!(id.slot(), Some(0x1234));
        assert_eq!(id.generation(), 7);
        assert_eq!(UniverseId::NONE.slot(), None);
        assert_eq!(format!("{id}"), "0xe001235");
    }

    #[test]
    fn alloc_free_reuse_bumps_generation() {
        let mut m = IdMap::new();
        let a = m.alloc(UniverseId(10), loc(0, 0)).unwrap();
        let b = m.alloc(UniverseId(11), loc(0, 1)).unwrap();
        assert_eq!(m.alloc(UniverseId(10), loc(1, 0)), Err(IdError::DuplicateUid(UniverseId(10))));
        assert_eq!(m.by_uid(UniverseId(11)), Some(b));
        assert_eq!(m.free(a), Ok(UniverseId(10)));
        assert!(!m.is_live(a));
        assert_eq!(m.free(a), Err(IdError::Stale(a)));
        let c = m.alloc(UniverseId(10), loc(2, 5)).unwrap();
        assert_eq!(c.index(), a.index());
        assert_ne!(c.generation(), a.generation());
        assert_eq!(m.loc(c), Some(loc(2, 5)));
        assert_eq!(m.loc(a), None);
        assert_eq!(m.len(), 2);
        assert_eq!(EntityId::from_bits(c.to_bits()), c);
    }
}
