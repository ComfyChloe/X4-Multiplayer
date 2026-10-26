//! One sector's world: dense SoA columns with rows in tree order.
//!
//! Invariants (checked by [`World::check`] in tests):
//! - every column, `entities`, `uids` and `keys` have the same length;
//! - rows are sorted by [`OrderKey`]; equal keys keep insertion order;
//! - the [`IdMap`] location of `entities[r]` is `(this sector, r)`.
//!
//! Structure changes are staged (spawn/insert, remove) and applied by `commit`, which does a
//! stable merge of the kept rows and the staged rows by key. Values may be changed in place at
//! any time through the column slices.

use std::any::{Any, TypeId};

use crate::column::{new_column, AnyColumn, Bundle, Src};
use crate::comp::Component;
use crate::hash::Fnv64;
use crate::id::{EntityId, IdMap, Loc, OrderKey, SectorIx, UniverseId};
use crate::shadow::Comparator;

/// What a world's root is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
#[repr(u8)]
pub enum WorldKind {
    /// A sector subtree (class 0x58).
    #[default]
    Sector = 0,
    /// Cluster-level components outside any sector (cluster, superhighways, ...).
    Cluster = 1,
    /// Galaxy-level components outside any cluster.
    Galaxy = 2,
}

impl WorldKind {
    pub fn from_u8(v: u8) -> WorldKind {
        match v {
            1 => WorldKind::Cluster,
            2 => WorldKind::Galaxy,
            _ => WorldKind::Sector,
        }
    }
}

/// Registered component type (same order in every world).
#[derive(Clone)]
pub(crate) struct ColInfo {
    pub type_id: TypeId,
    pub name: &'static str,
    pub make: fn() -> Box<dyn AnyColumn>,
    pub published: bool,
}

impl ColInfo {
    pub fn of<T: Component>() -> ColInfo {
        ColInfo { type_id: TypeId::of::<T>(), name: T::NAME, make: new_column::<T>, published: false }
    }
}

#[derive(Default)]
struct Staged {
    entities: Vec<EntityId>,
    uids: Vec<UniverseId>,
    keys: Vec<OrderKey>,
    columns: Vec<Box<dyn AnyColumn>>,
    /// Staged rows cancelled before commit (spawn then despawn in one batch).
    cancelled: Vec<bool>,
}

/// Error from a structural operation on a world.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StructError {
    UnknownComponent(&'static str),
    Id(crate::id::IdError),
    NoSuchSector(SectorIx),
}

impl std::fmt::Display for StructError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StructError::UnknownComponent(n) => write!(f, "component {n} is not registered"),
            StructError::Id(e) => write!(f, "{e}"),
            StructError::NoSuchSector(s) => write!(f, "no sector {s:?}"),
        }
    }
}

impl std::error::Error for StructError {}

/// Marker row for staged (not yet committed) entities in the IdMap.
pub(crate) const STAGED_ROW: u32 = u32::MAX;

/// One sector's dense storage. See the module docs.
pub struct World {
    pub(crate) ix: SectorIx,
    pub(crate) uid: UniverseId,
    pub(crate) kind: WorldKind,
    pub(crate) pinned: bool,
    entities: Vec<EntityId>,
    uids: Vec<UniverseId>,
    keys: Vec<OrderKey>,
    columns: Vec<Box<dyn AnyColumn>>,
    types: Vec<TypeId>,
    dead: Vec<bool>,
    removed: usize,
    staged: Staged,
    resources: Vec<(TypeId, Box<dyn Any + Send + Sync>)>,
    pub(crate) outbox: crate::bus::Outbox,
    /// Undelivered messages per addressed system, in total order.
    inboxes: Vec<(&'static str, Vec<crate::bus::Envelope>)>,
    /// Last measured job time (scheduling order only; never affects results).
    pub(crate) cost_ns: u64,
}

impl World {
    pub(crate) fn new(ix: SectorIx, uid: UniverseId, kind: WorldKind, registry: &[ColInfo]) -> World {
        let mut w = World {
            ix,
            uid,
            kind,
            pinned: false,
            entities: Vec::new(),
            uids: Vec::new(),
            keys: Vec::new(),
            columns: Vec::new(),
            types: Vec::new(),
            dead: Vec::new(),
            removed: 0,
            staged: Staged::default(),
            resources: Vec::new(),
            outbox: crate::bus::Outbox::new(ix, ""),
            inboxes: Vec::new(),
            cost_ns: 0,
        };
        for info in registry {
            w.add_column(info);
        }
        w
    }

