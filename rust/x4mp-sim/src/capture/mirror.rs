//! The mirror: load a captured frame into a [`Universe`] (and the inverse, for Rust-side
//! recordings and round-trip tests).
//!
//! After [`mirror`] the universe holds exactly the frame's entities: one world per record in
//! record order, rows in record order (tree order, `OrderKey` = row index), components filled
//! from the captured columns. Entities persist by UniverseId across frames (same `EntityId`,
//! system-owned components kept); entities absent from the frame are despawned, new ones
//! spawned, moved ones migrated.

use std::collections::HashSet;

use super::{col, CapError, ColumnData, FrameRecord, SectorRecord, FRAME_SUBSET, FRAME_TRUNCATED};
use crate::comp::{
    Attention, ClassFlags, ClassId, Controller, CtrlKind, EngineFlags, FactionIx, Hull, LocalOffset, Mat4f, NavCache,
    OrderState, Owner, PoseRef, Transform, Tree, Vel, Velocity, VtableRva,
};
use crate::id::{EntityId, IdHashMap, OrderKey, SectorIx, UniverseId};
use crate::universe::Universe;
use crate::world::{World, WorldKind};
use crate::Bundle;

/// What [`mirror`] changed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MirrorReport {
    /// Only these worlds were refreshed; other worlds retain their previous observations.
    pub updated_sectors: Vec<SectorIx>,
    /// This was an explicitly requested subset update, not a simultaneous universe snapshot.
    pub subset: bool,
    pub sectors: usize,
    pub rows: usize,
    pub added_sectors: usize,
    pub spawned: usize,
    pub despawned: usize,
    pub migrated: usize,
    /// Captured columns with no registered component to receive them.
    pub unmapped_columns: Vec<u16>,
}

fn uids_of(s: &SectorRecord, frame: u64) -> Result<Vec<u64>, CapError> {
    let c = s
        .column(col::UID)
        .ok_or_else(|| CapError::Schema(format!("frame {frame}: sector record without uid column")))?;
    Ok(c.u64s())
}

/// Loads `frame` into `u`. `factions` is the capture's faction dictionary (index, id string).
pub fn mirror(frame: &FrameRecord, factions: &[(u32, &str)], u: &mut Universe) -> Result<MirrorReport, CapError> {
    if frame.flags & FRAME_SUBSET != 0 {
        return Err(CapError::Schema("subset frame requires explicit mirror_subset; not a full snapshot".into()));
    }
    mirror_impl(frame, factions, u)
}

/// Refresh only the roots in a subset frame, after a full [`mirror`] initialized `u`.
/// Missing entities in observed roots are removed; unobserved roots retain stale values.
/// A migration into an observed root keeps its EntityId even if its old root was not observed.
/// A migration out of an observed root into an unobserved one cannot be distinguished from a
/// despawn until that destination is captured. This is diagnostic state, not authoritative state
/// or a simultaneous snapshot. `updated_sectors` identifies which worlds are current.
pub fn mirror_subset(
    frame: &FrameRecord,
    factions: &[(u32, &str)],
    u: &mut Universe,
) -> Result<MirrorReport, CapError> {
    if frame.flags & FRAME_SUBSET == 0 {
        return Err(CapError::Schema("mirror_subset requires a subset frame".into()));
    }
    mirror_impl(frame, factions, u)
}

