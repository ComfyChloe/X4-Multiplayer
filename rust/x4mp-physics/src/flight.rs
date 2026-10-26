//! Flight control models of X4's pathing agents (`U::Path::*FlightControlModel`), reimplemented with
//! the original operation order (Windows build 23660954, static analysis of the pinned X4.exe).
//!
//! The pathing pass calls, per agent, `e8f250` -> `model->Update(slot(W), dt, t, cheap, P(owner))`
//! (vtable slot +0x70). `slot(W)` is the agent's write-side pathing slot (pose, velocities, scalars).
//! Every kernel here is a pure function of the model fields, the slot and `dt`; the CRT functions the
//! original calls (`atan2f` 0x1a1a398, `asinf` 0x1b43ce0, statically linked) come in through [`CrtMath`]
//! so a caller can use X4's own implementations (bit-identical) or the platform's.

/// The CRT math functions the flight models call.
pub trait CrtMath {
    fn atan2f(&self, y: f32, x: f32) -> f32;
    fn asinf(&self, x: f32) -> f32;
}

/// The platform's implementations (Rust std: the C runtime on Windows MSVC, libm on Linux). Whether they
/// equal X4's statically linked ones is measured by the oracle tests, not assumed.
pub struct StdMath;

impl CrtMath for StdMath {
    fn atan2f(&self, y: f32, x: f32) -> f32 {
        y.atan2(x)
    }
    fn asinf(&self, x: f32) -> f32 {
        x.asin()
    }
}

/// X4's statically linked `asinf` (0x1b43ce0), ported with its operation order: a rational
/// approximation in float (|x| < 0.5: x + x*u; 0.5 <= |x| < 1: the split-sqrt form around pi/4), x itself
/// below 2^-14, +-pi/2 at +-1. The CRT's error paths (NaN, |x| > 1) return a quiet NaN here instead of
/// reporting a domain error; the flight models clamp to [-1, 1] and never reach them. The platform's
/// asinf differs from this one in ~0.9 % of inputs (oracle test std_math_vs_x4_crt).
pub fn x4_asinf(x: f32) -> f32 {
    const A: u32 = 0xbc5b_3fe1; // -0.013381929
    const B: u32 = 0x3b81_ce6b; // 0.0039613745
    const C4: u32 = 0x3d67_8bdd; // 0.056529868
    const C5: u32 = 0x3e3c_94dc; // 0.18416160
    const Q0: u32 = 0x3f8d_6fa5; // 1.1049696
    const Q1: u32 = 0x3f56_1f0d; // 0.83641130
    const PIO2_LO: u32 = 0x33a2_2168; // 7.5497894e-08
    const PIO4_HI: u32 = 0x3f49_0fda; // 0.78539813
    let f = f32::from_bits;
    let bits = x.to_bits();
    let exp = (bits >> 23) & 0xff;
    if bits & 0x7fff_ffff > 0x7f80_0000 {
        return f32::from_bits(bits | 0x0040_0000); // NaN: the CRT quiets it (error path)
    }
    if exp < 0x71 {
        return x; // |x| < 2^-14 (raises inexact only)
    }
    if exp >= 0x7f {
        if x == 1.0 {
            return f(0x3fc9_0fdb);
        }
        if x == -1.0 {
            return f(0xbfc9_0fdb);
        }
        return f(0xffc0_0000); // domain error
    }
    let ax = f(bits & 0x7fff_ffff);
    let big = exp >= 0x7e;
    let (r, s) = if big {
        let r = (1.0 - ax) * 0.5;
        (r, r.sqrt())
    } else {
        (ax * ax, 0.0)
    };
    let mut p = f(A) - r * f(B);
    p *= r;
    p -= f(C4);
    p *= r;
    p += f(C5);
    p *= r;
    let q = f(Q0) - r * f(Q1);
    let u = p / q;
    let result = if !big {
        u * ax + ax
    } else {
        let hi = f(s.to_bits() & 0xffff_0000);
        let c = (r - hi * hi) / (hi + s);
        let two_su = (s + s) * u;
        let t = two_su - (f(PIO2_LO) - (c + c));
        f(PIO4_HI) - (t - (f(PIO4_HI) - hi * 2.0))
    };
    if (bits as i32) < 0 {
        -result
    } else {
        result
    }
}

/// X4's math without X4: [`x4_asinf`] and the platform's `atan2f`, which equals X4's statically linked
/// `atan2f` on Windows (oracle test, 2 M inputs); on other platforms that equality is unverified.
pub struct X4Math;

impl CrtMath for X4Math {
    fn atan2f(&self, y: f32, x: f32) -> f32 {
        y.atan2(x)
    }
    fn asinf(&self, x: f32) -> f32 {
        x4_asinf(x)
    }
}

/// A 4-lane f32 vector as the original keeps it in an XMM register (`w` lane included).
pub type V4 = [f32; 4];

