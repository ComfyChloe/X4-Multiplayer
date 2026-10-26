//! Native cross-check of a mirror capture against values the engine itself returned in the same
//! frame (Lua `MIRRORCHECK` lines from dev/mirror_probe.lua).
//!
//! For each sampled object the Lua side logs, in the UI slot of the frame the probe captured:
//! - `off` = `GetPositionalOffset(id, 0)`: GetTransformRelativeTo with no ancestor = the object's
//!   own local transform (vfunc 648), x/y/z copied from row 0;
//! - `sec` = `GetObjectPositionInSector(id)`: the transform composed up to the sector.
//!
//! Checks:
//! 1. **local**: `off` must equal row 0 of the captured `pose` bit for bit (navigation: current
//!    pathing slot when the state is parent-relative; static: stored transform). When the
//!    pathing state is not parent-relative the engine returns its cached matrix instead, so a
//!    match with `nav_cache` row 0 is reported separately.
//! 2. **sector**: `sec` must equal the composition of the captured local poses along the parent
//!    chain up to the sector, computed with the engine's own operation order ([`apply_local`]).
//!    Only for Object classes: the export refuses everything else and returns zeros (it requires
//!    IsRealClass(object)); zones, clusters and highways are counted as not applicable. Skipped
//!    (and counted by reason) when an ancestor was not recorded, a pose reference is set or an
//!    ancestor's pose was not captured.

use std::collections::HashMap;

use super::{col, FrameRecord};
use crate::comp::Mat4f;

/// One composition step as `Positional::GetLocalTransform` (0x7461c0, apply mode) performs it:
/// `m' = m x local` with the SSE operation order of the engine:
/// row 0: `((y*t2 + x*t1) + z*t3) + t0` per lane (lane 3 uses w for x, y, z);
/// rows 1..3: `(y*t2 + x*t1) + z*t3` per lane. f32 throughout, no fused multiply-add.
#[allow(clippy::needless_range_loop)] // lane indices mirror the SSE lanes
pub fn apply_local(m: &Mat4f, t: &Mat4f) -> Mat4f {
    let (t0, t1, t2, t3) = (t.0[0], t.0[1], t.0[2], t.0[3]);
    let mut out = [[0.0f32; 4]; 4];
    let r = m.0[0];
    for k in 0..4 {
        let (x, y, z) = if k < 3 { (r[0], r[1], r[2]) } else { (r[3], r[3], r[3]) };
        out[0][k] = ((y * t2[k] + x * t1[k]) + z * t3[k]) + t0[k];
    }
    for i in 1..4 {
        let r = m.0[i];
        for k in 0..4 {
            out[i][k] = (r[1] * t2[k] + r[0] * t1[k]) + r[2] * t3[k];
        }
    }
    Mat4f(out)
}

/// Primary vtable RVA of U::Highway (layout table of build 23660954).
pub const HIGHWAY_VTABLE: u32 = 0x2b091e0;

/// Applies one component's local transform to `m` the way its class's GetLocalTransform does in
/// apply mode (the step GetTransformRelativeTo performs per ancestor). `pose` is the captured
/// controller matrix, `offset` the captured `local_offset`. `None` when the class overrides
/// GetLocalTransform in a way the capture does not model (Explosive/Missile/Bomb,
/// HighwayScene, HighwayBooster).
/// - standard classes (Positional::GetLocalTransform 0x7461c0): `apply_local(m, pose)`;
/// - U::Highway (0x5f79f0): row 0 of `m` += offset (per lane), then `apply_local`.
pub fn local_step(m: &Mat4f, pose: &Mat4f, class_flags: u16, vtable: u32, offset: [f32; 4]) -> Option<Mat4f> {
    if class_flags & crate::comp::ClassFlags::CUSTOM_LOCAL == 0 {
        return Some(apply_local(m, pose));
    }
    if vtable != HIGHWAY_VTABLE {
        return None;
    }
    let mut shifted = *m;
    for (k, o) in offset.iter().enumerate() {
        shifted.0[0][k] += *o;
    }
    Some(apply_local(&shifted, pose))
}

