//! Cross-language conformance: `tests/data/mirror_golden.x4cap` is written by the native mirror
//! probe's synthetic test (native/engine/test_mirror.c, fake component tree at the pinned
//! offsets). This test reads it with the Rust reader and checks every captured field against the
//! scene the C test builds, then loads both frames with the mirror.
//!
//! When native/engine/mirror_layout.py changes, rebuild (`build.py --fixedtick --test` fails until
//! the golden file is regenerated with `.local/engine/test_mirror.exe --write-golden`) and update
//! LAYOUT below.

use std::io::Cursor;

use x4mp_sim::capture::{col, mirror, BuildPin, CaptureReader, Dict, FrameRecord, SectorRecord};
use x4mp_sim::comp::{
    Attention, ClassId, Controller, CtrlKind, EngineFlags, Hull, NavCache, OrderState, Owner, PoseRef, Transform, Tree,
    Velocity, VtableRva,
};
use x4mp_sim::{SectorIx, Universe, UniverseId, WorldKind};

const GOLDEN: &[u8] = include_bytes!("data/mirror_golden.x4cap");
const LAYOUT: &str = "7665ec4132ec28ad95b8c638627c1ba7eb59cc493ee5e7b5a9709e248262c46a";

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn mat(base: f32) -> Vec<f32> {
    (0..16).map(|i| base + i as f32 * 0.25).collect()
}

const IDENTITY: [f32; 16] = [0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0.];

fn row_of(s: &SectorRecord, uid: u64) -> usize {
    s.column(col::UID).unwrap().u64s().iter().position(|u| *u == uid).unwrap_or_else(|| panic!("uid {uid} missing"))
}

fn pose(s: &SectorRecord, uid: u64) -> Vec<f32> {
    let r = row_of(s, uid);
    s.column(col::POSE).unwrap().f32s()[r * 16..r * 16 + 16].to_vec()
}

fn read() -> (CaptureReader<Cursor<&'static [u8]>>, Vec<FrameRecord>) {
    let mut r = CaptureReader::open(Cursor::new(GOLDEN)).expect("golden opens");
    let mut frames = Vec::new();
    while let Some(f) = r.next_frame().expect("valid frame") {
        frames.push(f);
    }
    (r, frames)
}

#[test]
fn golden_header_and_dictionary() {
    let (r, frames) = read();
    let h = r.header();
    r.require_pin(&BuildPin::windows()).unwrap();
    assert_eq!(hex(&h.layout_sha256), LAYOUT, "regenerate the golden file and update LAYOUT");
    assert_eq!(h.session_seed, 0x1122_3344_5566_7788);
    assert_eq!((h.interval, h.flags), (1, 0));
    assert_eq!(h.producer, "x4lan_fixedtick mirror v1");
    assert_eq!(h.columns, x4mp_sim::capture::core_schema(), "C and Rust core schemas agree");
    assert_eq!(frames.len(), 2);
    assert_eq!(r.dict(Dict::Faction), vec![(0, "argon"), (1, "teladi_long_faction_id")]);
    let st = r.stats().unwrap();
    assert_eq!((st.frames, st.dropped, st.bytes, st.walk_errors), (2, 0, GOLDEN.len() as u64, 2));
}

