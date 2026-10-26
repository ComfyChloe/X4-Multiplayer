//! Pose test cases shared with the offline oracle (`xform_oracle pose`).
//!
//! ```text
//! cases   "X4XFPOS1"  u32 n, case[n]:
//!           u32 kind (1 = GetLocalTransform compose, 2 = GetTransformRelativeTo)
//!           u32 nodes, per node: i32 parent, u32 local[16] (IEEE bits; the controller's
//!           transform; the parent's child-frame transform is the identity)
//!           u32 node, i32 target (-1 = none), u32 mat[16]
//! results "X4XFPOR1"  u32 n, u32 mat[16] per case
//! ```
//! Kind 1 calls `GetLocalTransform(node, mat, t, reset = 0, include_parent = 1)` on a node
//! with a parent; kind 2 calls `GetTransformRelativeTo(node, mat, t, target, 0)`. Cases never
//! compose a root (that path is controller-specific).

use crate::posrot::{compose_onto, transform_relative_to, PosRot, PoseTree};
use crate::scenario::Rng;
use std::io;

/// One case.
#[derive(Clone, Debug)]
pub struct PoseCase {
    /// 1 = compose, 2 = relative.
    pub kind: u32,
    /// Parent and controller transform per node.
    pub nodes: Vec<(Option<usize>, PosRot)>,
    /// Node the call is made on.
    pub node: usize,
    /// Target of the relative call.
    pub target: Option<usize>,
    /// Input matrix.
    pub mat: PosRot,
}

struct Tree<'a>(&'a [(Option<usize>, PosRot)]);
impl PoseTree for Tree<'_> {
    type Node = usize;
    fn parent(&self, n: usize) -> Option<usize> {
        self.0[n].0
    }
    fn compose_local(&self, n: usize, mat: &mut PosRot) {
        assert!(self.0[n].0.is_some(), "cases never compose a root");
        compose_onto(mat, &self.0[n].1);
    }
}

impl PoseCase {
    /// The model's result.
    pub fn expected(&self) -> PosRot {
        let mut m = self.mat;
        match self.kind {
            1 => compose_onto(&mut m, &self.nodes[self.node].1),
            _ => {
                transform_relative_to(&Tree(&self.nodes), self.node, &mut m, self.target);
            }
        }
        m
    }
}

fn wide(rng: &mut Rng) -> f32 {
    match rng.below(10) {
        0 => [0.0, -0.0, 1.0, -1.0, 0.5, 2.0][rng.below(6) as usize],
        1 => {
            // subnormal
            let bits = (rng.next() as u32 & 0x007f_ffff).max(1) | (rng.next() as u32 & 0x8000_0000);
            f32::from_bits(bits)
        }
        _ => {
            let exp = rng.below(100) as i32 - 40;
            let mant = 1.0 + rng.unit() as f32;
            let s = if rng.chance(0.5) { -1.0 } else { 1.0 };
            s * mant * (2.0f32).powi(exp)
        }
    }
}

fn rotation(rng: &mut Rng, scale_exp: (i32, i32)) -> PosRot {
    // random unit quaternion -> basis rows, computed in f64 and rounded
    let mut q = [0f64; 4];
    loop {
        for v in q.iter_mut() {
            *v = rng.unit() * 2.0 - 1.0;
        }
        let n = q.iter().map(|v| v * v).sum::<f64>();
        if n > 1e-6 && n <= 1.0 {
            let n = n.sqrt();
            for v in q.iter_mut() {
                *v /= n;
            }
            break;
        }
    }
    let [w, x, y, z] = q;
    let r = [
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y + w * z), 2.0 * (x * z - w * y)],
        [2.0 * (x * y - w * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z + w * x)],
        [2.0 * (x * z + w * y), 2.0 * (y * z - w * x), 1.0 - 2.0 * (x * x + y * y)],
    ];
    let mut m = PosRot::IDENTITY;
    let span = (scale_exp.1 - scale_exp.0).max(1) as u64;
    for i in 0..3 {
        let e = scale_exp.0 + rng.below(span) as i32;
        let s = if rng.chance(0.5) { -1.0 } else { 1.0 };
        m.rows[0][i] = (s * rng.unit() * 10f64.powi(e)) as f32;
        for j in 0..3 {
            m.rows[i + 1][j] = r[i][j] as f32;
        }
    }
    m
}

fn matrix(rng: &mut Rng, wide_values: bool) -> PosRot {
    if wide_values && rng.chance(0.4) {
        let mut m = PosRot::IDENTITY;
        for row in m.rows.iter_mut() {
            for v in row.iter_mut() {
                *v = wide(rng);
            }
        }
        return m;
    }
    let mut m = rotation(rng, (-3, 7));
    if rng.chance(0.2) {
        // non-zero w lanes and small perturbations
        for row in m.rows.iter_mut() {
            row[3] = if rng.chance(0.5) { (rng.unit() * 2.0 - 1.0) as f32 } else { -0.0 };
        }
    }
    m
}