    pub(crate) fn add_column(&mut self, info: &ColInfo) {
        let mut col = (info.make)();
        for _ in 0..self.entities.len() {
            col.push_default();
        }
        let mut staged = (info.make)();
        for _ in 0..self.staged.entities.len() {
            staged.push_default();
        }
        self.columns.push(col);
        self.staged.columns.push(staged);
        self.types.push(info.type_id);
    }

    /// Sector index (tree order).
    pub fn sector(&self) -> SectorIx {
        self.ix
    }

    /// UniverseId of the root component (the sector, cluster or galaxy).
    pub fn root_uid(&self) -> UniverseId {
        self.uid
    }

    pub fn kind(&self) -> WorldKind {
        self.kind
    }

    /// Pinned worlds run on the calling (engine) thread, never on a worker.
    pub fn pinned(&self) -> bool {
        self.pinned
    }

    pub fn len(&self) -> usize {
        self.entities.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entities.is_empty()
    }

    pub fn entities(&self) -> &[EntityId] {
        &self.entities
    }

    pub fn uids(&self) -> &[UniverseId] {
        &self.uids
    }

    pub fn keys(&self) -> &[OrderKey] {
        &self.keys
    }

    fn index_of<T: 'static>(&self) -> Option<usize> {
        let id = TypeId::of::<T>();
        self.types.iter().position(|t| *t == id)
    }

    pub fn has<T: Component>(&self) -> bool {
        self.index_of::<T>().is_some()
    }

    pub fn try_col<T: Component>(&self) -> Option<&[T]> {
        let i = self.index_of::<T>()?;
        Some(&self.columns[i].as_any().downcast_ref::<crate::column::Column<T>>().expect("column type").0)
    }

    pub fn try_col_mut<T: Component>(&mut self) -> Option<&mut [T]> {
        let i = self.index_of::<T>()?;
        Some(&mut self.columns[i].as_any_mut().downcast_mut::<crate::column::Column<T>>().expect("column type").0)
    }

    /// Dense column of `T` in row order. Panics if `T` is not registered.
    pub fn col<T: Component>(&self) -> &[T] {
        self.try_col::<T>().unwrap_or_else(|| panic!("component {} is not registered", T::NAME))
    }

    pub fn col_mut<T: Component>(&mut self) -> &mut [T] {
        self.try_col_mut::<T>().unwrap_or_else(|| panic!("component {} is not registered", T::NAME))
    }

    fn two_mut(&mut self, a: usize, b: usize) -> (&mut Box<dyn AnyColumn>, &mut Box<dyn AnyColumn>) {
        assert_ne!(a, b, "the same column borrowed twice");
        if a < b {
            let (l, r) = self.columns.split_at_mut(b);
            (&mut l[a], &mut r[0])
        } else {
            let (l, r) = self.columns.split_at_mut(a);
            (&mut r[0], &mut l[b])
        }
    }

    /// Two distinct columns at once (e.g. integrate `Transform` from `Velocity`).
    pub fn cols_mut<A: Component, B: Component>(&mut self) -> (&mut [A], &mut [B]) {
        let a = self.index_of::<A>().unwrap_or_else(|| panic!("component {} is not registered", A::NAME));
        let b = self.index_of::<B>().unwrap_or_else(|| panic!("component {} is not registered", B::NAME));
        let (ca, cb) = self.two_mut(a, b);
        (
            &mut ca.as_any_mut().downcast_mut::<crate::column::Column<A>>().expect("column type").0,
            &mut cb.as_any_mut().downcast_mut::<crate::column::Column<B>>().expect("column type").0,
        )
    }

    /// Three distinct columns at once.
    pub fn cols_mut3<A: Component, B: Component, C: Component>(&mut self) -> (&mut [A], &mut [B], &mut [C]) {
        let idx = [
            self.index_of::<A>().unwrap_or_else(|| panic!("component {} is not registered", A::NAME)),
            self.index_of::<B>().unwrap_or_else(|| panic!("component {} is not registered", B::NAME)),
            self.index_of::<C>().unwrap_or_else(|| panic!("component {} is not registered", C::NAME)),
        ];
        assert!(idx[0] != idx[1] && idx[1] != idx[2] && idx[0] != idx[2], "the same column borrowed twice");
        let ptr = self.columns.as_mut_ptr();
        // SAFETY: three distinct indices into one Vec give three disjoint &mut.
        unsafe {
            (
                &mut (*ptr.add(idx[0])).as_any_mut().downcast_mut::<crate::column::Column<A>>().expect("column type").0,
                &mut (*ptr.add(idx[1])).as_any_mut().downcast_mut::<crate::column::Column<B>>().expect("column type").0,
                &mut (*ptr.add(idx[2])).as_any_mut().downcast_mut::<crate::column::Column<C>>().expect("column type").0,
            )
        }
    }

