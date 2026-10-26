//! Capture format round trip and replay: records produced by the original forced Update (oracle, mapped
//! pinned X4.exe) in the probe's file layout replay with 0 mismatches; a flipped bit is caught.
#![cfg(all(windows, target_arch = "x86_64"))]

use x4mp_physics::capture::{replay, write, FlightCapture};
use x4mp_physics::flight::X4Math;
use x4mp_physics::oracle::{default_exe, Image};

#[test]
fn oracle_capture_replays_exactly_and_a_flipped_bit_is_caught() {
    let path = default_exe();
    if !path.exists() {
        eprintln!("SKIP: {} not found", path.display());
        return;
    }
    let image = Image::load(&path).unwrap();
    let mut seed = 0x2545_f491_4f6c_dd1du64;
    let mut next = || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        seed
    };
    let mut records = Vec::new();
    for i in 0..2000 {
        let mut model = [0u8; 0x60];
        let mut slot = [0u8; 0x1a0];
        for chunk in model[0x20..].chunks_mut(4).chain(slot[..0x40].chunks_mut(4)) {
            let v = ((next() >> 40) as f32 / (1u64 << 24) as f32) * 2.0 - 1.0;
            chunk.copy_from_slice(&v.to_le_bytes());
        }
        for b in slot[0x40..].iter_mut() {
            *b = next() as u8;
        }
        let (dt, vfwd) = (1.0 / 60.0, 250.0 + i as f32);
        let after = image.call_forced_update(&model, &slot, dt, 100.0, 0, 1, vfwd);
        let mut agent = vec![0u8; 0x60];
        agent[0x24..0x28].copy_from_slice(&vfwd.to_le_bytes());
        records.push(((2u32, dt, vfwd, 100.0f64, i as u64), model.to_vec(), agent, slot.to_vec(), after.to_vec()));
    }
    let file = write(0, 0x60, 3, &records);
    let cap = FlightCapture::parse(file.clone()).unwrap();
    assert_eq!(cap.header.records, 2000);
    assert_eq!(cap.header.dropped, 3);
    let report = replay(&cap, &X4Math).unwrap();
    println!("{}", report.line("flight-forced"));
    assert_eq!((report.records, report.mismatched), (2000, 0));
    // Negative control: one bit of one record's "after" slot (velocity x of record 1234).
    let mut bad = file;
    let at = 56 + 1234 * cap.header.record_size as usize + 32 + 0x60 + 0x60 + 0x1a0 + 0x40;
    bad[at] ^= 1;
    let report = replay(&FlightCapture::parse(bad).unwrap(), &X4Math).unwrap();
    assert_eq!((report.mismatched, report.first), (1, Some((1234, 0x40))));
    // Wrong build or truncated file are refused.
    let mut wrong = write(0, 0x60, 0, &records[..1]);
    wrong[8 + 4 * 9] ^= 1;
    assert!(FlightCapture::parse(wrong).is_err());
    let mut short = write(0, 0x60, 0, &records[..2]);
    short.truncate(short.len() - 1);
    assert!(FlightCapture::parse(short).is_err());
}
