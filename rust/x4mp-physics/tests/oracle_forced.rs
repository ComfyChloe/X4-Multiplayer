//! Differential test: `flight::forced_update` against the original `ForcedFlightControlModel::Update`
//! (0xeb04c0) executed from the mapped pinned X4.exe. OFFLINE evidence (original machine code,
//! synthetic inputs), not a native result.
#![cfg(all(windows, target_arch = "x86_64"))]

use x4mp_physics::flight::{forced_update, x4_asinf, CrtMath, ForcedModel, Slot, StdMath, X4Math, V4};
use x4mp_physics::oracle::{default_exe, Image, MappedMath, FORCED_UPDATE_HEAD};

fn image() -> Option<Image> {
    let path = default_exe();
    if !path.exists() {
        eprintln!("SKIP: {} not found (set X4MP_X4_EXE)", path.display());
        return None;
    }
    Some(Image::load(&path).expect("pinned image"))
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn unit(&mut self) -> f32 {
        (self.next() >> 40) as f32 / (1u64 << 24) as f32
    }
    fn range(&mut self, lo: f32, hi: f32) -> f32 {
        lo + (hi - lo) * self.unit()
    }
    /// Any finite f32 bit pattern (subnormals and signed zeros included).
    fn finite(&mut self) -> f32 {
        loop {
            let v = f32::from_bits(self.next() as u32);
            if v.is_finite() {
                return v;
            }
        }
    }
}

/// Rotation rows from a random unit quaternion (w lane 0 like the engine's matrices).
fn rotation(r: &mut Rng) -> [V4; 3] {
    let (mut x, mut y, mut z, mut w) = (r.range(-1.0, 1.0), r.range(-1.0, 1.0), r.range(-1.0, 1.0), r.range(-1.0, 1.0));
    let n = (x * x + y * y + z * z + w * w).sqrt().max(1e-6);
    x /= n;
    y /= n;
    z /= n;
    w /= n;
    [
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y + z * w), 2.0 * (x * z - y * w), 0.0],
        [2.0 * (x * y - z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z + x * w), 0.0],
        [2.0 * (x * z + y * w), 2.0 * (y * z - x * w), 1.0 - 2.0 * (x * x + y * y), 0.0],
    ]
}

fn model_bytes(m: &ForcedModel) -> [u8; 0x60] {
    let mut b = [0u8; 0x60];
    let vs = [m.target_pos, m.target_rot[0], m.target_rot[1], m.target_rot[2]];
    for (k, v) in vs.iter().enumerate() {
        for (i, lane) in v.iter().enumerate() {
            let at = 0x20 + 0x10 * k + 4 * i;
            b[at..at + 4].copy_from_slice(&lane.to_le_bytes());
        }
    }
    b
}

fn slot_bytes(s: &Slot, filler: u8) -> [u8; 0x1a0] {
    let mut b = [filler; 0x1a0];
    s.write_bytes(&mut b);
    b
}

/// Run both and compare the whole 0x1a0-byte slot image bitwise.
fn compare(image: &Image, math: &impl CrtMath, m: &ForcedModel, s: &Slot, dt: f32, vfwd: f32, filler: u8) -> bool {
    let before = slot_bytes(s, filler);
    let theirs = image.call_forced_update(&model_bytes(m), &before, dt, 1234.5, 0, 1, vfwd);
    let mut ours_slot = *s;
    forced_update(m, &mut ours_slot, dt, vfwd, math);
    let mut ours = before;
    ours_slot.write_bytes(&mut ours);
    if theirs != ours {
        let diff = (0..0x1a0).find(|&i| theirs[i] != ours[i]).unwrap();
        let at = diff & !3;
        eprintln!("mismatch at slot+{diff:#x}: theirs {:02x?} ours {:02x?} (dt {dt:e})", &theirs[at..at + 4], &ours[at..at + 4]);
        return false;
    }
    true
}

#[test]
fn forced_update_matches_the_original_bitwise() {
    let Some(image) = image() else { return };
    assert_eq!(image.sha256_at(0xeb04c0, 32), FORCED_UPDATE_HEAD);
    let math = MappedMath::new(&image);
    let mut r = Rng(0x9e37_79b9_7f4a_7c15);
    let mut cases = 0;
    // Realistic: unit rotations, positions up to 1e5 m, dt 1/1000..1/5 s.
    for _ in 0..200_000 {
        let m = ForcedModel {
            target_pos: [r.range(-1e5, 1e5), r.range(-1e5, 1e5), r.range(-1e5, 1e5), 0.0],
            target_rot: rotation(&mut r),
        };
        let s = Slot {
            pos: [r.range(-1e5, 1e5), r.range(-1e5, 1e5), r.range(-1e5, 1e5), 0.0],
            rot: rotation(&mut r),
            vel: [r.range(-500.0, 500.0); 4],
            ang: [r.range(-3.0, 3.0); 4],
            ..Slot::default()
        };
        let (dt, vfwd) = (r.range(0.001, 0.2), r.range(0.0, 1000.0));
        assert!(compare(&image, &math, &m, &s, dt, vfwd, 0xa5));
        cases += 1;
    }
    // Near-identity relative rotations (target close to the current pose) and the asin clamp boundary.
    for k in 0..100_000 {
        let rot = rotation(&mut r);
        let mut target = rot;
        for row in target.iter_mut() {
            for lane in row.iter_mut().take(3) {
                *lane += r.range(-1e-3, 1e-3) * (k % 7) as f32;
            }
        }
        let m = ForcedModel { target_pos: [r.range(-10.0, 10.0); 4], target_rot: target };
        let s = Slot { pos: [0.0; 4], rot, ..Slot::default() };
        assert!(compare(&image, &math, &m, &s, 1.0 / 60.0, 300.0, 0));
        cases += 1;
    }
    // Arbitrary finite bit patterns, skipping inputs whose atan2f argument would be NaN (the CRT's error
    // path is not callable offline).
    let mut skipped = 0;
    for _ in 0..200_000 {
        let mut v = || [r.finite(), r.finite(), r.finite(), r.finite()];
        let m = ForcedModel { target_pos: v(), target_rot: [v(), v(), v()] };
        let s = Slot { pos: v(), rot: [v(), v(), v()], ..Slot::default() };
        let dt = r.finite();
        let vfwd = r.finite();
        let (r0, r1, r2) = (s.rot[0], s.rot[1], s.rot[2]);
        let c0 = [r0[0], r1[0], r2[0], r2[3]];
        let c1 = [r0[1], r1[1], r2[1], r2[3]];
        let c2 = [r0[2], r1[2], r2[2], r2[3]];
        let lane = |t: V4, i: usize| (t[1] * c1[i] + t[0] * c0[i]) + t[2] * c2[i];
        let args = [
            lane(m.target_rot[0], 1),
            lane(m.target_rot[1], 1),
            lane(m.target_rot[2], 0),
            lane(m.target_rot[2], 2),
        ];
        if args.iter().any(|a| a.is_nan()) {
            skipped += 1;
            continue;
        }
        assert!(compare(&image, &math, &m, &s, dt, vfwd, 0x3c));
        cases += 1;
    }
    eprintln!("forced update: {cases} cases bit-identical, {skipped} skipped (NaN atan2 argument)");
}

