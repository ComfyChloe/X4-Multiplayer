//! Pose matrices (`Math::PosRot`) and the exact single-precision arithmetic that X4 9.00
//! (Steam build 23660954) uses to compose them.
//!
//! # Layout
//!
//! 16 x `f32`, 64 bytes, 16-byte aligned, row-major. `rows[0]` is the translation
//! (x, y, z, w); `rows[1..=3]` are the basis vectors. A point `p` maps to
//! `p.x * rows[1] + p.y * rows[2] + p.z * rows[3] + rows[0]`. The identity has a zero
//! translation row and the unit basis rows `(1,0,0,0)`, `(0,1,0,0)`, `(0,0,1,0)`
//! (the constants at RVA 0x22c01d0 / 0x22c01e0 / 0x22c01f0).
//!
//! # Exactness
//!
//! The engine evaluates every lane with packed `mulps` / `addps` / `subps`: one rounding
//! per operation, round to nearest even, no FMA. The MoveWorker thread runs with the
//! default MXCSR (no flush-to-zero, no denormals-are-zero; only the main thread sets FTZ in
//! 0x14dd0e0), which is also Rust's default, so subnormal values are handled identically.
//! IEEE addition and multiplication are commutative, so bit-identity only depends on the
//! association of the additions, which each function documents. The fourth lane (w) is
//! computed exactly like the engine does, including its broadcast pattern, even though
//! engine poses keep it at zero.
//!
//! Not covered: NaN payloads. A NaN input gives a NaN output, but which payload survives
//! depends on the operand order of each instruction, which neither Rust nor LLVM promise to
//! keep. Signed zeros are reproduced (commutativity holds for them).

/// A pose matrix exactly as X4 stores it (`Math::PosRot`, 64 bytes).
#[derive(Clone, Copy, Debug, PartialEq)]
#[repr(C, align(16))]
pub struct PosRot {
    /// `rows[0]` = translation, `rows[1..=3]` = basis vectors.
    pub rows: [[f32; 4]; 4],
}

impl PosRot {
    /// The engine's identity pose (zero translation, unit basis).
    pub const IDENTITY: PosRot =
        PosRot { rows: [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]] };

    /// Builds a pose from 16 floats in memory order.
    pub fn from_array(v: [f32; 16]) -> PosRot {
        let mut rows = [[0.0; 4]; 4];
        for (k, row) in rows.iter_mut().enumerate() {
            row.copy_from_slice(&v[k * 4..k * 4 + 4]);
        }
        PosRot { rows }
    }

    /// The 16 floats in memory order.
    pub fn to_array(&self) -> [f32; 16] {
        let mut v = [0.0; 16];
        for (k, row) in self.rows.iter().enumerate() {
            v[k * 4..k * 4 + 4].copy_from_slice(row);
        }
        v
    }

    /// The raw IEEE bits in memory order (for bit-exact comparison and hashing).
    pub fn to_bits(&self) -> [u32; 16] {
        self.to_array().map(f32::to_bits)
    }

    /// Builds a pose from raw IEEE bits in memory order.
    pub fn from_bits(b: [u32; 16]) -> PosRot {
        PosRot::from_array(b.map(f32::from_bits))
    }

    /// Bitwise equality (distinguishes +0/-0 and compares NaNs by payload).
    pub fn bit_eq(&self, other: &PosRot) -> bool {
        self.to_bits() == other.to_bits()
    }
}

impl Default for PosRot {
    fn default() -> PosRot {
        PosRot::IDENTITY
    }
}

/// `mat := mat` expressed through `local`: every row of `mat` is mapped by `local`, the
/// translation row as a point and the basis rows as vectors.
///
/// Reproduces `U::Positional::GetLocalTransform` (vtable slot 648, 0x7461c0), non-reset
/// path, instructions 0x74626e..0x746320, which composes the caller's matrix with the
/// local transform `L` it obtained from the movement controller (`+0x200` vfunc +0x30) after
/// the parent's child-frame transform (parent vfunc +0x1520, slot 676).
///
/// Per lane `i`, with `L = [t, r1, r2, r3]`:
/// - translation row `p`: `((p.y*r2[i] + p.x*r1[i]) + p.z*r3[i]) + t[i]`, where lane 3
///   uses `p.w` in place of x, y and z (`shufps` 0xc0 / 0xd5 / 0xea);
/// - basis row `v`: `(v.y*r2[i] + v.x*r1[i]) + v.z*r3[i]` (broadcasts 0x00 / 0x55 / 0xaa).
pub fn compose_onto(mat: &mut PosRot, local: &PosRot) {
    let [t, r1, r2, r3] = local.rows;
    let p = mat.rows[0];
    let mut row0 = [0.0f32; 4];
    for i in 0..4 {
        let (x, y, z) = if i < 3 { (p[0], p[1], p[2]) } else { (p[3], p[3], p[3]) };
        row0[i] = ((y * r2[i] + x * r1[i]) + z * r3[i]) + t[i];
    }
    mat.rows[0] = row0;
    for k in 1..4 {
        let v = mat.rows[k];
        let mut row = [0.0f32; 4];
        for i in 0..4 {
            row[i] = (v[1] * r2[i] + v[0] * r1[i]) + v[2] * r3[i];
        }
        mat.rows[k] = row;
    }
}

