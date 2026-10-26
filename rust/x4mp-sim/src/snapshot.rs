//! Read-only copy of the published columns of every sector, taken at the start of each
//! parallel phase. Sector jobs read other sectors only through it, so a cross-sector read
//! sees the state after the previous barrier, never a concurrently mutated value.

use std::any::TypeId;

use crate::column::{AnyColumn, Column};
use crate::comp::Component;
use crate::id::{EntityId, IdMap, Loc, SectorIx, UniverseId};
use crate::world::World;

#[derive(Default)]
pub(crate) struct SnapSector {
    uids: Vec<UniverseId>,
    columns: Vec<Box<dyn AnyColumn>>,
}

/// See the module docs. Empty unless columns were published with
/// [`crate::universe::Universe::publish`].
#[derive(Default)]
pub struct Snapshot {
    types: Vec<TypeId>,
    sectors: Vec<SnapSector>,
}

impl Snapshot {
    /// Re-copies the published columns (reuses allocations).
    pub(crate) fn refresh(&mut self, worlds: &[World], published: &[TypeId]) {
        if published.is_empty() {
            self.types.clear();
            self.sectors.clear();
            return;
        }
        if self.types != published {
            self.types = published.to_vec();
            self.sectors.clear();
        }
        self.sectors.resize_with(worlds.len(), SnapSector::default);
        for (snap, w) in self.sectors.iter_mut().zip(worlds) {
            snap.uids.clear();
            snap.uids.extend_from_slice(w.uids());
            if snap.columns.len() != self.types.len() {
                snap.columns = self
                    .types
                    .iter()
                    .map(|t| w.columns()[w.column_index(*t).expect("published column registered")].new_empty())
                    .collect();
            }
            for (k, t) in self.types.iter().enumerate() {
                let src = &w.columns()[w.column_index(*t).expect("published column registered")];
                src.copy_into(snap.columns[k].as_mut());
            }
        }
    }

    fn col_index<T: 'static>(&self) -> Option<usize> {
        let id = TypeId::of::<T>();
        self.types.iter().position(|t| *t == id)
    }

    /// Whether `T` is published.
    pub fn has<T: Component>(&self) -> bool {
        self.col_index::<T>().is_some()
    }

    /// The published column of one sector.
    pub fn column<T: Component>(&self, s: SectorIx) -> Option<&[T]> {
        let k = self.col_index::<T>()?;
        let sec = self.sectors.get(s.index())?;
        Some(&sec.columns[k].as_any().downcast_ref::<Column<T>>().expect("column type").0)
    }

    pub fn uids(&self, s: SectorIx) -> Option<&[UniverseId]> {
        self.sectors.get(s.index()).map(|x| x.uids.as_slice())
    }

    /// Value at a location.
    pub fn at<T: Component>(&self, loc: Loc) -> Option<&T> {
        self.column::<T>(loc.sector)?.get(loc.row as usize)
    }

    /// Value of an entity anywhere (location from the IdMap, which does not change during a
    /// parallel phase).
    pub fn get<T: Component>(&self, ids: &IdMap, e: EntityId) -> Option<&T> {
        self.at::<T>(ids.loc(e)?)
    }
}