#[test]
fn golden_frame_one_fields() {
    let (_, frames) = read();
    let f = &frames[0];
    assert_eq!((f.frame, f.game_time, f.flags), (1, 123.25, 0));
    let roots: Vec<(u64, u8, u32)> = f.sectors.iter().map(|s| (s.root_uid, s.kind, s.rows)).collect();
    assert_eq!(roots, vec![(1, 2, 2), (2, 1, 2), (3, 0, 7), (13, 0, 3)]);
    let uids: Vec<Vec<u64>> = f.sectors.iter().map(|s| s.column(col::UID).unwrap().u64s()).collect();
    assert_eq!(uids, vec![vec![1, 17], vec![2, 16], vec![3, 4, 5, 6, 9, 10, 12], vec![13, 14, 15]]);
    let s1 = &f.sectors[2];
    let get_u8 = |s: &SectorRecord, c: u16| s.column(c).unwrap().u8s();
    assert_eq!(get_u8(s1, col::CLASS), vec![0x58, 0x6d, 0x5b, 0x62, 0x49, 0x04, 0x30]);
    assert_eq!(s1.column(col::PARENT).unwrap().u64s(), vec![2, 3, 4, 4, 8, 4, 4]);
    assert_eq!(s1.column(col::DEPTH).unwrap().u16s(), vec![0, 1, 2, 2, 5, 2, 2]);
    assert_eq!(s1.column(col::ATTENTION).unwrap().i32s(), vec![5, 8, 9, 8, 8, 8, 8]);
    assert_eq!(get_u8(s1, col::FLAGS), vec![0x31, 0x31, 0xf1, 0x31, 0x3b, 0x31, 0x31]);
    assert_eq!(
        s1.column(col::VTABLE).unwrap().u32s(),
        vec![0x2b1ea48, 0x2b66080, 0x2b7feb8, 0x2b764d8, 0x2b82440, 0x2ab3148, 0x2b13b50]
    );
    assert_eq!(get_u8(s1, col::CTRL), vec![0, 2, 1, 2, 0, 0, 0]);
    assert_eq!(s1.column(col::CTRL_VTABLE).unwrap().u32s(), vec![0, 0x2a34b38, 0x2c51158, 0x2a34b38, 0, 0, 0]);
    assert_eq!(pose(s1, 5), mat(1007.0), "ship1: current pathing slot (index 1)");
    assert_eq!(pose(s1, 6), mat(-300.0), "station: static controller transform");
    assert_eq!(pose(s1, 4), mat(500.0), "zone: static controller transform");
    assert_eq!(pose(s1, 9), IDENTITY.to_vec(), "no controller: identity (GetLocalTransform)");
    let r5 = row_of(s1, 5);
    let vel = s1.column(col::VEL).unwrap().f32s()[r5 * 8..r5 * 8 + 8].to_vec();
    assert_eq!(vel, vec![17.0, 18.0, 19.0, 20.0, -17.0 / 64.0, -18.0 / 64.0, -19.0 / 64.0, -20.0 / 64.0]);
    assert_eq!(s1.column(col::POSE_REF).unwrap().u64s()[r5], 0);
    let cache = s1.column(col::NAV_CACHE).unwrap().f32s()[r5 * 16..r5 * 16 + 16].to_vec();
    assert_eq!(cache, mat(-7.0));
    let hull = s1.column(col::HULL).unwrap().f64s();
    assert!(hull[0].is_nan() && hull[1].is_nan(), "sector and zone are not destructible");
    assert_eq!(&hull[2..], &[1234.5, 50000.0, 77.0, 3.0, 0.0]);
    assert_eq!(s1.column(col::OWNER).unwrap().u16s(), vec![0xffff, 0xffff, 0, 1, 0xffff, 0xffff, 0xffff]);
    assert_eq!(s1.column(col::COVER).unwrap().u16s(), vec![0xffff, 0xffff, 0xffff, 0, 0xffff, 0xffff, 0xffff]);
    assert_eq!(s1.column(col::ORDERS).unwrap().u32s(), vec![0, 0, 0, 0, 3, 1, 0, 0, 0, 0, 0, 0, 0, 0]);
    // galaxy and cluster records
    let g = &f.sectors[0];
    assert_eq!(get_u8(g, col::CLASS), vec![0x2f, 0x55]);
    assert_eq!(g.column(col::PARENT).unwrap().u64s(), vec![0, 1]);
    assert_eq!(g.column(col::DEPTH).unwrap().u16s(), vec![0, 1]);
    let c = &f.sectors[1];
    assert_eq!(get_u8(c, col::CLASS), vec![0x0f, 0x33]);
    assert_eq!(c.column(col::PARENT).unwrap().u64s(), vec![1, 2]);
}

