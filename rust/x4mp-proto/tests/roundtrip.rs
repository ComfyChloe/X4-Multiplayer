//! Property-style round-trip tests with a deterministic generator.

mod common;

use common::*;
use x4mp_proto::msg::{RECORD_BASE_LEN, RECORD_COMBAT_LEN, SNAPSHOT_HEAD_LEN};
use x4mp_proto::quant;
use x4mp_proto::*;

const CASES: usize = 3000;

#[test]
fn every_message_kind_round_trips_and_encodes_canonically() {
    let mut rng = Rng::new(0x5EED_0001);
    for kind in Kind::ALL.into_iter().filter(|k| *k != Kind::Snapshot) {
        for _ in 0..CASES {
            let storage = Storage::new(&mut rng);
            let message = message(&mut rng, kind, &storage);
            let player = if rng.chance(30) { None } else { Some(any_player(&mut rng)) };
            let lease = lease(&mut rng);
            let bytes = encode(player, lease, &message).unwrap();
            assert!(bytes.len() <= MAX_DATAGRAM);
            let packet = decode(&bytes).unwrap_or_else(|e| panic!("{kind:?}: {e}"));
            assert_eq!(packet.header, Header { minor: MINOR, kind, flags: 0, player, lease });
            assert_eq!(packet.message, message);
            assert_eq!(encode(player, lease, &packet.message).unwrap(), bytes, "{kind:?} not canonical");
        }
    }
}

#[test]
fn snapshots_round_trip_split_and_stay_within_the_datagram_limit() {
    let mut rng = Rng::new(0x5EED_0002);
    for _ in 0..CASES {
        let frame = rng.interesting_u64();
        let states: Vec<ObjectState> = (0..rng.below(80)).map(|_| state(&mut rng, frame)).collect();
        let source = any_player(&mut rng);
        let datagrams = encode_snapshots(Some(source), None, source, frame, &states).unwrap();
        let mut decoded = Vec::new();
        for bytes in &datagrams {
            assert!(bytes.len() <= MAX_DATAGRAM);
            let packet = decode(bytes).unwrap();
            let Message::Snapshot(view) = packet.message else { panic!("not a snapshot") };
            assert_eq!((view.source, view.frame), (source, frame));
            assert_eq!(view.records().count(), view.len());
            decoded.extend(view.records().map(|r| *r.state()));
            assert_eq!(encode(Some(source), None, &Message::Snapshot(view)).unwrap(), *bytes);
        }
        assert_eq!(decoded, states);
        // Greedy packing: each datagram but the last was too full for the next record.
        let mut offset = 0;
        for bytes in &datagrams[..datagrams.len() - 1] {
            let n = usize::from(bytes[HEADER_LEN + SNAPSHOT_HEAD_LEN - 1]);
            offset += n;
            assert!(bytes.len() + states[offset].wire_len() > MAX_DATAGRAM);
        }
    }
}

#[test]
fn relay_repacking_preserves_record_frames() {
    let mut rng = Rng::new(0x5EED_0003);
    for _ in 0..CASES {
        let frame = 1_000_000 + rng.below(1_000_000);
        let states: Vec<ObjectState> = (0..1 + rng.below(15)).map(|_| state(&mut rng, frame)).collect();
        let original = encode_snapshots(None, None, PlayerId::AUTHORITY, frame, &states).unwrap();
        let packet = decode(&original[0]).unwrap();
        let Message::Snapshot(view) = packet.message else { panic!() };
        // Re-base onto a later frame, as the relay does when it merges records.
        let later = frame + rng.below(1000);
        let target = player(&mut rng);
        let mut builder = SnapshotBuilder::new(Some(target), Lease::new(7), PlayerId::AUTHORITY, later);
        for record in view.records() {
            assert!(builder.push_record(&record).unwrap());
        }
        let repacked = builder.finish();
        let packet = decode(&repacked).unwrap();
        assert_eq!(packet.header.player, Some(target));
        let Message::Snapshot(view2) = packet.message else { panic!() };
        assert_eq!(view2.frame, later);
        let a: Vec<ObjectState> = view.records().map(|r| *r.state()).collect();
        let b: Vec<ObjectState> = view2.records().map(|r| *r.state()).collect();
        assert_eq!(a, b);
    }
}