/// Generates `n` cases: compose cases with wide-range values (subnormals, signed zeros,
/// magnitudes up to 2^60) and relative cases on random trees of up to 8 components.
pub fn generate(seed: u64, n: usize) -> Vec<PoseCase> {
    let mut rng = Rng(seed);
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        if rng.chance(0.5) {
            let nodes = vec![(None, PosRot::IDENTITY), (Some(0), matrix(&mut rng, true))];
            out.push(PoseCase { kind: 1, nodes, node: 1, target: None, mat: matrix(&mut rng, true) });
            continue;
        }
        let count = 2 + rng.below(7) as usize;
        let mut nodes = vec![(None, matrix(&mut rng, false))];
        for i in 1..count {
            let p = rng.below(i as u64) as usize;
            nodes.push((Some(p), matrix(&mut rng, false)));
        }
        let node = 1 + rng.below(count as u64 - 1) as usize;
        let target = match rng.below(6) {
            0 => None,
            1 => Some(node),
            2 => nodes[node].0,
            _ => Some(rng.below(count as u64) as usize),
        };
        out.push(PoseCase { kind: 2, nodes, node, target, mat: matrix(&mut rng, false) });
    }
    out
}

/// Serialises cases.
pub fn to_bytes(cases: &[PoseCase]) -> Vec<u8> {
    let mut b = b"X4XFPOS1".to_vec();
    let put = |v: u32, b: &mut Vec<u8>| b.extend_from_slice(&v.to_le_bytes());
    put(cases.len() as u32, &mut b);
    for c in cases {
        put(c.kind, &mut b);
        put(c.nodes.len() as u32, &mut b);
        for (p, l) in &c.nodes {
            put(p.map_or(u32::MAX, |x| x as u32), &mut b);
            for v in l.to_bits() {
                put(v, &mut b);
            }
        }
        put(c.node as u32, &mut b);
        put(c.target.map_or(u32::MAX, |x| x as u32), &mut b);
        for v in c.mat.to_bits() {
            put(v, &mut b);
        }
    }
    b
}

/// Parses cases.
pub fn from_bytes(b: &[u8]) -> io::Result<Vec<PoseCase>> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, "bad pose case file");
    if b.len() < 12 || &b[..8] != b"X4XFPOS1" {
        return Err(bad());
    }
    let mut at = 8;
    let get = |at: &mut usize| -> io::Result<u32> {
        let v = b.get(*at..*at + 4).ok_or_else(bad)?;
        *at += 4;
        Ok(u32::from_le_bytes(v.try_into().unwrap()))
    };
    let n = get(&mut at)? as usize;
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let kind = get(&mut at)?;
        let count = get(&mut at)? as usize;
        let mut nodes = Vec::with_capacity(count);
        for _ in 0..count {
            let p = get(&mut at)?;
            let mut bits = [0u32; 16];
            for v in bits.iter_mut() {
                *v = get(&mut at)?;
            }
            nodes.push((if p == u32::MAX { None } else { Some(p as usize) }, PosRot::from_bits(bits)));
        }
        let node = get(&mut at)? as usize;
        let t = get(&mut at)?;
        let mut bits = [0u32; 16];
        for v in bits.iter_mut() {
            *v = get(&mut at)?;
        }
        out.push(PoseCase {
            kind,
            nodes,
            node,
            target: if t == u32::MAX { None } else { Some(t as usize) },
            mat: PosRot::from_bits(bits),
        });
    }
    Ok(out)
}

/// Parses an oracle result file.
pub fn results_from_bytes(b: &[u8]) -> io::Result<Vec<PosRot>> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, "bad pose result file");
    if b.len() < 12 || &b[..8] != b"X4XFPOR1" {
        return Err(bad());
    }
    let n = u32::from_le_bytes(b[8..12].try_into().unwrap()) as usize;
    if b.len() != 12 + n * 64 {
        return Err(bad());
    }
    Ok((0..n)
        .map(|k| {
            let mut bits = [0u32; 16];
            for (j, v) in bits.iter_mut().enumerate() {
                let at = 12 + k * 64 + j * 4;
                *v = u32::from_le_bytes(b[at..at + 4].try_into().unwrap());
            }
            PosRot::from_bits(bits)
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_and_no_root_composition() {
        let cases = generate(3, 500);
        let parsed = from_bytes(&to_bytes(&cases)).unwrap();
        for (a, b) in cases.iter().zip(&parsed) {
            assert!(a.mat.bit_eq(&b.mat));
            let ea = a.expected();
            assert!(ea.bit_eq(&b.expected()));
            assert!(ea.to_array().iter().all(|v| v.is_finite()), "inputs must not overflow");
        }
    }
}
