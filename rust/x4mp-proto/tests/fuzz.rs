//! Deterministic malformed-input fuzzing: decoding hostile bytes must never panic, and
//! anything that does decode must re-encode to exactly the same bytes (no ignored
//! garbage at the current minor version).

mod common;

use common::*;
use x4mp_proto::*;

fn fix_length(bytes: &mut [u8]) {
    if bytes.len() >= HEADER_LEN {
        let body = (bytes.len() - HEADER_LEN).min(usize::from(u16::MAX)) as u16;
        bytes[6..8].copy_from_slice(&body.to_le_bytes());
    }
}

fn mutate(rng: &mut Rng, bytes: &mut Vec<u8>) {
    for _ in 0..1 + rng.below(4) {
        match rng.below(8) {
            0 if !bytes.is_empty() => {
                let i = rng.below(bytes.len() as u64) as usize;
                bytes[i] ^= 1 << rng.below(8);
            }
            1 if !bytes.is_empty() => {
                let i = rng.below(bytes.len() as u64) as usize;
                bytes[i] = [0, 1, 0x7F, 0x80, 0xFF, rng.u8()][rng.below(6) as usize];
            }
            2 => {
                let n = rng.below(bytes.len() as u64 + 1) as usize;
                bytes.truncate(n);
            }
            3 => {
                let n = rng.below(80) as usize;
                let extra = rng.bytes(n);
                bytes.extend_from_slice(&extra);
            }
            4 if bytes.len() > 4 => {
                // Duplicate or drop an inner range (shifts every later field).
                let a = rng.below(bytes.len() as u64) as usize;
                let b = (a + rng.below(16) as usize).min(bytes.len());
                if rng.chance(50) {
                    let copy = bytes[a..b].to_vec();
                    bytes.splice(a..a, copy);
                } else {
                    bytes.drain(a..b);
                }
            }
            5 if bytes.len() > HEADER_LEN + 12 => {
                // Hit length/count fields in the body specifically.
                let i = HEADER_LEN + rng.below((bytes.len() - HEADER_LEN) as u64) as usize;
                bytes[i] = rng.u8();
            }
            _ => {}
        }
    }
    if rng.chance(70) {
        // Usually keep the header length consistent so the body parsers are reached.
        fix_length(bytes);
    }
}

fn check(bytes: &[u8]) -> bool {
    let _ = peek_header(bytes);
    let _ = retarget(bytes, PlayerId::human(1), Lease::new(1));
    match decode(bytes) {
        Err(_) => false,
        Ok(packet) => {
            if let Message::Snapshot(view) = &packet.message {
                // Re-packing hostile-but-valid records must not panic either.
                let mut builder = SnapshotBuilder::new(None, None, view.source, view.frame);
                for record in view.records() {
                    let _ = builder.push_record(&record);
                }
                let _ = builder.finish();
            }
            if packet.header.strict() {
                let again = encode(packet.header.player, packet.header.lease, &packet.message)
                    .expect("a decoded message re-encodes");
                assert_eq!(again, bytes, "decoded {:?} but re-encoded differently", packet.message.kind());
            }
            true
        }
    }
}

#[test]
fn mutated_datagrams_never_panic_and_accepted_ones_are_canonical() {
    let mut rng = Rng::new(0xF022_0001);
    let mut accepted = 0;
    let iterations = 200_000;
    for _ in 0..iterations {
        let mut bytes = datagram(&mut rng);
        mutate(&mut rng, &mut bytes);
        accepted += usize::from(check(&bytes));
    }
    // Sanity: the mutator is not so destructive that nothing ever parses.
    assert!(accepted > iterations / 50, "only {accepted} accepted");
}

#[test]
fn random_garbage_never_panics() {
    let mut rng = Rng::new(0xF022_0002);
    for _ in 0..100_000 {
        let n = rng.below(1300) as usize;
        let mut bytes = rng.bytes(n);
        if rng.chance(50) && bytes.len() >= 6 {
            bytes[..3].copy_from_slice(&[MAGIC[0], MAGIC[1], MAJOR]);
            bytes[3] = 0;
            bytes[4] = 1 + rng.below(11) as u8;
            bytes[5] = 0;
            fix_length(&mut bytes);
        }
        check(&bytes);
    }
}

#[test]
fn every_truncation_of_a_valid_datagram_is_rejected() {
    let mut rng = Rng::new(0xF022_0003);
    for _ in 0..500 {
        let bytes = datagram(&mut rng);
        assert!(decode(&bytes).is_ok());
        for n in 0..bytes.len() {
            let mut cut = bytes[..n].to_vec();
            assert!(decode(&cut).is_err(), "prefix {n} of {:?} accepted", bytes);
            // Even with a consistent header length, a cut body never decodes.
            fix_length(&mut cut);
            if n >= HEADER_LEN {
                let packet = decode(&cut);
                // A shorter body can only be valid if the message has a variable tail
                // that still parses; it must then be canonical.
                if let Ok(p) = packet {
                    assert_eq!(encode(p.header.player, p.header.lease, &p.message).unwrap(), cut);
                }
            }
        }
    }
}

#[test]
fn oversized_and_inconsistent_lengths_are_rejected() {
    let ping = encode(None, None, &Message::Ping(Ping { seq: 1, time_us: 2 })).unwrap();
    let mut big = ping.clone();
    big.resize(MAX_DATAGRAM + 1, 0);
    assert_eq!(decode(&big), Err(DecodeError::TooLong));
    let mut wrong = ping.clone();
    wrong[6] = wrong[6].wrapping_add(1);
    assert_eq!(decode(&wrong), Err(DecodeError::LengthMismatch));
    // A count larger than the records present.
    let mut rng = Rng::new(4);
    let s = state(&mut rng, 10);
    let mut snap = encode_snapshots(None, None, PlayerId::AUTHORITY, 10, &[s]).unwrap().remove(0);
    snap[HEADER_LEN + 10] = 2;
    assert_eq!(decode(&snap), Err(DecodeError::TooShort));
    snap[HEADER_LEN + 10] = 255;
    assert_eq!(decode(&snap), Err(DecodeError::TooShort));
}