#[test]
fn record_frames_after_the_snapshot_frame_are_rejected_at_encode() {
    let mut rng = Rng::new(9);
    let mut s = state(&mut rng, 100);
    s.frame = 101;
    assert_eq!(
        encode_snapshots(None, None, PlayerId::AUTHORITY, 100, &[s]),
        Err(EncodeError::Invalid("record frame after snapshot frame"))
    );
    s.frame = 0;
    assert_eq!(
        encode_snapshots(None, None, PlayerId::AUTHORITY, u64::MAX, &[s]),
        Err(EncodeError::Invalid("record age"))
    );
}

#[test]
fn quantisation_error_is_at_most_half_a_step() {
    let mut rng = Rng::new(0x5EED_0004);
    for _ in 0..20_000 {
        let p = [0; 3].map(|_: i32| rng.f64_in(-quant::POSITION_LIMIT_M, quant::POSITION_LIMIT_M));
        let back = quant::position_m(quant::position(p).unwrap());
        for (a, b) in p.iter().zip(back) {
            assert!((a - b).abs() <= 0.5 / quant::POSITION_SCALE + 1e-9);
        }
        let v = [0; 3].map(|_: i32| rng.f64_in(-quant::VELOCITY_LIMIT_MPS, quant::VELOCITY_LIMIT_MPS));
        let back = quant::velocity_mps(quant::velocity(v).unwrap());
        for (a, b) in v.iter().zip(back) {
            assert!((a - b).abs() <= 0.5 / quant::VELOCITY_SCALE + 1e-9);
        }
        let w = [0; 3].map(|_: i32| rng.f64_in(-quant::ANGULAR_LIMIT_RPS, quant::ANGULAR_LIMIT_RPS));
        let back = quant::angular_rps(quant::angular(w).unwrap());
        for (a, b) in w.iter().zip(back) {
            assert!((a - b).abs() <= 0.5 / quant::ANGULAR_SCALE + 1e-9);
        }
        let r = [0; 3].map(|_: i32| rng.f64_in(-std::f64::consts::PI, std::f64::consts::PI - 1e-3));
        let back = quant::angles_rad(quant::angles(r).unwrap());
        for (a, b) in r.iter().zip(back) {
            assert!((a - b).abs() <= 0.5 / quant::ANGLE_SCALE + 1e-9, "{a} {b}");
        }
    }
}

#[test]
fn documented_message_sizes() {
    let token = Token([1; 16]);
    let p = PlayerId::human(1).unwrap();
    let id = ObjectId::new(5).unwrap();
    let sec = SectorId::new(3).unwrap();
    let size = |m: &Message| encode(Some(p), Lease::new(9), m).unwrap().len();
    let hello = Hello {
        token,
        nonce: [2; 16],
        role: Role::Player,
        want_player: None,
        resume_lease: None,
        capabilities: Capabilities::SUPPORTED,
        build: Label::new("world1-test").unwrap(),
        world: Label::new("seed").unwrap(),
    };
    assert_eq!(size(&Message::Hello(hello)), 67 + 11 + 4);
    let welcome = Welcome {
        nonce: [2; 16],
        admission: Admission::Accepted { player: p, lease: Lease::new(9).unwrap() },
        capabilities: Capabilities::SUPPORTED,
        max_players: 6,
        relay_epoch: 1,
        relay_time_us: 2,
    };
    assert_eq!(size(&Message::Welcome(welcome)), 67);
    assert_eq!(size(&Message::Ping(Ping { seq: 1, time_us: 2 })), 30);
    assert_eq!(size(&Message::Pong(Pong { seq: 1, echo_time_us: 2, time_us: 3 })), 38);
    let sub = |n: u32| Subscribe {
        revision: 1,
        current: sec,
        own_ship: Some(id),
        target: None,
        all_map: false,
        sectors: (1..=n)
            .map(|s| SectorInterest { sector: SectorId::new(s).unwrap(), interest: Interest::Adjacent })
            .collect(),
    };
    assert_eq!(size(&Message::Subscribe(sub(0))), 44);
    assert_eq!(size(&Message::Subscribe(sub(32))), 44 + 5 * 32);
    let intent = |params: &[u8]| {
        encode(
            Some(p),
            None,
            &Message::Intent(Intent {
                client_seq: 1,
                player: p,
                kind: IntentKind::CARGO_DROP,
                object: Some(id),
                generation: 1,
                frame: 1,
                params,
            }),
        )
        .unwrap()
        .len()
    };
    assert_eq!(intent(&[]), 48);
    assert_eq!(intent(&[0; 512]), 48 + 512);
    let result = IntentResult { client_seq: 1, player: p, outcome: Outcome::Accepted, frame: 0 };
    assert_eq!(size(&Message::IntentResult(result)), 35);
    let event = |kind| Message::Event(Event { event_seq: 1, frame: 1, object: id, generation: 1, kind });
    assert_eq!(size(&event(EventKind::OwnerChange { from: Owner::Unowned, to: Owner::Player(p) })), 47);
    assert_eq!(size(&event(EventKind::Destroy { sector: sec })), 47);
    assert_eq!(size(&event(EventKind::SectorChange { from: sec, to: sec })), 51);
    let roster = |n: u16| Roster {
        revision: 1,
        authority_connected: true,
        players: (1..=n)
            .map(|i| RosterEntry {
                player: PlayerId::human(i).unwrap(),
                connected: true,
                rtt_ms: Some(1),
                jitter_ms: None,
            })
            .collect(),
    };
    assert_eq!(size(&Message::Roster(roster(0))), 24);
    assert_eq!(size(&Message::Roster(roster(6))), 24 + 7 * 6);
    assert_eq!(size(&Message::Bye(Bye { reason: 0 })), 19);
    // Snapshots: 29 bytes + 61 per record (71 with combat intent).
    let mut rng = Rng::new(1);
    let mut s = state(&mut rng, 10);
    s.combat = None;
    let one = encode_snapshots(None, None, p, 10, &[s]).unwrap();
    assert_eq!(one[0].len(), HEADER_LEN + SNAPSHOT_HEAD_LEN + RECORD_BASE_LEN);
    s.combat = Some(CombatIntent { target: Some(id), firing: 1, manoeuvre: Manoeuvre::AttackRun });
    let one = encode_snapshots(None, None, p, 10, &[s]).unwrap();
    assert_eq!(one[0].len(), HEADER_LEN + SNAPSHOT_HEAD_LEN + RECORD_COMBAT_LEN);
    // Records per datagram.
    assert_eq!((MAX_DATAGRAM - HEADER_LEN - SNAPSHOT_HEAD_LEN) / RECORD_BASE_LEN, 19);
    assert_eq!((MAX_DATAGRAM - HEADER_LEN - SNAPSHOT_HEAD_LEN) / RECORD_COMBAT_LEN, 16);
}