fn mirror_impl(frame: &FrameRecord, factions: &[(u32, &str)], u: &mut Universe) -> Result<MirrorReport, CapError> {
    if frame.flags & FRAME_TRUNCATED != 0 {
        return Err(CapError::Schema("truncated frame cannot replace mirror state".into()));
    }
    let subset = frame.flags & FRAME_SUBSET != 0;
    let mut rep = MirrorReport { sectors: frame.sectors.len(), subset, ..MirrorReport::default() };
    let mut frame_uids: Vec<Vec<u64>> = Vec::with_capacity(frame.sectors.len());
    let mut all: HashSet<u64> = HashSet::new();
    for s in &frame.sectors {
        let uids = uids_of(s, frame.frame)?;
        for v in &uids {
            if *v == 0 || !all.insert(*v) {
                return Err(CapError::Schema(format!("frame {}: uid {v:#x} null or duplicated", frame.frame)));
            }
        }
        rep.rows += uids.len();
        frame_uids.push(uids);
    }
    // Resolve all subset roots before any mutation. Their record indices are not SectorIx.
    let mut seen_roots = HashSet::new();
    for s in &frame.sectors {
        if !seen_roots.insert(s.root_uid) {
            return Err(CapError::Schema(format!("frame {}: duplicated root {:#x}", frame.frame, s.root_uid)));
        }
        if subset {
            let ix =
                u.worlds().iter().position(|w| w.root_uid().0 == s.root_uid && w.kind() as u8 == s.kind).ok_or_else(
                    || CapError::Schema(format!("subset root {:#x} is absent from the initialized mirror", s.root_uid)),
                )?;
            rep.updated_sectors.push(SectorIx(ix as u32));
        }
    }
    // Full sector list: identical prefix required; new records are appended.
    for (i, s) in frame.sectors.iter().enumerate() {
        if subset {
            continue;
        }
        match u.worlds().get(i) {
            Some(w) if w.root_uid().0 == s.root_uid => {}
            Some(w) => {
                return Err(CapError::Schema(format!(
                    "frame {}: sector {i} is {:#x} in the capture but {} in the universe (sector list changed)",
                    frame.frame,
                    s.root_uid,
                    w.root_uid()
                )))
            }
            None => {
                u.add_sector(UniverseId(s.root_uid), WorldKind::from_u8(s.kind));
                rep.added_sectors += 1;
            }
        }
        rep.updated_sectors.push(SectorIx(i as u32));
    }
    if !subset && u.sector_count() != frame.sectors.len() {
        return Err(CapError::Schema(format!(
            "frame {}: universe has {} sectors, capture {}",
            frame.frame,
            u.sector_count(),
            frame.sectors.len()
        )));
    }
    // Despawn what is gone.
    let mut gone: Vec<EntityId> = Vec::new();
    for (si, w) in u.worlds().iter().enumerate() {
        if subset && !rep.updated_sectors.contains(&SectorIx(si as u32)) {
            continue;
        }
        for (r, uid) in w.uids().iter().enumerate() {
            if !all.contains(&uid.0) {
                gone.push(w.entities()[r]);
            }
        }
    }
    for e in gone {
        u.despawn(e).map_err(|e| CapError::Schema(format!("mirror despawn: {e}")))?;
        rep.despawned += 1;
    }
    // Spawn new, migrate moved.
    for (si, uids) in frame_uids.iter().enumerate() {
        let s = rep.updated_sectors[si];
        for (row, uid) in uids.iter().enumerate() {
            let uid = UniverseId(*uid);
            match u.ids().by_uid(uid) {
                Some(e) => {
                    let loc = u.ids().loc(e).expect("live entity");
                    if loc.sector != s {
                        u.migrate_at(e, s, OrderKey(row as u64))
                            .map_err(|e| CapError::Schema(format!("mirror migrate: {e}")))?;
                        rep.migrated += 1;
                    }
                }
                None => {
                    u.spawn_at(s, uid, OrderKey(row as u64), Bundle::new())
                        .map_err(|e| CapError::Schema(format!("mirror spawn: {e}")))?;
                    rep.spawned += 1;
                }
            }
        }
    }
    u.commit();
    // Rows into record order.
    for (si, uids) in frame_uids.iter().enumerate() {
        let s = rep.updated_sectors[si];
        {
            let w = u.world_mut(s);
            let mut by_uid: IdHashMap<UniverseId, usize> = IdHashMap::default();
            for (r, uid) in w.uids().iter().enumerate() {
                by_uid.insert(*uid, r);
            }
            let keys = w.keys_mut();
            for (row, uid) in uids.iter().enumerate() {
                let r = by_uid[&UniverseId(*uid)];
                keys[r] = OrderKey(row as u64);
            }
        }
        u.reorder_by_keys(s);
        let w = u.world(s);
        if w.len() != uids.len() || w.uids().iter().zip(uids).any(|(a, b)| a.0 != *b) {
            return Err(CapError::Schema(format!("frame {}: mirror order mismatch in sector {si}", frame.frame)));
        }
    }
    // Values.
    for (si, rec) in frame.sectors.iter().enumerate() {
        let w = u.world_mut(rep.updated_sectors[si]);
        fill(w, rec, &mut rep.unmapped_columns);
    }
    rep.unmapped_columns.sort_unstable();
    rep.unmapped_columns.dedup();
    for (index, name) in factions {
        if let Ok(ix) = u16::try_from(*index) {
            u.set_faction(ix, name);
        }
    }
    Ok(rep)
}