/// One parsed `MIRRORCHECK` log line.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CheckLine {
    pub frame: u64,
    pub uid: u64,
    pub off: [f32; 3],
    pub sec: [f32; 3],
}

/// Parses `[X4LAN] MIRRORCHECK frame=<n> uid=<id> off=<x>,<y>,<z> sec=<x>,<y>,<z>` from any line.
pub fn parse_check_line(line: &str) -> Option<CheckLine> {
    let at = line.find("MIRRORCHECK ")?;
    let mut frame = None;
    let mut uid = None;
    let mut off = None;
    let mut sec = None;
    let triple = |v: &str| -> Option<[f32; 3]> {
        let mut it = v.split(',').map(|x| x.trim().parse::<f64>().ok().map(|d| d as f32));
        Some([it.next()??, it.next()??, it.next()??])
    };
    for kv in line[at + 12..].split_whitespace() {
        let (k, v) = kv.split_once('=')?;
        match k {
            "frame" => frame = v.parse::<u64>().ok(),
            "uid" => uid = v.parse::<f64>().ok().map(|d| d as u64),
            "off" => off = triple(v),
            "sec" => sec = triple(v),
            _ => {}
        }
    }
    Some(CheckLine { frame: frame?, uid: uid?, off: off?, sec: sec? })
}

/// Outcome of [`check_frames`].
#[derive(Debug, Clone, Default, PartialEq)]
pub struct CheckReport {
    pub lines: usize,
    pub frame_missing: usize,
    pub uid_missing: usize,
    pub local_exact: usize,
    pub local_cache_exact: usize,
    pub local_mismatch: usize,
    pub local_max_ulps: u64,
    /// Classes whose GetLocalTransform the capture does not model (no local or sector check).
    pub unsupported_class: usize,
    /// Of `local_exact`: rows of a class with a modelled custom GetLocalTransform (U::Highway).
    pub custom_local_exact: usize,
    pub sector_exact: usize,
    pub sector_mismatch: usize,
    /// Not an Object class: the export answers zeros by design.
    pub sector_not_object: usize,
    pub sector_skipped_ancestor: usize,
    pub sector_skipped_ref: usize,
    pub sector_skipped_pose: usize,
    pub sector_max_ulps: u64,
    /// First few mismatch descriptions.
    pub notes: Vec<String>,
}

impl CheckReport {
    pub fn line(&self) -> String {
        format!(
            "MIRRORCHECK RESULT lines={} frame_missing={} uid_missing={} local_exact={} local_cache_exact={} local_mismatch={} local_max_ulps={} unsupported_class={} custom_local_exact={} sector_exact={} sector_mismatch={} sector_not_object={} sector_skipped_ancestor={} sector_skipped_ref={} sector_skipped_pose={} sector_max_ulps={}",
            self.lines,
            self.frame_missing,
            self.uid_missing,
            self.local_exact,
            self.local_cache_exact,
            self.local_mismatch,
            self.local_max_ulps,
            self.unsupported_class,
            self.custom_local_exact,
            self.sector_exact,
            self.sector_mismatch,
            self.sector_not_object,
            self.sector_skipped_ancestor,
            self.sector_skipped_ref,
            self.sector_skipped_pose,
            self.sector_max_ulps
        )
    }

    /// Every checked value matched bit for bit (local always; sector where checkable).
    pub fn exact(&self) -> bool {
        self.lines > 0
            && self.frame_missing == 0
            && self.uid_missing == 0
            && self.local_mismatch == 0
            && self.sector_mismatch == 0
    }
}

struct Row {
    parent: u64,
    class: u8,
    flags: u8,
    /// C++ class derives from U::Object (capture column class_flags; older captures: by class id).
    object: bool,
    pose: Mat4f,
    cache: Option<Mat4f>,
    pose_ref: u64,
    class_flags: u16,
    vtable: u32,
    offset: [f32; 4],
}

impl Row {
    fn step(&self, m: &Mat4f) -> Option<Mat4f> {
        local_step(m, &self.pose, self.class_flags, self.vtable, self.offset)
    }
}