    /// Row of a uid in this world (linear scan; prefer the IdMap from a context).
    pub fn find_uid(&self, uid: UniverseId) -> Option<usize> {
        self.uids.iter().position(|u| *u == uid)
    }

    /// Per-sector singleton of type `R` (created with `Default` on first use).
    pub fn resource_mut<R: Any + Send + Sync + Default>(&mut self) -> &mut R {
        let id = TypeId::of::<R>();
        let pos = match self.resources.iter().position(|(t, _)| *t == id) {
            Some(p) => p,
            None => {
                self.resources.push((id, Box::new(R::default())));
                self.resources.len() - 1
            }
        };
        self.resources[pos].1.downcast_mut::<R>().expect("resource type")
    }

    pub fn resource<R: Any + Send + Sync>(&self) -> Option<&R> {
        let id = TypeId::of::<R>();
        self.resources.iter().find(|(t, _)| *t == id).and_then(|(_, r)| r.downcast_ref::<R>())
    }

    /// Messages waiting for `system`'s next run in this sector.
    pub fn pending_for(&self, system: &str) -> usize {
        self.inboxes.iter().find(|(n, _)| *n == system).map_or(0, |(_, v)| v.len())
    }

    /// All undelivered messages in this sector (every system).
    pub fn pending_messages(&self) -> usize {
        self.inboxes.iter().map(|(_, v)| v.len()).sum()
    }

    pub(crate) fn inbox_mut(&mut self, system: &'static str) -> &mut Vec<crate::bus::Envelope> {
        let pos = match self.inboxes.iter().position(|(n, _)| *n == system) {
            Some(p) => p,
            None => {
                self.inboxes.push((system, Vec::new()));
                self.inboxes.len() - 1
            }
        };
        &mut self.inboxes[pos].1
    }

    pub(crate) fn take_inbox(&mut self, system: &'static str) -> Vec<crate::bus::Envelope> {
        std::mem::take(self.inbox_mut(system))
    }

    /// Gives a consumed inbox back (cleared; keeps the allocation).
    pub(crate) fn return_inbox(&mut self, system: &'static str, mut v: Vec<crate::bus::Envelope>) {
        v.clear();
        let slot = self.inbox_mut(system);
        if slot.is_empty() {
            *slot = v;
        }
    }

    // --- structure (crate-internal; driven by Universe) ---------------------------------

    /// Largest key of the rows that will exist after commit (removed and cancelled rows
    /// excluded).
    pub(crate) fn last_key(&self) -> Option<OrderKey> {
        let committed = (0..self.keys.len()).rev().find(|&r| !self.is_dead(r)).map(|r| self.keys[r]);
        let staged =
            (0..self.staged.keys.len()).filter(|&i| !self.staged.cancelled[i]).map(|i| self.staged.keys[i]).max();
        committed.max(staged)
    }

    /// Stages a new row. Components missing from the bundle get `Default`.
    pub(crate) fn stage(
        &mut self,
        e: EntityId,
        uid: UniverseId,
        key: OrderKey,
        mut bundle: Bundle,
    ) -> Result<(), StructError> {
        if let Some((_, name, _)) = bundle.items.iter().find(|(t, _, _)| !self.types.contains(t)) {
            return Err(StructError::UnknownComponent(name));
        }
        for (i, t) in self.types.iter().enumerate() {
            match bundle.take(*t) {
                Some(v) => {
                    if self.staged.columns[i].push_boxed(v).is_err() {
                        unreachable!("bundle entry typed by its TypeId");
                    }
                }
                None => self.staged.columns[i].push_default(),
            }
        }
        self.staged.entities.push(e);
        self.staged.uids.push(uid);
        self.staged.keys.push(key);
        self.staged.cancelled.push(false);
        Ok(())
    }

    /// Cancels a staged row of `e` (spawned and removed before commit). Returns its values.
    pub(crate) fn unstage(&mut self, e: EntityId) -> Option<Bundle> {
        let pos =
            (0..self.staged.entities.len()).find(|&i| self.staged.entities[i] == e && !self.staged.cancelled[i])?;
        self.staged.cancelled[pos] = true;
        let mut b = Bundle::new();
        for (i, col) in self.staged.columns.iter().enumerate() {
            b.items.push((self.types[i], col.name(), col.clone_row(pos)));
        }
        Some(b)
    }

