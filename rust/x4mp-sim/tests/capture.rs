//! Capture format: write/read round trip, corruption and version handling, schema checks,
//! mirror loading across frames, and replay determinism (mirror -> step -> capture twice).

mod common;

use std::io::Cursor;

use common::{build_universe, frame, Traffic};
use x4mp_sim::capture::{
    capture_universe, col, core_schema, mirror, mirror_subset, BuildPin, CapError, CaptureReader, CaptureWriter,
    ColumnData, Dict, FrameRecord, Header, SectorRecord, FRAME_SUBSET, FRAME_TRUNCATED,
};
use x4mp_sim::comp::{Attention, ClassId, Controller, CtrlKind, Hull, Owner, Transform, Tree};
use x4mp_sim::shadow::{Comparator, Policy};
use x4mp_sim::{Pool, SectorIx, Universe, UniverseId};

fn sector(root: u64, uids: &[u64]) -> SectorRecord {
    let n = uids.len();
    let pose: Vec<f32> = (0..n * 16).map(|i| i as f32 * 0.5 - 3.0).collect();
    SectorRecord {
        root_uid: root,
        kind: 0,
        rows: n as u32,
        columns: vec![
            ColumnData::from_u64s(col::UID, uids),
            ColumnData::from_u8s(col::CLASS, &vec![0x5c; n]),
            ColumnData::from_u64s(col::PARENT, &vec![root; n]),
            ColumnData::from_u16s(col::DEPTH, &vec![2; n]),
            ColumnData::from_i32s(col::ATTENTION, &(0..n as i32).map(|i| 1 + i % 11).collect::<Vec<_>>()),
            ColumnData::from_u8s(col::CTRL, &vec![1; n]),
            ColumnData::from_u32s(col::CTRL_VTABLE, &vec![0x2c51158; n]),
            ColumnData::from_f32s(col::POSE, &pose),
            ColumnData::from_f64s(col::HULL, &(0..n).map(|i| 1000.0 - i as f64 * 0.1).collect::<Vec<_>>()),
            ColumnData::from_u16s(col::OWNER, &vec![3; n]),
            ColumnData::from_u16s(col::COVER, &vec![0xffff; n]),
        ],
    }
}

fn header() -> Header {
    let mut h = Header::synthetic("capture-test", 77);
    h.pin = BuildPin::windows();
    h.layout_sha256 = [0xab; 32];
    h
}

fn write(frames: &[FrameRecord]) -> Vec<u8> {
    let mut w = CaptureWriter::new(Vec::new(), header()).unwrap();
    w.dict(Dict::Faction, &[(3, "argon"), (4, "xenon")]).unwrap();
    for f in frames {
        w.frame(f).unwrap();
    }
    w.note_dropped();
    w.finish().unwrap()
}

fn frames() -> Vec<FrameRecord> {
    vec![
        FrameRecord {
            frame: 10,
            game_time: 1.25,
            flags: 0,
            sectors: vec![sector(0xa0, &[1, 2, 3]), sector(0xa1, &[4])],
        },
        FrameRecord {
            frame: 11,
            game_time: 1.5,
            flags: 0,
            sectors: vec![sector(0xa0, &[2, 3]), sector(0xa1, &[4, 1, 5])],
        },
        FrameRecord { frame: 12, game_time: 1.75, flags: 0, sectors: vec![sector(0xa0, &[]), sector(0xa1, &[5, 4])] },
    ]
}