/// `mat := mat` expressed in the frame `frame`, i.e. multiplied by the inverse of an
/// orthonormal `frame`: subtract the frame's translation, then project onto its basis
/// (multiply by the transposed basis).
///
/// Reproduces `U::Component::GetTransformRelativeTo` (0x3db8a0), instructions
/// 0x3dbba5..0x3dbc8f, used when the target is a descendant of the common ancestor.
/// With `frame = [t, a, b, c]` the transposed basis is `X = (a.x, b.x, c.x, c.w)`,
/// `Y = (a.y, b.y, c.y, c.w)`, `Z = (a.z, b.z, c.z, c.w)` (`unpcklps`/`unpckhps`/`shufps`
/// 0xc4 / 0xde / 0xe4). Per lane `i`:
/// - translation row: `q = mat[0] - t` (packed), then `(q.x*X[i] + q.y*Y[i]) + q.z*Z[i]`,
///   lane 3 using `q.w` for all three;
/// - basis row `v`: `(v.x*X[i] + v.y*Y[i]) + v.z*Z[i]`.
pub fn express_in_frame(mat: &mut PosRot, frame: &PosRot) {
    let [t, a, b, c] = frame.rows;
    let xs = [a[0], b[0], c[0], c[3]];
    let ys = [a[1], b[1], c[1], c[3]];
    let zs = [a[2], b[2], c[2], c[3]];
    let m0 = mat.rows[0];
    let q = [m0[0] - t[0], m0[1] - t[1], m0[2] - t[2], m0[3] - t[3]];
    let mut row0 = [0.0f32; 4];
    for i in 0..4 {
        let (x, y, z) = if i < 3 { (q[0], q[1], q[2]) } else { (q[3], q[3], q[3]) };
        row0[i] = z * zs[i] + (x * xs[i] + y * ys[i]);
    }
    mat.rows[0] = row0;
    for k in 1..4 {
        let v = mat.rows[k];
        let mut row = [0.0f32; 4];
        for i in 0..4 {
            row[i] = (v[0] * xs[i] + v[1] * ys[i]) + v[2] * zs[i];
        }
        mat.rows[k] = row;
    }
}

/// A component hierarchy that can answer the two questions
/// `GetTransformRelativeTo` asks of it.
pub trait PoseTree {
    /// Component handle.
    type Node: Copy + PartialEq;

    /// The live parent (`Component+0x70`).
    fn parent(&self, n: Self::Node) -> Option<Self::Node>;

    /// `vfunc648(n, mat, time, reset = false, include_parent = true)`: compose `n`'s local
    /// transform onto `mat`. For a positional with a movement controller and a parent this
    /// is [`compose_onto`] with the controller transform after the parent's child-frame
    /// transform; components without a controller or without a parent take class-specific
    /// paths that the implementor supplies.
    fn compose_local(&self, n: Self::Node, mat: &mut PosRot);
}

/// How [`transform_relative_to`] ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Relative {
    /// `target == node`: `mat` is unchanged.
    SameNode,
    /// `target` was absent or `node`'s parent: `node`'s local transform was composed once.
    Local,
    /// `target` is an ancestor of `node`: the chain up to it was composed.
    Ancestor,
    /// `target` is in another branch: the chain up to the common ancestor was composed,
    /// then the result was expressed in `target`'s frame.
    Branch,
    /// No common ancestor: every local up to the root was composed (the engine returns
    /// the same partial result without logging).
    Disjoint,
}