#[test]
fn golden_frame_two_changes() {
    let (_, frames) = read();
    let f = &frames[1];
    assert_eq!(f.frame, 2);
    let uids: Vec<Vec<u64>> = f.sectors.iter().map(|s| s.column(col::UID).unwrap().u64s()).collect();
    assert_eq!(uids, vec![vec![1, 17], vec![2, 16], vec![3, 4, 5, 6, 15, 10, 12], vec![13, 14]]);
    let s1 = &f.sectors[1 + 1];
    assert_eq!(pose(s1, 5), mat(7.0), "pathing index flipped to slot 0");
    assert_eq!(pose(s1, 15), mat(-50.0));
    let r5 = row_of(s1, 5);
    assert_eq!(s1.column(col::POSE_REF).unwrap().u64s()[r5], 0xdead);
    assert_eq!(s1.column(col::HULL).unwrap().f64s()[r5], 1000.25);
    let r15 = row_of(s1, 15);
    assert_eq!(s1.column(col::PARENT).unwrap().u64s()[r15], 4);
    assert_eq!(s1.column(col::OWNER).unwrap().u16s()[r15], 1);
}

#[test]
fn golden_frames_load_into_a_universe() {
    let (r, frames) = read();
    let factions = r.dict(Dict::Faction);
    let mut u = Universe::new(1);
    u.register::<VtableRva>().register::<NavCache>();
    let rep = mirror(&frames[0], &factions, &mut u).unwrap();
    assert_eq!((rep.added_sectors, rep.spawned, rep.rows), (4, 14, 14));
    assert!(rep.unmapped_columns.is_empty(), "{:?}", rep.unmapped_columns);
    u.check().unwrap();
    assert_eq!(u.world(SectorIx(0)).kind(), WorldKind::Galaxy);
    assert_eq!(u.world(SectorIx(1)).kind(), WorldKind::Cluster);
    let ship1 = u.ids().by_uid(UniverseId(5)).unwrap();
    let ship3 = u.ids().by_uid(UniverseId(15)).unwrap();
    assert_eq!(u.get::<ClassId>(ship1), Some(&ClassId(0x5b)));
    assert_eq!(u.get::<Attention>(ship1), Some(&Attention(9)));
    assert_eq!(u.get::<Tree>(ship1), Some(&Tree { parent: UniverseId(4), depth: 2 }));
    assert_eq!(u.get::<Controller>(ship1), Some(&Controller { kind: CtrlKind::Navigation, vtable: 0x2c51158 }));
    assert_eq!(u.get::<Transform>(ship1).unwrap().0.to_flat().to_vec(), mat(1007.0));
    assert_eq!(u.get::<Velocity>(ship1).unwrap().0.linear, [17.0, 18.0, 19.0, 20.0]);
    assert_eq!(u.get::<PoseRef>(ship1), Some(&PoseRef(UniverseId(0))));
    assert_eq!(u.get::<NavCache>(ship1).unwrap().0.to_flat().to_vec(), mat(-7.0));
    assert_eq!(u.get::<VtableRva>(ship1), Some(&VtableRva(0x2b7feb8)));
    assert_eq!(u.get::<Hull>(ship1).unwrap().current, 1234.5);
    assert_eq!(u.get::<Owner>(ship1).unwrap().owner.0, 0);
    assert_eq!(u.get::<OrderState>(ship1), Some(&OrderState { queue_len: 3, current: 1 }));
    assert_eq!(u.factions().get(&1).map(String::as_str), Some("teladi_long_faction_id"));
    let ship2 = u.ids().by_uid(UniverseId(9)).unwrap();
    let f2 = *u.get::<EngineFlags>(ship2).unwrap();
    assert!(f2.registered() && f2.alt_mode() && f2.death_state() == 1);
    // Frame 2: ship2 gone, ship3 migrated into sector 1 at its tree position.
    let rep = mirror(&frames[1], &factions, &mut u).unwrap();
    assert_eq!((rep.spawned, rep.despawned, rep.migrated), (0, 1, 1));
    u.check().unwrap();
    assert_eq!(u.ids().by_uid(UniverseId(15)), Some(ship3), "same handle across the migration");
    assert_eq!(u.ids().loc(ship3).unwrap().sector, SectorIx(2));
    let order: Vec<u64> = u.world(SectorIx(2)).uids().iter().map(|x| x.0).collect();
    assert_eq!(order, vec![3, 4, 5, 6, 15, 10, 12]);
    assert_eq!(u.get::<Transform>(ship1).unwrap().0.to_flat().to_vec(), mat(7.0));
    assert!(u.ids().by_uid(UniverseId(9)).is_none());
    // Round trip: the universe reproduces frame 2's core columns byte for byte.
    let back = x4mp_sim::capture::capture_universe(&u, 2, f64::from_bits(frames[1].game_time.to_bits()));
    for (a, b) in back.sectors.iter().zip(&frames[1].sectors) {
        assert_eq!(a.root_uid, b.root_uid);
        for c in &b.columns {
            assert_eq!(a.column(c.id), Some(c), "column {:#x} of root {}", c.id, b.root_uid);
        }
    }
}

