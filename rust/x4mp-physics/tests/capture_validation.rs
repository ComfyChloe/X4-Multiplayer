//! Capture acceptance must not turn missing native coverage or a malformed record into a pass.
use x4mp_physics::capture::{replay, write, FlightCapture};
use x4mp_physics::flight::X4Math;

fn fixture() -> Vec<u8> {
    write(0, 0x70, 0, &[
        ((0, 1.0 / 60.0, 100.0, 0.0, 1), vec![0; 0x70], vec![0; 0x60],
         vec![0; 0x1a0], vec![0; 0x1a0]),
    ])
}

fn field(file: &mut [u8], index: usize, value: u32) {
    file[8 + index * 4..12 + index * 4].copy_from_slice(&value.to_le_bytes());
}

#[test]
fn empty_native_capture_is_readable_but_never_equivalence_evidence() {
    let cap = FlightCapture::parse(write(0, 0x70, 0, &[])).unwrap();
    assert!(replay(&cap, &X4Math).unwrap_err().contains("no calls"));
}

#[test]
fn malformed_layout_is_rejected_without_overflow_or_replay_panic() {
    for (index, value) in [
        (2, 0), (2, u32::MAX - 15), (3, 0), (3, 0x5f), (3, u32::MAX),
        (4, 0x341), (5, 0), (5, u32::MAX), (7, u32::MAX),
    ] {
        let mut file = fixture();
        field(&mut file, index, value);
        assert!(FlightCapture::parse(file).is_err(), "accepted field {index}={value}");
    }
}

#[test]
fn each_record_must_match_its_declared_model_and_flags() {
    let mut wrong_model = fixture();
    wrong_model[56..60].copy_from_slice(&1u32.to_le_bytes());
    assert!(matches!(FlightCapture::parse(wrong_model), Err(e) if e.contains("model")));
    let mut wrong_flags = fixture();
    wrong_flags[60..64].copy_from_slice(&8u32.to_le_bytes());
    assert!(FlightCapture::parse(wrong_flags).is_err());
    assert!(FlightCapture::parse(fixture()).is_ok());
}