    /// Marks a committed row for removal and returns its component values.
    pub(crate) fn remove_row(&mut self, row: usize) -> Bundle {
        if self.dead.len() < self.entities.len() {
            self.dead.resize(self.entities.len(), false);
        }
        debug_assert!(!self.dead[row], "row removed twice");
        self.dead[row] = true;
        self.removed += 1;
        let mut b = Bundle::new();
        for (i, col) in self.columns.iter().enumerate() {
            b.items.push((self.types[i], col.name(), col.clone_row(row)));
        }
        b
    }

    pub(crate) fn is_dead(&self, row: usize) -> bool {
        self.dead.get(row).copied().unwrap_or(false)
    }

    pub(crate) fn has_staged(&self) -> bool {
        self.removed > 0 || !self.staged.entities.is_empty()
    }

    /// Applies staged structure: removes dead rows and merges staged rows by key (stable).
    /// Updates the IdMap location of every row. Returns true if anything changed.
    pub(crate) fn commit(&mut self, ids: &mut IdMap) -> bool {
        if !self.has_staged() {
            return false;
        }
        let n_old = self.entities.len();
        let mut order: Vec<u32> =
            (0..self.staged.entities.len() as u32).filter(|&i| !self.staged.cancelled[i as usize]).collect();
        order.sort_by_key(|&i| (self.staged.keys[i as usize], i));
        let mut plan = Vec::with_capacity(n_old - self.removed + order.len());
        let (mut i, mut j) = (0usize, 0usize);
        loop {
            while i < n_old && self.is_dead(i) {
                i += 1;
            }
            let take_old = match (i < n_old, j < order.len()) {
                (false, false) => break,
                (true, false) => true,
                (false, true) => false,
                (true, true) => self.keys[i] <= self.staged.keys[order[j] as usize],
            };
            if take_old {
                plan.push(Src::Old(i as u32));
                i += 1;
            } else {
                plan.push(Src::New(order[j]));
                j += 1;
            }
        }
        self.entities = pick(&plan, &self.entities, &self.staged.entities);
        self.uids = pick(&plan, &self.uids, &self.staged.uids);
        self.keys = pick(&plan, &self.keys, &self.staged.keys);
        for (col, staged) in self.columns.iter_mut().zip(self.staged.columns.iter_mut()) {
            col.rebuild(&plan, staged.as_mut());
        }
        self.staged.entities.clear();
        self.staged.uids.clear();
        self.staged.keys.clear();
        self.staged.cancelled.clear();
        self.dead.clear();
        self.removed = 0;
        for (row, e) in self.entities.iter().enumerate() {
            ids.set_loc(*e, Loc { sector: self.ix, row: row as u32 });
        }
        true
    }

    /// Re-sorts committed rows by key (stable) after keys were rewritten in place.
    pub(crate) fn reorder_by_keys(&mut self, ids: &mut IdMap) {
        if self.keys.windows(2).all(|w| w[0] <= w[1]) {
            return;
        }
        let mut perm: Vec<u32> = (0..self.entities.len() as u32).collect();
        perm.sort_by_key(|&i| (self.keys[i as usize], i));
        permute_copy(&mut self.entities, &perm);
        permute_copy(&mut self.uids, &perm);
        permute_copy(&mut self.keys, &perm);
        for col in &mut self.columns {
            col.permute(&perm);
        }
        for (row, e) in self.entities.iter().enumerate() {
            ids.set_loc(*e, Loc { sector: self.ix, row: row as u32 });
        }
    }

    pub(crate) fn keys_mut(&mut self) -> &mut [OrderKey] {
        &mut self.keys
    }

    pub(crate) fn columns(&self) -> &[Box<dyn AnyColumn>] {
        &self.columns
    }

    pub(crate) fn column_index(&self, t: TypeId) -> Option<usize> {
        self.types.iter().position(|x| *x == t)
    }

    /// Hash of the whole world in row order: root, entities, uids, keys, every column in
    /// registration order.
    pub fn hash_into(&self, h: &mut Fnv64) {
        h.write_u32(self.ix.0);
        h.write_u64(self.uid.0);
        h.write_u8(self.kind as u8);
        h.write_u64(self.entities.len() as u64);
        for i in 0..self.entities.len() {
            h.write_u64(self.entities[i].to_bits());
            h.write_u64(self.uids[i].0);
            h.write_u64(self.keys[i].0);
        }
        for col in &self.columns {
            h.write(col.name().as_bytes());
            col.hash_into(h);
        }
    }