/// The native check logic against the golden capture: lines built from the engine's own
/// composition rule must match exactly; a one-ulp change must be reported.
#[test]
fn check_logic_on_golden_frames() {
    use x4mp_sim::capture::check::{apply_local, check_frames, parse_check_line};
    use x4mp_sim::comp::Mat4f;
    let (_, frames) = read();
    let s1 = &frames[0].sectors[2];
    let m = |uid: u64| Mat4f::from_flat(pose(s1, uid).try_into().unwrap());
    // ship1 (nav, in zone 4) and the station (static, in zone 4)
    let mut lines = Vec::new();
    for uid in [5u64, 6] {
        let local = m(uid);
        let sector = apply_local(&apply_local(&Mat4f::IDENTITY, &local), &m(4));
        let (o, s) = (local.position(), sector.position());
        let text = format!(
            "[X4LAN] MIRRORCHECK frame=1 uid={uid} off={:.9e},{:.9e},{:.9e} sec={:.9e},{:.9e},{:.9e}",
            o[0], o[1], o[2], s[0], s[1], s[2]
        );
        lines.push(parse_check_line(&text).unwrap());
    }
    let rep = check_frames(&frames, &lines);
    assert!(rep.exact(), "{}", rep.line());
    assert_eq!((rep.local_exact, rep.sector_exact, rep.sector_skipped_ancestor), (2, 2, 0));
    // the docked ship's chain passes through unrecorded components: sector check skipped
    let mut l = lines[0];
    l.uid = 9;
    l.off = [0.0, 0.0, 0.0];
    let rep = check_frames(&frames, &[l]);
    assert_eq!((rep.local_exact, rep.sector_skipped_ancestor), (1, 1));
    // a zone is not an Object class: the export answers zeros, the check does not apply
    let mut z = lines[0];
    z.uid = 4;
    z.off = m(4).position();
    z.sec = [0.0; 3];
    let rep = check_frames(&frames, &[z]);
    assert!(rep.exact(), "{}", rep.line());
    assert_eq!((rep.local_exact, rep.sector_not_object), (1, 1));
    // one ulp off is a mismatch
    let mut bad = lines[1];
    bad.sec[1] = f32::from_bits(bad.sec[1].to_bits() + 1);
    let rep = check_frames(&frames, &[bad]);
    assert!(!rep.exact());
    assert_eq!((rep.sector_mismatch, rep.sector_max_ulps), (1, 1));
    // a frame that was not captured
    let mut late = lines[0];
    late.frame = 99;
    assert_eq!(check_frames(&frames, &[late]).frame_missing, 1);
}