fn mat(v: &[f32]) -> Mat4f {
    Mat4f::from_flat(v.try_into().expect("16 floats"))
}

fn fill(w: &mut World, rec: &SectorRecord, unmapped: &mut Vec<u16>) {
    let rows = rec.rows as usize;
    for c in &rec.columns {
        let mapped = match c.id {
            col::UID | col::DEPTH | col::COVER | col::CTRL_VTABLE => true,
            col::CLASS => set(w, c.u8s(), ClassId),
            col::ATTENTION => set(w, c.i32s(), Attention),
            col::CLASS_FLAGS => set(w, c.u16s(), ClassFlags),
            col::LOCAL_OFFSET => {
                let f = c.f32s();
                set_rows(w, rows, |r| LocalOffset([f[r * 4], f[r * 4 + 1], f[r * 4 + 2], f[r * 4 + 3]]))
            }
            col::FLAGS => set(w, c.u8s(), EngineFlags),
            col::VTABLE => set(w, c.u32s(), VtableRva),
            col::POSE => {
                let f = c.f32s();
                set_rows(w, rows, |r| Transform(mat(&f[r * 16..r * 16 + 16])))
            }
            col::NAV_CACHE => {
                let f = c.f32s();
                set_rows(w, rows, |r| NavCache(mat(&f[r * 16..r * 16 + 16])))
            }
            col::VEL => {
                let f = c.f32s();
                set_rows(w, rows, |r| {
                    let v = &f[r * 8..r * 8 + 8];
                    Velocity(Vel { linear: [v[0], v[1], v[2], v[3]], angular: [v[4], v[5], v[6], v[7]] })
                })
            }
            col::POSE_REF => set(w, c.u64s(), |v| PoseRef(UniverseId(v))),
            col::HULL => {
                let h = c.f64s();
                match w.try_col_mut::<Hull>() {
                    Some(dst) => {
                        for (d, v) in dst.iter_mut().zip(h) {
                            d.current = v;
                        }
                        true
                    }
                    None => false,
                }
            }
            col::ORDERS => {
                let o = c.u32s();
                set_rows(w, rows, |r| OrderState { queue_len: o[2 * r], current: o[2 * r + 1] })
            }
            col::PARENT | col::OWNER | col::CTRL => true, // combined below
            _ => false,
        };
        if !mapped {
            unmapped.push(c.id);
        }
    }
    if let Some(p) = rec.column(col::PARENT) {
        let parents = p.u64s();
        let depths = rec.column(col::DEPTH).map(|d| d.u16s()).unwrap_or_else(|| vec![0; rows]);
        if !set_rows(w, rows, |r| Tree { parent: UniverseId(parents[r]), depth: depths[r] }) {
            unmapped.push(col::PARENT);
        }
    }
    if let Some(o) = rec.column(col::OWNER) {
        let owners = o.u16s();
        let covers = rec.column(col::COVER).map(|c| c.u16s()).unwrap_or_else(|| vec![FactionIx::NONE.0; rows]);
        if !set_rows(w, rows, |r| Owner { owner: FactionIx(owners[r]), cover: FactionIx(covers[r]) }) {
            unmapped.push(col::OWNER);
        }
    }
    if let Some(k) = rec.column(col::CTRL) {
        let kinds = k.u8s();
        let vts = rec.column(col::CTRL_VTABLE).map(|c| c.u32s()).unwrap_or_else(|| vec![0; rows]);
        if !set_rows(w, rows, |r| Controller { kind: CtrlKind::from_u8(kinds[r]), vtable: vts[r] }) {
            unmapped.push(col::CTRL);
        }
    }
}