    /// Hash of one row's components (all columns).
    pub fn hash_row(&self, row: usize) -> u64 {
        let mut h = Fnv64::new();
        for col in &self.columns {
            col.hash_row(row, &mut h);
        }
        h.finish()
    }

    /// Debug text of one row.
    pub fn describe_row(&self, row: usize) -> String {
        let mut s = format!("{:?} uid={} key={}", self.entities[row], self.uids[row], self.keys[row].0);
        for col in &self.columns {
            s.push_str(&format!(" {}={}", col.name(), col.debug_row(row)));
        }
        s
    }

    /// Compares this (expected) world with `actual`: rows are matched by UniverseId; missing
    /// and extra entities and order differences are structural differences; every column
    /// present in both worlds (and selected by `columns`, all when empty) is compared row-wise.
    pub fn compare(&self, actual: &World, columns: &[&str], frame: u64, c: &mut Comparator) {
        use crate::shadow::At;
        c.set_at(At { frame, sector: Some(self.ix), entity: UniverseId::NONE });
        let mut index = crate::id::IdHashMap::default();
        for (r, u) in actual.uids.iter().enumerate() {
            index.insert(*u, r);
        }
        let mut matched = 0usize;
        let mut last_actual_row: Option<usize> = None;
        let mut order_ok = true;
        for (row, uid) in self.uids.iter().enumerate() {
            c.set_at(At { frame, sector: Some(self.ix), entity: *uid });
            let Some(&arow) = index.get(uid) else {
                c.structural("entity", format!("present (row {row})"), "missing".into());
                continue;
            };
            matched += 1;
            if last_actual_row.is_some_and(|l| arow < l) {
                order_ok = false;
            }
            last_actual_row = Some(arow);
            for (ci, col) in self.columns.iter().enumerate() {
                if !columns.is_empty() && !columns.contains(&col.name()) {
                    continue;
                }
                if let Some(aci) = actual.column_index(self.types[ci]) {
                    col.compare_row(row, actual.columns[aci].as_ref(), arow, c);
                }
            }
        }
        c.set_at(At { frame, sector: Some(self.ix), entity: UniverseId::NONE });
        if matched != actual.uids.len() {
            for uid in actual.uids.iter().filter(|u| !self.uids.contains(u)) {
                c.set_at(At { frame, sector: Some(self.ix), entity: *uid });
                c.structural("entity", "absent".into(), "extra".into());
            }
        }
        c.set_at(At { frame, sector: Some(self.ix), entity: UniverseId::NONE });
        if !order_ok {
            c.structural("order", "tree order".into(), "different row order".into());
        }
    }

    /// Verifies the module invariants against the IdMap (tests, debugging).
    pub fn check(&self, ids: &IdMap) -> Result<(), String> {
        let n = self.entities.len();
        if self.uids.len() != n || self.keys.len() != n {
            return Err(format!("{:?}: header lengths differ", self.ix));
        }
        for col in &self.columns {
            if col.len() != n {
                return Err(format!("{:?}: column {} has {} rows, expected {n}", self.ix, col.name(), col.len()));
            }
        }
        if !self.keys.windows(2).all(|w| w[0] <= w[1]) {
            return Err(format!("{:?}: rows not sorted by key", self.ix));
        }
        for (row, e) in self.entities.iter().enumerate() {
            let loc = ids.loc(*e).ok_or_else(|| format!("{:?}: {e:?} not live", self.ix))?;
            if loc != (Loc { sector: self.ix, row: row as u32 }) {
                return Err(format!("{:?}: {e:?} at row {row} but IdMap says {loc:?}", self.ix));
            }
            if ids.uid(*e) != Some(self.uids[row]) {
                return Err(format!("{:?}: uid mismatch for {e:?}", self.ix));
            }
        }
        Ok(())
    }
}

fn pick<T: Copy>(plan: &[Src], old: &[T], new: &[T]) -> Vec<T> {
    plan.iter()
        .map(|s| match *s {
            Src::Old(k) => old[k as usize],
            Src::New(k) => new[k as usize],
        })
        .collect()
}

fn permute_copy<T: Copy>(v: &mut Vec<T>, perm: &[u32]) {
    let old = std::mem::take(v);
    *v = perm.iter().map(|&i| old[i as usize]).collect();
}