/// `U::Component::GetTransformRelativeTo(node, mat, time, target, false)` (0x3db8a0):
/// composes `node`'s pose onto `mat` and expresses it relative to `target`.
///
/// Control flow, as in the engine: `target == node` returns at once; an absent target or
/// the parent composes `node`'s local once. Otherwise the target's ancestor chain
/// (target first) is collected; walking up from `node`, every component that is not in
/// that chain composes its local onto `mat`. At the first one that is (the common
/// ancestor `A`), unless `A` is the target itself, the target's transform relative to `A`
/// is built from the identity by composing the target's local, its parent's, ... up to but
/// excluding `A`, and `mat` is expressed in that frame ([`express_in_frame`]).
/// The `include_attachments` variant (fifth argument true, list built by 0x469420) is not
/// modelled.
pub fn transform_relative_to<T: PoseTree>(
    tree: &T,
    node: T::Node,
    mat: &mut PosRot,
    target: Option<T::Node>,
) -> Relative {
    if target == Some(node) {
        return Relative::SameNode;
    }
    let target = match target {
        Some(t) if Some(t) != tree.parent(node) => t,
        _ => {
            tree.compose_local(node, mat);
            return Relative::Local;
        }
    };
    let mut chain = Vec::new();
    let mut c = Some(target);
    while let Some(n) = c {
        chain.push(n);
        c = tree.parent(n);
    }
    let mut cur = node;
    loop {
        if chain.contains(&cur) {
            if cur == target {
                return Relative::Ancestor;
            }
            let mut frame = PosRot::IDENTITY;
            let mut c = target;
            loop {
                tree.compose_local(c, &mut frame);
                c = match tree.parent(c) {
                    Some(p) => p,
                    None => break,
                };
                if c == cur {
                    break;
                }
            }
            express_in_frame(mat, &frame);
            return Relative::Branch;
        }
        tree.compose_local(cur, mat);
        cur = match tree.parent(cur) {
            Some(p) => p,
            None => return Relative::Disjoint,
        };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rot_z(angle: f32, t: [f32; 3]) -> PosRot {
        let (s, c) = angle.sin_cos();
        PosRot { rows: [[t[0], t[1], t[2], 0.0], [c, s, 0.0, 0.0], [-s, c, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]] }
    }

    #[test]
    fn identity_composition_keeps_finite_poses() {
        let m = rot_z(0.3, [1.0, 2.0, 3.0]);
        let mut a = m;
        compose_onto(&mut a, &PosRot::IDENTITY);
        assert!(a.bit_eq(&m));
        let mut b = PosRot::IDENTITY;
        compose_onto(&mut b, &m);
        assert!(b.bit_eq(&m));
    }

    #[test]
    fn negative_zero_translation_is_not_preserved_through_identity() {
        // The engine adds the products first, so a -0.0 translation of the local becomes
        // +0.0 when composed onto the identity: (0*r + 0*r) + 0*r = +0, +0 + -0 = +0.
        let mut local = PosRot::IDENTITY;
        local.rows[0][1] = -0.0;
        let mut m = PosRot::IDENTITY;
        compose_onto(&mut m, &local);
        assert_eq!(m.rows[0][1].to_bits(), 0.0f32.to_bits());
    }

    #[test]
    fn composition_maps_points() {
        // Child at (1,0,0) in a parent rotated 90 degrees about z and moved by (10,0,0).
        let parent = rot_z(std::f32::consts::FRAC_PI_2, [10.0, 0.0, 0.0]);
        let mut child = PosRot::IDENTITY;
        child.rows[0] = [1.0, 0.0, 0.0, 0.0];
        compose_onto(&mut child, &parent);
        assert!((child.rows[0][0] - 10.0).abs() < 1e-6);
        assert!((child.rows[0][1] - 1.0).abs() < 1e-6);
    }

    #[test]
    fn express_in_frame_inverts_composition_for_orthonormal_frames() {
        let frame = rot_z(0.7, [3.0, -4.0, 5.0]);
        let original = rot_z(-1.1, [0.5, 0.25, -2.0]);
        let mut m = original;
        compose_onto(&mut m, &frame);
        express_in_frame(&mut m, &frame);
        for (a, b) in m.to_array().iter().zip(original.to_array()) {
            assert!((a - b).abs() < 1e-5, "{a} vs {b}");
        }
    }

    struct Chain(Vec<(Option<usize>, PosRot)>);
    impl PoseTree for Chain {
        type Node = usize;
        fn parent(&self, n: usize) -> Option<usize> {
            self.0[n].0
        }
        fn compose_local(&self, n: usize, mat: &mut PosRot) {
            compose_onto(mat, &self.0[n].1);
        }
    }

    #[test]
    fn relative_control_flow() {
        // 0 root, 1 child of 0, 2 child of 1, 3 child of 0
        let t = Chain(vec![
            (None, rot_z(0.1, [1.0, 0.0, 0.0])),
            (Some(0), rot_z(0.2, [0.0, 1.0, 0.0])),
            (Some(1), rot_z(0.3, [0.0, 0.0, 1.0])),
            (Some(0), rot_z(0.4, [2.0, 2.0, 0.0])),
        ]);
        let mut m = PosRot::IDENTITY;
        assert_eq!(transform_relative_to(&t, 2, &mut m, Some(2)), Relative::SameNode);
        assert!(m.bit_eq(&PosRot::IDENTITY));
        assert_eq!(transform_relative_to(&t, 2, &mut m, Some(1)), Relative::Local);
        assert_eq!(transform_relative_to(&t, 2, &mut PosRot::IDENTITY.clone(), None), Relative::Local);
        let mut m = PosRot::IDENTITY;
        assert_eq!(transform_relative_to(&t, 2, &mut m, Some(0)), Relative::Ancestor);
        let mut expect = PosRot::IDENTITY;
        compose_onto(&mut expect, &t.0[2].1);
        compose_onto(&mut expect, &t.0[1].1);
        assert!(m.bit_eq(&expect));
        let mut m = PosRot::IDENTITY;
        assert_eq!(transform_relative_to(&t, 2, &mut m, Some(3)), Relative::Branch);
        let mut frame = PosRot::IDENTITY;
        compose_onto(&mut frame, &t.0[3].1);
        express_in_frame(&mut expect, &frame);
        assert!(m.bit_eq(&expect));
    }
}