/// Class ids of the recorded non-Object (Space) classes, for captures without class_flags.
const SPACE_CLASSES: [u8; 6] = [0x0f, 0x2f, 0x33, 0x58, 0x6d, 0x76];

fn index_frame(f: &FrameRecord) -> HashMap<u64, Row> {
    let mut map = HashMap::new();
    for s in &f.sectors {
        let (Some(uid), Some(parent), Some(class), Some(flags), Some(pose)) = (
            s.column(col::UID).map(|c| c.u64s()),
            s.column(col::PARENT).map(|c| c.u64s()),
            s.column(col::CLASS).map(|c| c.u8s()),
            s.column(col::FLAGS).map(|c| c.u8s()),
            s.column(col::POSE).map(|c| c.f32s()),
        ) else {
            continue;
        };
        let cache = s.column(col::NAV_CACHE).map(|c| c.f32s());
        let refs = s.column(col::POSE_REF).map(|c| c.u64s());
        let class_flags = s.column(col::CLASS_FLAGS).map(|c| c.u16s());
        let vtables = s.column(col::VTABLE).map(|c| c.u32s());
        let offsets = s.column(col::LOCAL_OFFSET).map(|c| c.f32s());
        for r in 0..uid.len() {
            let m = Mat4f::from_flat(pose[r * 16..r * 16 + 16].try_into().expect("16"));
            let c = cache
                .as_ref()
                .filter(|_| flags[r] & 0x80 != 0)
                .map(|c| Mat4f::from_flat(c[r * 16..r * 16 + 16].try_into().expect("16")));
            map.insert(
                uid[r],
                Row {
                    parent: parent[r],
                    class: class[r],
                    flags: flags[r],
                    object: match &class_flags {
                        Some(f) => f[r] & crate::comp::ClassFlags::OBJECT != 0,
                        None => !SPACE_CLASSES.contains(&class[r]),
                    },
                    pose: m,
                    cache: c,
                    pose_ref: refs.as_ref().map_or(0, |v| v[r]),
                    class_flags: class_flags.as_ref().map_or(0, |v| v[r]),
                    vtable: vtables.as_ref().map_or(0, |v| v[r]),
                    offset: offsets.as_ref().map_or([0.0; 4], |v| [v[r * 4], v[r * 4 + 1], v[r * 4 + 2], v[r * 4 + 3]]),
                },
            );
        }
    }
    map
}

fn ulps3(a: [f32; 3], b: [f32; 3]) -> u64 {
    (0..3).map(|k| crate::shadow::ulps32(a[k], b[k])).max().unwrap_or(0)
}

fn bits3(a: [f32; 3]) -> [u32; 3] {
    [a[0].to_bits(), a[1].to_bits(), a[2].to_bits()]
}