#[test]
fn subset_updates_preserve_omitted_roots_and_resolve_roots_by_uid() {
    let mut u = Universe::new(77);
    mirror(&frames()[0], &[], &mut u).unwrap();
    let old1 = u.ids().by_uid(UniverseId(1)).unwrap();
    let old2 = u.ids().by_uid(UniverseId(2)).unwrap();
    let f = FrameRecord { frame: 11, game_time: 1.5, flags: FRAME_SUBSET, sectors: vec![sector(0xa1, &[4, 1, 5])] };
    // Explicit opt-in is required, and rejection leaves the mirror alone.
    let before = u.state_hash();
    assert!(mirror(&f, &[], &mut u).is_err());
    assert_eq!(before, u.state_hash());
    let r = mirror_subset(&f, &[], &mut u).unwrap();
    assert!(r.subset);
    assert_eq!(r.updated_sectors, vec![SectorIx(1)]);
    assert_eq!((r.spawned, r.migrated, r.despawned), (1, 1, 0));
    assert_eq!(u.ids().by_uid(UniverseId(1)), Some(old1));
    assert_eq!(u.ids().by_uid(UniverseId(2)), Some(old2));
    assert_eq!(u.world(SectorIx(0)).uids(), &[UniverseId(2), UniverseId(3)]);
    assert_eq!(u.world(SectorIx(1)).uids(), &[UniverseId(4), UniverseId(1), UniverseId(5)]);

    // An observed empty root removes its rows; an omitted root keeps its last observation.
    let empty = FrameRecord { frame: 12, game_time: 1.75, flags: FRAME_SUBSET, sectors: vec![sector(0xa1, &[])] };
    let r = mirror_subset(&empty, &[], &mut u).unwrap();
    assert_eq!(r.despawned, 3);
    assert_eq!(u.world(SectorIx(0)).uids(), &[UniverseId(2), UniverseId(3)]);
    assert!(u.check().is_ok());
}

#[test]
fn subset_requires_initialized_roots_and_refuses_truncation_before_mutation() {
    let mut u = Universe::new(77);
    let mut f = frames()[0].clone();
    f.flags = FRAME_SUBSET;
    assert!(mirror_subset(&f, &[], &mut u).is_err());
    assert_eq!(u.sector_count(), 0);
    mirror(&frames()[0], &[], &mut u).unwrap();
    let before = u.state_hash();
    f.sectors.push(sector(0xa2, &[9]));
    assert!(mirror_subset(&f, &[], &mut u).is_err());
    assert_eq!(before, u.state_hash());
    f = frames()[1].clone();
    f.flags = FRAME_TRUNCATED;
    assert!(mirror(&f, &[], &mut u).is_err());
    f.flags |= FRAME_SUBSET;
    assert!(mirror_subset(&f, &[], &mut u).is_err());
    assert_eq!(before, u.state_hash());
}

#[test]
fn write_read_roundtrip() {
    let fr = frames();
    let bytes = write(&fr);
    let mut r = CaptureReader::open(Cursor::new(&bytes)).unwrap();
    assert_eq!(r.header(), &header());
    r.require_pin(&BuildPin::windows()).unwrap();
    r.require_layout(&[0xab; 32]).unwrap();
    assert!(r.require_layout(&[0; 32]).is_err());
    assert!(r.require_pin(&BuildPin::synthetic()).is_err());
    let mut got = Vec::new();
    while let Some(f) = r.next_frame().unwrap() {
        got.push(f);
    }
    assert_eq!(got, fr);
    assert_eq!(r.dict_value(Dict::Faction, 4), Some("xenon"));
    let st = r.stats().unwrap();
    assert_eq!((st.frames, st.dropped, st.bytes), (3, 1, bytes.len() as u64));
}

#[test]
fn every_single_byte_corruption_is_detected() {
    let bytes = write(&frames()[..1]);
    for i in 0..bytes.len() {
        let mut b = bytes.clone();
        b[i] ^= 0x01;
        let result = (|| -> Result<usize, CapError> {
            let mut r = CaptureReader::open(Cursor::new(&b))?;
            let mut n = 0;
            while r.next_frame()?.is_some() {
                n += 1;
            }
            r.stats().ok_or(CapError::Truncated { offset: 0 })?;
            Ok(n)
        })();
        assert!(result.is_err(), "corruption at byte {i} not detected");
    }
}