#[test]
fn versioning_rules() {
    let ping = encode(None, None, &Message::Ping(Ping { seq: 7, time_us: 8 })).unwrap();
    // Trailing bytes and flags: rejected at our minor, ignored from a newer minor.
    let mut extended = ping.clone();
    extended.extend_from_slice(&[1, 2, 3]);
    let body = (extended.len() - HEADER_LEN) as u16;
    extended[6..8].copy_from_slice(&body.to_le_bytes());
    assert_eq!(decode(&extended), Err(DecodeError::TrailingBytes));
    extended[3] = MINOR + 1;
    extended[5] = 0x80;
    let packet = decode(&extended).unwrap();
    assert_eq!(packet.message, Message::Ping(Ping { seq: 7, time_us: 8 }));
    let mut flagged = ping.clone();
    flagged[5] = 1;
    assert_eq!(decode(&flagged), Err(DecodeError::ReservedBits));
    // Another major, an unknown kind, a version-1 text datagram.
    let mut major = ping.clone();
    major[2] = 3;
    assert_eq!(decode(&major), Err(DecodeError::UnsupportedMajor(3)));
    let mut kind = ping.clone();
    kind[4] = 200;
    assert_eq!(decode(&kind), Err(DecodeError::UnknownKind(200)));
    assert_eq!(decode(b"HELLO|1|aaaa"), Err(DecodeError::BadMagic));
    // Snapshot records may grow in a newer minor; older decoders skip the extension.
    let mut rng = Rng::new(3);
    let mut s = state(&mut rng, 50);
    s.combat = None;
    let snap = encode_snapshots(None, None, PlayerId::AUTHORITY, 50, &[s]).unwrap().remove(0);
    let mut grown = snap.clone();
    grown[HEADER_LEN + SNAPSHOT_HEAD_LEN] = (RECORD_BASE_LEN + 2) as u8;
    grown.extend_from_slice(&[0xAA, 0xBB]);
    let body = (grown.len() - HEADER_LEN) as u16;
    grown[6..8].copy_from_slice(&body.to_le_bytes());
    assert_eq!(decode(&grown), Err(DecodeError::TrailingBytes));
    grown[3] = MINOR + 1;
    let packet = decode(&grown).unwrap();
    let Message::Snapshot(view) = packet.message else { panic!() };
    let record = view.records().next().unwrap();
    assert_eq!(*record.state(), s);
    assert_eq!(record.raw().len(), RECORD_BASE_LEN + 2);
}