/// Measured, not asserted: does the platform's atan2f/asinf (Rust std) equal X4's statically linked CRT?
#[test]
fn std_math_vs_x4_crt() {
    let Some(image) = image() else { return };
    let x4 = MappedMath::new(&image);
    let mut r = Rng(0x1234_5678_9abc_def1);
    let (mut atan2_diff, mut asin_diff) = (0u64, 0u64);
    let n = 2_000_000;
    for _ in 0..n {
        let (y, x) = (r.range(-2.0, 2.0), r.range(-2.0, 2.0));
        if StdMath.atan2f(y, x).to_bits() != x4.atan2f(y, x).to_bits() {
            atan2_diff += 1;
        }
        let a = r.range(-1.0, 1.0);
        if StdMath.asinf(a).to_bits() != x4.asinf(a).to_bits() {
            asin_diff += 1;
        }
    }
    eprintln!("std vs X4 CRT over {n} inputs: atan2f differs {atan2_diff}, asinf differs {asin_diff}");
    // Candidates for an X4-independent asinf: the double-precision asin rounded to f32.
    let mut via_f64 = 0u64;
    let mut exhaustive = 0u64;
    let mut checked = 0u64;
    for _ in 0..n {
        let a = r.range(-1.0, 1.0);
        if ((a as f64).asin() as f32).to_bits() != x4.asinf(a).to_bits() {
            via_f64 += 1;
        }
    }
    // Every f32 in [-1, 1] with a stride (covers all exponents, subnormals included).
    let mut bits = 0u32;
    while bits <= 0x3f80_0000 {
        for sign in [0u32, 0x8000_0000] {
            let a = f32::from_bits(bits | sign);
            checked += 1;
            if ((a as f64).asin() as f32).to_bits() != x4.asinf(a).to_bits() {
                exhaustive += 1;
            }
        }
        bits += 97;
    }
    eprintln!("(x as f64).asin() as f32 vs X4 asinf: {via_f64} of {n} random differ, {exhaustive} of {checked} strided differ");
}

/// X4's asinf, ported: every f32 in [-1, 1] (both signs, subnormals and zeros included) gives the
/// original's bits.
#[test]
fn ported_asinf_equals_x4_asinf_on_every_input_in_range() {
    let Some(image) = image() else { return };
    let x4 = MappedMath::new(&image);
    let mut checked = 0u64;
    let mut first: Option<u32> = None;
    let mut differ = 0u64;
    for sign in [0u32, 0x8000_0000] {
        let mut bits = 0u32;
        while bits <= 0x3f80_0000 {
            let a = f32::from_bits(bits | sign);
            if x4_asinf(a).to_bits() != x4.asinf(a).to_bits() {
                differ += 1;
                first.get_or_insert(bits | sign);
            }
            checked += 1;
            bits += 1;
        }
    }
    eprintln!("ported asinf: {checked} inputs, {differ} differ, first {first:x?}");
    assert_eq!(differ, 0);
}

/// The forced update with X4Math (no X4 code at all) equals the original executed with X4's CRT.
#[test]
fn forced_update_with_ported_math_matches_the_original() {
    let Some(image) = image() else { return };
    let mut r = Rng(0x0dd1_7ea5_e5b1_0b5f);
    for _ in 0..300_000 {
        let m = ForcedModel {
            target_pos: [r.range(-1e5, 1e5), r.range(-1e5, 1e5), r.range(-1e5, 1e5), 0.0],
            target_rot: rotation(&mut r),
        };
        let s = Slot { pos: [r.range(-1e5, 1e5), r.range(-1e5, 1e5), r.range(-1e5, 1e5), 0.0], rot: rotation(&mut r), ..Slot::default() };
        let (dt, vfwd) = (r.range(0.001, 0.2), r.range(0.0, 1000.0));
        assert!(compare(&image, &X4Math, &m, &s, dt, vfwd, 0x5a));
    }
}