#[test]
fn truncation_version_and_magic() {
    let bytes = write(&frames());
    for cut in [bytes.len() - 1, bytes.len() - 9, 40, 17] {
        let mut r = match CaptureReader::open(Cursor::new(&bytes[..cut])) {
            Ok(r) => r,
            Err(CapError::Truncated { .. }) => continue,
            Err(e) => panic!("cut {cut}: {e}"),
        };
        let mut err = None;
        loop {
            match r.next_frame() {
                Ok(Some(_)) => {}
                Ok(None) => break,
                Err(e) => {
                    err = Some(e);
                    break;
                }
            }
        }
        assert!(matches!(err, Some(CapError::Truncated { .. })), "cut {cut}: {err:?}");
    }
    let mut v2 = bytes.clone();
    v2[8] = 2;
    assert!(matches!(CaptureReader::open(Cursor::new(&v2)), Err(CapError::Version { major: 2, .. })));
    // A newer minor version is readable, but the file start must agree with HEAD's copy.
    let mut minor = bytes.clone();
    minor[10] = 9;
    assert!(matches!(CaptureReader::open(Cursor::new(&minor)), Err(CapError::Malformed { .. })));
    assert!(matches!(CaptureReader::open(Cursor::new(b"NOTACAPTUREFILE!")), Err(CapError::BadMagic)));
}

#[test]
fn writer_refuses_schema_violations() {
    let mut w = CaptureWriter::new(Vec::new(), header()).unwrap();
    let mut bad = frames()[0].clone();
    bad.sectors[0].columns[1] = ColumnData::from_u8s(col::CLASS, &[1, 2]); // 2 values for 3 rows
    assert!(w.frame(&bad).is_err());
    let mut unknown = frames()[0].clone();
    unknown.sectors[0].columns.push(ColumnData::from_u8s(0x777, &[0, 0, 0]));
    assert!(w.frame(&unknown).is_err());
    assert_eq!(core_schema().len(), 19);
}

#[test]
fn mirror_tracks_entities_across_frames() {
    let fr = frames();
    let mut u = Universe::new(5);
    let f0 = mirror(&fr[0], &[(3, "argon")], &mut u).unwrap();
    assert_eq!((f0.added_sectors, f0.spawned, f0.despawned, f0.migrated, f0.rows), (2, 4, 0, 0, 4));
    u.check().unwrap();
    let e1 = u.ids().by_uid(UniverseId(1)).unwrap();
    assert_eq!(u.get::<ClassId>(e1), Some(&ClassId(0x5c)));
    assert_eq!(u.get::<Attention>(e1), Some(&Attention(1)));
    assert_eq!(u.get::<Tree>(e1), Some(&Tree { parent: UniverseId(0xa0), depth: 2 }));
    assert_eq!(u.get::<Controller>(e1).map(|c| (c.kind, c.vtable)), Some((CtrlKind::Navigation, 0x2c51158)));
    assert_eq!(u.get::<Hull>(e1).unwrap().current, 1000.0);
    assert_eq!(u.get::<Owner>(e1).unwrap().apparent().0, 3);
    assert_eq!(u.factions().get(&3).map(|s| s.as_str()), Some("argon"));
    // Frame 11: entity 1 moved to sector a1 (in the middle), 5 is new.
    let f1 = mirror(&fr[1], &[], &mut u).unwrap();
    assert_eq!((f1.spawned, f1.despawned, f1.migrated), (1, 0, 1));
    u.check().unwrap();
    assert_eq!(u.ids().by_uid(UniverseId(1)), Some(e1), "same handle after migration");
    assert_eq!(u.ids().loc(e1).unwrap().sector, SectorIx(1));
    let order: Vec<u64> = u.world(SectorIx(1)).uids().iter().map(|x| x.0).collect();
    assert_eq!(order, vec![4, 1, 5], "rows in record (tree) order");
    // Hull and pose of entity 1 now come from its new row (row 1 of sector a1).
    assert_eq!(u.get::<Hull>(e1).unwrap().current, 1000.0 - 0.1);
    assert_eq!(u.get::<Transform>(e1).unwrap().0 .0[0][0], 16.0 * 0.5 - 3.0);
    // Frame 12: 1, 2, 3 gone, 5 and 4 swap.
    let f2 = mirror(&fr[2], &[], &mut u).unwrap();
    assert_eq!((f2.spawned, f2.despawned, f2.migrated), (0, 3, 0));
    u.check().unwrap();
    assert_eq!(u.world(SectorIx(1)).uids(), &[UniverseId(5), UniverseId(4)]);
    assert!(u.world(SectorIx(0)).is_empty());
    // Round trip: the universe reproduces the frame exactly (all captured core columns).
    let back = capture_universe(&u, 12, 1.75);
    assert_eq!(back.sectors[1].column(col::UID), fr[2].sectors[1].column(col::UID));
    for id in [
        col::CLASS,
        col::PARENT,
        col::DEPTH,
        col::ATTENTION,
        col::CTRL,
        col::CTRL_VTABLE,
        col::POSE,
        col::HULL,
        col::OWNER,
        col::COVER,
    ] {
        assert_eq!(back.sectors[1].column(id), fr[2].sectors[1].column(id), "column {id:#x}");
    }
}