/// Checks every line against the capture frame with the same frame number.
pub fn check_frames(frames: &[FrameRecord], lines: &[CheckLine]) -> CheckReport {
    let mut rep = CheckReport { lines: lines.len(), ..CheckReport::default() };
    let by_frame: HashMap<u64, &FrameRecord> = frames.iter().map(|f| (f.frame, f)).collect();
    let mut indexed: HashMap<u64, HashMap<u64, Row>> = HashMap::new();
    let note = |rep: &mut CheckReport, s: String| {
        if rep.notes.len() < 12 {
            rep.notes.push(s);
        }
    };
    for l in lines {
        let Some(f) = by_frame.get(&l.frame) else {
            rep.frame_missing += 1;
            continue;
        };
        let rows = indexed.entry(l.frame).or_insert_with(|| index_frame(f));
        let Some(row) = rows.get(&l.uid) else {
            rep.uid_missing += 1;
            continue;
        };
        // 1. local transform (identity input, as GetPositionalOffset(id, 0) calls it)
        let Some(local) = row.step(&Mat4f::IDENTITY) else {
            rep.unsupported_class += 1;
            continue;
        };
        let pose0 = local.position();
        if bits3(pose0) == bits3(l.off) {
            rep.local_exact += 1;
            if row.class_flags & crate::comp::ClassFlags::CUSTOM_LOCAL != 0 {
                rep.custom_local_exact += 1;
            }
        } else if row.cache.is_some_and(|c| bits3(c.position()) == bits3(l.off)) {
            rep.local_cache_exact += 1;
        } else {
            rep.local_mismatch += 1;
            rep.local_max_ulps = rep.local_max_ulps.max(ulps3(pose0, l.off));
            note(
                &mut rep,
                format!(
                    "frame {} uid {} local: captured {:?} engine {:?} ref {:#x}",
                    l.frame, l.uid, pose0, l.off, row.pose_ref
                ),
            );
        }
        // 2. composition up to the sector (class 0x58), Object classes only
        if !row.object {
            rep.sector_not_object += 1;
            continue;
        }
        let mut m = Mat4f::IDENTITY;
        let mut at = l.uid;
        let mut skipped = None;
        let mut steps = 0;
        loop {
            let Some(r) = rows.get(&at) else {
                skipped = Some(&mut rep.sector_skipped_ancestor);
                break;
            };
            if r.class == 0x58 {
                break;
            }
            if r.pose_ref != 0 {
                skipped = Some(&mut rep.sector_skipped_ref);
                break;
            }
            if r.flags & 0x20 == 0 || steps > 64 {
                skipped = Some(&mut rep.sector_skipped_pose);
                break;
            }
            let Some(next) = r.step(&m) else {
                skipped = Some(&mut rep.sector_skipped_pose);
                break;
            };
            m = next;
            at = r.parent;
            steps += 1;
        }
        if let Some(counter) = skipped {
            *counter += 1;
            continue;
        }
        let p = m.position();
        if bits3(p) == bits3(l.sec) {
            rep.sector_exact += 1;
        } else {
            rep.sector_mismatch += 1;
            rep.sector_max_ulps = rep.sector_max_ulps.max(ulps3(p, l.sec));
            note(&mut rep, format!("frame {} uid {} sector: composed {:?} engine {:?}", l.frame, l.uid, p, l.sec));
        }
    }
    rep
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apply_identity_and_translation() {
        let t = Mat4f([[10.0, -20.0, 30.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]);
        // identity input: the local transform itself
        assert_eq!(apply_local(&Mat4f::IDENTITY, &t), t);
        // a point in a translated frame: position adds
        let m = Mat4f([[1.5, 2.5, -3.5, 0.0], [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]);
        assert_eq!(apply_local(&m, &t).position(), [11.5, -17.5, 26.5]);
    }

    #[test]
    fn apply_uses_engine_operation_order() {
        // t1 = 1e8, t2 = 1, t3 = -1e8 and x = y = z = 1: the engine adds (y*t2 + x*t1) first,
        // 1 + 1e8 rounds to 1e8 in f32, so the lane is 0; adding x*t1 + z*t3 first would give 1.
        let t = Mat4f([[0.0, 0.0, 0.0, 0.0], [1.0e8, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0], [-1.0e8, 0.0, 0.0, 0.0]]);
        let m = Mat4f([[1.0, 1.0, 1.0, 0.0], [0.0; 4], [0.0; 4], [0.0; 4]]);
        assert_eq!(apply_local(&m, &t).0[0][0], 0.0);
        let (x, y, z) = (1.0f32, 1.0f32, 1.0f32);
        assert_eq!((x * 1.0e8 + z * -1.0e8) + y * 1.0, 1.0, "a different association differs");
    }

    #[test]
    fn parses_check_lines() {
        let l = parse_check_line(
            "[=ERROR=] 12.3 [X4LAN] MIRRORCHECK frame=42 uid=1234567 off=1.5,-2.25,3 sec=10.5,0,-7.125",
        )
        .unwrap();
        assert_eq!(l, CheckLine { frame: 42, uid: 1234567, off: [1.5, -2.25, 3.0], sec: [10.5, 0.0, -7.125] });
        assert!(parse_check_line("[X4LAN] MIRROR done").is_none());
        // 9 significant digits round-trip every f32
        let v = 0.1f32;
        assert_eq!(format!("{:.9e}", v).parse::<f64>().unwrap() as f32, v);
    }
}