/// The part of the pathing slot (0x1a0 bytes) the flight models touch, at the slot's offsets.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
#[repr(C, align(16))]
pub struct Slot {
    /// +0x00 position.
    pub pos: V4,
    /// +0x10, +0x20, +0x30 orientation rows.
    pub rot: [V4; 3],
    /// +0x40 velocity.
    pub vel: V4,
    /// +0x50 angular term (the forced model stores negated Euler angles here, not divided by dt).
    pub ang: V4,
    /// +0x60 local velocity target (forced: (0, 0, max forward speed, 0)).
    pub v60: V4,
    /// +0x70.
    pub v70: V4,
    /// +0x80 forward-speed ratio, +0x84 throttle target, +0x88, +0x8c.
    pub s80: [f32; 4],
}

impl Slot {
    /// Byte size of the leading part described by this struct.
    pub const BYTES: usize = 0x90;

    /// Read from the first [`Slot::BYTES`] bytes of a slot image.
    pub fn from_bytes(b: &[u8]) -> Slot {
        let f = |at: usize| f32::from_le_bytes(b[at..at + 4].try_into().unwrap());
        let v = |at: usize| [f(at), f(at + 4), f(at + 8), f(at + 12)];
        Slot {
            pos: v(0x00),
            rot: [v(0x10), v(0x20), v(0x30)],
            vel: v(0x40),
            ang: v(0x50),
            v60: v(0x60),
            v70: v(0x70),
            s80: v(0x80),
        }
    }

    /// Write into the first [`Slot::BYTES`] bytes of a slot image.
    pub fn write_bytes(&self, b: &mut [u8]) {
        let mut put = |at: usize, x: &V4| {
            for (i, lane) in x.iter().enumerate() {
                b[at + 4 * i..at + 4 * i + 4].copy_from_slice(&lane.to_le_bytes());
            }
        };
        put(0x00, &self.pos);
        put(0x10, &self.rot[0]);
        put(0x20, &self.rot[1]);
        put(0x30, &self.rot[2]);
        put(0x40, &self.vel);
        put(0x50, &self.ang);
        put(0x60, &self.v60);
        put(0x70, &self.v70);
        put(0x80, &self.s80);
    }
}

/// `U::Path::ForcedFlightControlModel` (vtable 0x2c4f7d0, Update 0xeb04c0): the agent is put on a target
/// pose every pass. Model fields: +0x10 owning agent (its +0x364 = maximum forward speed),
/// +0x20 target position, +0x30/+0x40/+0x50 target orientation rows.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct ForcedModel {
    pub target_pos: V4,
    pub target_rot: [V4; 3],
}

fn add(a: V4, b: V4) -> V4 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3]]
}
fn mul_s(s: f32, a: V4) -> V4 {
    [s * a[0], s * a[1], s * a[2], s * a[3]]
}

/// One forced update (`dt` = the pass's dt, `max_forward` = agent+0x364). `t`, the cheap flag and P(owner)
/// are passed by the caller but not used by this model. Writes exactly the fields the original writes:
/// vel, ang, v60, v70, s80[1] (throttle target = 1), then pos and rot.
pub fn forced_update(model: &ForcedModel, slot: &mut Slot, dt: f32, max_forward: f32, math: &impl CrtMath) {
    // vel = (target - pos) / dt, all four lanes (subps, divps by the broadcast dt).
    let mut vel = [0f32; 4];
    for i in 0..4 {
        vel[i] = (model.target_pos[i] - slot.pos[i]) / dt;
    }
    slot.vel = vel;
    // Columns of the current orientation (the w lane of each column is row 2's w).
    let (r0, r1, r2) = (slot.rot[0], slot.rot[1], slot.rot[2]);
    let c0 = [r0[0], r1[0], r2[0], r2[3]];
    let c1 = [r0[1], r1[1], r2[1], r2[3]];
    let c2 = [r0[2], r1[2], r2[2], r2[3]];
    // m_k = (t_k.y * c1 + t_k.x * c0) + t_k.z * c2: relative orientation target * current^T.
    let row = |t: V4| add(add(mul_s(t[1], c1), mul_s(t[0], c0)), mul_s(t[2], c2));
    let m0 = row(model.target_rot[0]);
    let m1 = row(model.target_rot[1]);
    let m2 = row(model.target_rot[2]);
    let a = math.atan2f(-m0[1], m1[1]);
    let b = math.atan2f(-m2[0], m2[2]);
    // comiss m2.y >= -1 ? m2.y : -1 (NaN -> -1), then minss with 1.
    let mut s = if m2[1] >= -1.0 { m2[1] } else { -1.0 };
    s = if s < 1.0 { s } else { 1.0 };
    let c = math.asinf(s);
    slot.ang = [-c, -b, -a, 0.0];
    slot.s80[1] = 1.0;
    slot.v60 = [0.0, 0.0, max_forward, 0.0];
    slot.v70 = [0.0; 4];
    slot.pos = model.target_pos;
    slot.rot = model.target_rot;
}