#[test]
fn mirror_refuses_changed_sector_list_and_duplicates() {
    let fr = frames();
    let mut u = Universe::new(5);
    mirror(&fr[0], &[], &mut u).unwrap();
    let mut other = fr[1].clone();
    other.sectors[0].root_uid = 0xdead;
    assert!(mirror(&other, &[], &mut u).is_err());
    let mut dup = fr[1].clone();
    dup.sectors[1] = sector(0xa1, &[4, 4]);
    let mut fresh = Universe::new(5);
    assert!(mirror(&dup, &[], &mut fresh).is_err());
}

/// Replay determinism: record a live universe through the capture format, mirror it into a
/// fresh universe (plus the system-owned state a real recording would carry), check the mirror
/// equals the source field by field, then replay: 1 and 16 threads and a repeated run must give
/// identical hash sequences.
#[test]
fn replay_from_capture_is_deterministic() {
    let pool = Pool::new(8);
    let mut live = build_universe(0x77, 10, 50);
    let traffic = Traffic::new(10);
    for f in 0..5 {
        live.step(&pool, &[&traffic], &frame(f));
    }
    let rec = capture_universe(&live, 5, 5.0 / 60.0);
    let mut file = CaptureWriter::new(Vec::new(), Header::synthetic("replay-test", 0x77)).unwrap();
    file.frame(&rec).unwrap();
    let bytes = file.finish().unwrap();
    let system_state: Vec<Vec<SystemState>> =
        (0..live.sector_count()).map(|s| live_state(&live, SectorIx(s as u32))).collect();

    let load = || -> Universe {
        let mut r = CaptureReader::open(Cursor::new(&bytes)).unwrap();
        let f = r.next_frame().unwrap().unwrap();
        let mut u = build_universe(0x77, 0, 0); // same registrations, no sectors
        mirror(&f, &[], &mut u).unwrap();
        for rows in &system_state {
            for (uid, age, rng, hull_max) in rows {
                let e = u.ids().by_uid(*uid).unwrap();
                *u.get_mut::<common::Age>(e).unwrap() = *age;
                *u.get_mut::<x4mp_sim::comp::RngState>(e).unwrap() = *rng;
                u.get_mut::<Hull>(e).unwrap().max = *hull_max;
            }
        }
        u
    };
    // The mirror equals its source in every column (rows matched by UniverseId, same order).
    let mut cmp = Comparator::new(Policy::exact());
    live.compare(&load(), &[], 5, &mut cmp);
    assert!(cmp.is_clean(), "{}", cmp.summary());
    assert!(cmp.compared() > 500 * 10, "compared {}", cmp.compared());

    let replay = |threads: usize| -> Vec<u64> {
        let pool = Pool::new(threads);
        let mut u = load();
        let t = Traffic::new(10);
        (5..15)
            .map(|f| {
                u.step(&pool, &[&t], &frame(f));
                u.state_hash()
            })
            .collect()
    };
    let a = replay(1);
    assert_eq!(a, replay(16), "replay depends on thread count");
    assert_eq!(a, replay(1), "replay is not repeatable");
}

/// State a recording carries besides the engine capture: the system's own component, the
/// per-entity streams, and max hull (a virtual in the engine, not in the v1 core columns).
type SystemState = (UniverseId, common::Age, x4mp_sim::comp::RngState, f64);

fn live_state(u: &Universe, s: SectorIx) -> Vec<SystemState> {
    let w = u.world(s);
    (0..w.len())
        .map(|r| {
            (w.uids()[r], w.col::<common::Age>()[r], w.col::<x4mp_sim::comp::RngState>()[r], w.col::<Hull>()[r].max)
        })
        .collect()
}