fn set<T: crate::Component, V: Copy>(w: &mut World, values: Vec<V>, f: impl Fn(V) -> T) -> bool {
    match w.try_col_mut::<T>() {
        Some(dst) => {
            for (d, v) in dst.iter_mut().zip(values) {
                *d = f(v);
            }
            true
        }
        None => false,
    }
}

fn set_rows<T: crate::Component>(w: &mut World, rows: usize, f: impl Fn(usize) -> T) -> bool {
    match w.try_col_mut::<T>() {
        Some(dst) => {
            for (r, d) in dst.iter_mut().enumerate().take(rows) {
                *d = f(r);
            }
            true
        }
        None => false,
    }
}

/// Produces a frame record from the universe's registered core components (the inverse of
/// [`mirror`]). Unregistered components are omitted (except `uid`, always present).
pub fn capture_universe(u: &Universe, frame: u64, game_time: f64) -> FrameRecord {
    let mut sectors = Vec::with_capacity(u.sector_count());
    for w in u.worlds() {
        let rows = w.len() as u32;
        let mut cols = vec![ColumnData::from_u64s(col::UID, &w.uids().iter().map(|x| x.0).collect::<Vec<_>>())];
        if let Some(c) = w.try_col::<ClassId>() {
            cols.push(ColumnData::from_u8s(col::CLASS, &c.iter().map(|x| x.0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<ClassFlags>() {
            cols.push(ColumnData::from_u16s(col::CLASS_FLAGS, &c.iter().map(|x| x.0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<LocalOffset>() {
            cols.push(ColumnData::from_f32s(col::LOCAL_OFFSET, &c.iter().flat_map(|x| x.0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<Tree>() {
            cols.push(ColumnData::from_u64s(col::PARENT, &c.iter().map(|x| x.parent.0).collect::<Vec<_>>()));
            cols.push(ColumnData::from_u16s(col::DEPTH, &c.iter().map(|x| x.depth).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<Attention>() {
            cols.push(ColumnData::from_i32s(col::ATTENTION, &c.iter().map(|x| x.0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<EngineFlags>() {
            cols.push(ColumnData::from_u8s(col::FLAGS, &c.iter().map(|x| x.0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<VtableRva>() {
            cols.push(ColumnData::from_u32s(col::VTABLE, &c.iter().map(|x| x.0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<Controller>() {
            cols.push(ColumnData::from_u8s(col::CTRL, &c.iter().map(|x| x.kind as u8).collect::<Vec<_>>()));
            cols.push(ColumnData::from_u32s(col::CTRL_VTABLE, &c.iter().map(|x| x.vtable).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<Transform>() {
            cols.push(ColumnData::from_f32s(col::POSE, &c.iter().flat_map(|x| x.0.to_flat()).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<Velocity>() {
            let v: Vec<f32> = c.iter().flat_map(|x| x.0.linear.into_iter().chain(x.0.angular)).collect();
            cols.push(ColumnData::from_f32s(col::VEL, &v));
        }
        if let Some(c) = w.try_col::<PoseRef>() {
            cols.push(ColumnData::from_u64s(col::POSE_REF, &c.iter().map(|x| x.0 .0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<NavCache>() {
            cols.push(ColumnData::from_f32s(col::NAV_CACHE, &c.iter().flat_map(|x| x.0.to_flat()).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<Hull>() {
            cols.push(ColumnData::from_f64s(col::HULL, &c.iter().map(|x| x.current).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<Owner>() {
            cols.push(ColumnData::from_u16s(col::OWNER, &c.iter().map(|x| x.owner.0).collect::<Vec<_>>()));
            cols.push(ColumnData::from_u16s(col::COVER, &c.iter().map(|x| x.cover.0).collect::<Vec<_>>()));
        }
        if let Some(c) = w.try_col::<OrderState>() {
            let v: Vec<u32> = c.iter().flat_map(|x| [x.queue_len, x.current]).collect();
            cols.push(ColumnData::from_u32s(col::ORDERS, &v));
        }
        cols.sort_by_key(|c| c.id);
        sectors.push(SectorRecord { root_uid: w.root_uid().0, kind: w.kind() as u8, rows, columns: cols });
    }
    FrameRecord { frame, game_time, flags: 0, sectors }
}
