//! Scenario and result files shared with the offline oracle (`native/engine/xform_oracle.c`)
//! and, later, the in-game capture probe; and a generator of engine-like universes.
//!
//! All integers little-endian. Floats are stored as their IEEE bits.
//!
//! ```text
//! scenario  "X4XFSCN1"
//!   u32 nodes, u32 root, u32 frames, u32 first_idx, u32 flags (bit 0 = global 0x2f4b9bc)
//!   f64 time0, f64 dt                      frame k: idx = first_idx ^ (k & 1), time = time0 + k*dt
//!   per node:
//!     u64 id, i32 parent, u32 flags(+0x1e0), u32 mask(+0x1e4), i32 count(+0x1e8),
//!     u32 dirty[2], u64 classes_lo, u64 classes_hi, u32 registered, i32 mode, i32 attention,
//!     i32 vc_attention[2], i32 vc_parent[2], i32 refs,
//!     u32 n, u32 child[n]   (cache 0)      u32 n, u32 child[n]   (cache 1)
//!     i32 n, u32 live[n]    (n = -1: no child container)
//!     i32 n, u32 attach[n]  (n = -1: no attachment container; 0xffffffff = empty slot,
//!                            0xfffffffe = bucket boundary)
//!     i32 n, entry[n]       (n = -1: no list) entry = i32 kind, u32 flags, u32 class,
//!                            u32 truncated, u64 interval bits, u64 last bits
//!
//! result    "X4XFRES1"
//!   u32 frames; per frame: u32 n, event[n] = u32 kind, u32 node, u32 a, u32 b
//!     kind 1 IsClass(a = class, b = result)  2 pose getter (a = idx)  3 scale getter (a = idx)
//!     kind 4 velocity getter (a = idx, b = pose refreshed)  5 attachment getter (a = idx, b = other)
//!   per node: u32 flags, i32 count, u32 dirty[2], i32 vc_attention[2], i32 vc_parent[2],
//!             i32 refs, i32 n, entry[n]
//! ```

use crate::cache::{CacheTree, Event, Getter, Node, SpecEntry};
use std::io;

const SCN_MAGIC: &[u8; 8] = b"X4XFSCN1";
const RES_MAGIC: &[u8; 8] = b"X4XFRES1";
/// Empty slot marker in an attachment container.
pub const ATTACH_HOLE: u32 = 0xffff_ffff;
/// Bucket boundary marker in an attachment container.
pub const ATTACH_BUCKET: u32 = 0xffff_fffe;

/// A view-cache scenario: a tree, its attachment container layout and the frames to run.
#[derive(Clone, Debug)]
pub struct Scenario {
    /// The tree.
    pub tree: CacheTree,
    /// Raw attachment container layout per node (holes and bucket boundaries included);
    /// `Node::attach` holds the non-empty slots in order.
    pub attach_layout: Vec<Option<Vec<u32>>>,
    /// Root (galaxy).
    pub root: usize,
    /// Frames to run.
    pub frames: u32,
    /// Cache index of frame 0 (alternates every frame like the engine's double buffer).
    pub first_idx: u32,
    /// Time of frame 0.
    pub time0: f64,
    /// Time step.
    pub dt: f64,
}

/// What a frame run produced, in the form the oracle can observe.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Observed {
    /// Virtual-call events per frame: (kind, node, a, b).
    pub frames: Vec<Vec<[u32; 4]>>,
    /// Final per-node state, serialised (see the module docs).
    pub state: Vec<u8>,
}

struct W(Vec<u8>);
impl W {
    fn u32(&mut self, v: u32) {
        self.0.extend_from_slice(&v.to_le_bytes());
    }
    fn i32(&mut self, v: i32) {
        self.0.extend_from_slice(&v.to_le_bytes());
    }
    fn u64(&mut self, v: u64) {
        self.0.extend_from_slice(&v.to_le_bytes());
    }
    fn f64(&mut self, v: f64) {
        self.u64(v.to_bits());
    }
    fn opt(&mut self, v: Option<usize>) {
        self.i32(v.map_or(-1, |x| x as i32));
    }
    fn entry(&mut self, e: &SpecEntry) {
        self.i32(e.kind);
        self.u32(e.flags);
        self.u32(e.class);
        self.u32(e.truncated as u32);
        self.f64(e.interval);
        self.f64(e.last);
    }
}

struct R<'a>(&'a [u8], usize);
impl R<'_> {
    fn take(&mut self, n: usize) -> io::Result<&[u8]> {
        if self.1 + n > self.0.len() {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "truncated file"));
        }
        let s = &self.0[self.1..self.1 + n];
        self.1 += n;
        Ok(s)
    }
    fn u32(&mut self) -> io::Result<u32> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn i32(&mut self) -> io::Result<i32> {
        Ok(i32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> io::Result<u64> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
    fn f64(&mut self) -> io::Result<f64> {
        Ok(f64::from_bits(self.u64()?))
    }
    fn opt(&mut self) -> io::Result<Option<usize>> {
        let v = self.i32()?;
        Ok(if v < 0 { None } else { Some(v as usize) })
    }
    fn list(&mut self) -> io::Result<Vec<usize>> {
        let n = self.u32()? as usize;
        (0..n).map(|_| self.u32().map(|v| v as usize)).collect()
    }
    fn opt_list(&mut self) -> io::Result<Option<Vec<u32>>> {
        let n = self.i32()?;
        if n < 0 {
            return Ok(None);
        }
        (0..n).map(|_| self.u32()).collect::<io::Result<Vec<u32>>>().map(Some)
    }
    fn entry(&mut self) -> io::Result<SpecEntry> {
        Ok(SpecEntry {
            kind: self.i32()?,
            flags: self.u32()?,
            class: self.u32()?,
            truncated: self.u32()? != 0,
            interval: self.f64()?,
            last: self.f64()?,
        })
    }
}

impl Scenario {
    /// Serialises the scenario.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut w = W(SCN_MAGIC.to_vec());
        let t = &self.tree;
        w.u32(t.nodes.len() as u32);
        w.u32(self.root as u32);
        w.u32(self.frames);
        w.u32(self.first_idx);
        w.u32(t.drop_pose_blocks as u32);
        w.f64(self.time0);
        w.f64(self.dt);
        for (i, n) in t.nodes.iter().enumerate() {
            w.u64(n.id);
            w.opt(n.parent);
            w.u32(n.flags);
            w.u32(n.mask);
            w.i32(n.count);
            w.u32(n.dirty[0]);
            w.u32(n.dirty[1]);
            w.u64(n.classes as u64);
            w.u64((n.classes >> 64) as u64);
            w.u32(n.registered as u32);
            w.i32(n.mode);
            w.i32(n.attention);
            w.i32(n.vc_attention[0]);
            w.i32(n.vc_attention[1]);
            w.opt(n.vc_parent[0]);
            w.opt(n.vc_parent[1]);
            w.i32(n.refs);
            for k in 0..2 {
                w.u32(n.children[k].len() as u32);
                for &c in &n.children[k] {
                    w.u32(c as u32);
                }
            }
            match &n.live_children {
                None => w.i32(-1),
                Some(l) => {
                    w.i32(l.len() as i32);
                    for &c in l {
                        w.u32(c as u32);
                    }
                }
            }
            match self.attach_layout.get(i).cloned().flatten() {
                None => w.i32(-1),
                Some(l) => {
                    w.i32(l.len() as i32);
                    for c in l {
                        w.u32(c);
                    }
                }
            }
            match &n.spec {
                None => w.i32(-1),
                Some(l) => {
                    w.i32(l.len() as i32);
                    for e in l {
                        w.entry(e);
                    }
                }
            }
        }
        w.0
    }

    /// Parses a scenario.
    pub fn from_bytes(b: &[u8]) -> io::Result<Scenario> {
        let mut r = R(b, 0);
        if r.take(8)? != SCN_MAGIC {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "not a view-cache scenario"));
        }
        let count = r.u32()? as usize;
        let root = r.u32()? as usize;
        let frames = r.u32()?;
        let first_idx = r.u32()?;
        let flags = r.u32()?;
        let time0 = r.f64()?;
        let dt = r.f64()?;
        let mut nodes = Vec::with_capacity(count);
        let mut layout = Vec::with_capacity(count);
        for _ in 0..count {
            let mut n = Node {
                id: r.u64()?,
                parent: r.opt()?,
                flags: r.u32()?,
                mask: r.u32()?,
                count: r.i32()?,
                ..Node::default()
            };
            n.dirty = [r.u32()?, r.u32()?];
            n.classes = r.u64()? as u128 | (r.u64()? as u128) << 64;
            n.registered = r.u32()? != 0;
            n.mode = r.i32()?;
            n.attention = r.i32()?;
            n.vc_attention = [r.i32()?, r.i32()?];
            n.vc_parent = [r.opt()?, r.opt()?];
            n.refs = r.i32()?;
            n.children = [r.list()?, r.list()?];
            n.live_children = r.opt_list()?.map(|v| v.into_iter().map(|x| x as usize).collect());
            let att = r.opt_list()?;
            n.attach = att.as_ref().map(|v| v.iter().filter(|&&x| x < ATTACH_BUCKET).map(|&x| x as usize).collect());
            layout.push(att);
            let ns = r.i32()?;
            n.spec = if ns < 0 { None } else { Some((0..ns).map(|_| r.entry()).collect::<io::Result<_>>()?) };
            nodes.push(n);
        }
        Ok(Scenario {
            tree: CacheTree { nodes, drop_pose_blocks: flags & 1 != 0 },
            attach_layout: layout,
            root,
            frames,
            first_idx,
            time0,
            dt,
        })
    }

    /// Runs every frame with the model and returns what the oracle can observe.
    pub fn run_model(&self) -> Result<(Observed, Vec<Vec<Event>>), crate::cache::Overrun> {
        let mut tree = self.tree.clone();
        let mut frames = Vec::new();
        let mut logs = Vec::new();
        for k in 0..self.frames {
            let idx = (self.first_idx ^ (k & 1)) as usize;
            let time = self.time0 + k as f64 * self.dt;
            let log = crate::cache::frame(&mut tree, self.root, idx, time, &mut crate::cache::NoGetters)?;
            frames.push(project(&log, idx as u32));
            logs.push(log);
        }
        Ok((Observed { frames, state: state_bytes(&tree) }, logs))
    }
}

/// The virtual calls of an event log, as the oracle records them.
pub fn project(log: &[Event], idx: u32) -> Vec<[u32; 4]> {
    let mut out = Vec::new();
    for e in log {
        match *e {
            Event::IsClass { node, class, result } => out.push([1, node, class, result as u32]),
            Event::Get { node, getter: Getter::Pose } => out.push([2, node, idx, 0]),
            Event::Get { node, getter: Getter::Scale } => out.push([3, node, idx, 0]),
            Event::Get { node, getter: Getter::Velocity { with_pose } } => out.push([4, node, idx, with_pose as u32]),
            Event::Get { node, getter: Getter::Connections { other } } => out.push([5, node, idx, other as u32]),
            _ => {}
        }
    }
    out
}

/// The final per-node state in the result-file layout.
pub fn state_bytes(tree: &CacheTree) -> Vec<u8> {
    let mut w = W(Vec::new());
    for n in &tree.nodes {
        w.u32(n.flags);
        w.i32(n.count);
        w.u32(n.dirty[0]);
        w.u32(n.dirty[1]);
        w.i32(n.vc_attention[0]);
        w.i32(n.vc_attention[1]);
        w.opt(n.vc_parent[0]);
        w.opt(n.vc_parent[1]);
        w.i32(n.refs);
        match &n.spec {
            None => w.i32(-1),
            Some(l) => {
                w.i32(l.len() as i32);
                for e in l {
                    w.entry(e);
                }
            }
        }
    }
    w.0
}

/// Parses a result file written by the oracle (or by [`Observed::to_bytes`]).
pub fn parse_result(b: &[u8], nodes: usize) -> io::Result<Observed> {
    let mut r = R(b, 0);
    if r.take(8)? != RES_MAGIC {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "not a view-cache result"));
    }
    let nf = r.u32()?;
    let mut frames = Vec::new();
    for _ in 0..nf {
        let n = r.u32()?;
        let mut f = Vec::with_capacity(n as usize);
        for _ in 0..n {
            f.push([r.u32()?, r.u32()?, r.u32()?, r.u32()?]);
        }
        frames.push(f);
    }
    let start = r.1;
    // Walk the state to find its end (variable-length lists).
    for _ in 0..nodes {
        r.take(4 * 9)?;
        let ns = r.i32()?;
        if ns > 0 {
            r.take(ns as usize * 32)?;
        }
    }
    Ok(Observed { frames, state: b[start..r.1].to_vec() })
}

impl Observed {
    /// Serialises in the result-file layout.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut w = W(RES_MAGIC.to_vec());
        w.u32(self.frames.len() as u32);
        for f in &self.frames {
            w.u32(f.len() as u32);
            for e in f {
                for v in e {
                    w.u32(*v);
                }
            }
        }
        w.0.extend_from_slice(&self.state);
        w.0
    }
}

/// SplitMix64, for reproducible scenarios without dependencies.
#[derive(Clone, Debug)]
pub struct Rng(pub u64);
impl Rng {
    /// Next 64 random bits.
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }
    /// Uniform in `0..n` (n > 0).
    pub fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    /// True with probability `p`.
    pub fn chance(&mut self, p: f64) -> bool {
        self.unit() < p
    }
    /// Uniform in [0, 1).
    pub fn unit(&mut self) -> f64 {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// Generator settings.
#[derive(Clone, Debug)]
pub struct Shape {
    /// Clusters under the galaxy.
    pub clusters: usize,
    /// Sectors per cluster (1..=n).
    pub sectors: usize,
    /// Zones per sector (1..=n).
    pub zones: usize,
    /// Objects per zone (0..=n).
    pub objects: usize,
    /// Child depth below objects (0..=n levels, 0..=3 children each).
    pub depth: usize,
    /// Frames to run.
    pub frames: u32,
    /// Restrict to what the offline oracle can execute: no child refresh (bit 0x2), no
    /// pose-block drop (bit 0x200000), no live child containers, and every specification
    /// list keeps an entry that never expires (an empty list is freed with the engine's
    /// allocator, which the oracle does not run).
    pub oracle_safe: bool,
    /// Make some child vectors stale (live parent elsewhere) to exercise the parallel
    /// hazard checks; never for the oracle.
    pub stale: bool,
}

impl Default for Shape {
    fn default() -> Shape {
        Shape { clusters: 3, sectors: 3, zones: 3, objects: 6, depth: 2, frames: 6, oracle_safe: true, stale: false }
    }
}

const GALAXY: &[u32] = &[0x2f, 0x76, 0x57, 0x4d, 0x16];
const CLUSTER: &[u32] = &[0x0f, 0x76, 0x57, 0x4d, 0x16];
const SECTOR: &[u32] = &[0x58, 0x76, 0x57, 0x4d, 0x16];
const ZONE: &[u32] = &[0x6d, 0x76, 0x57, 0x4d, 0x16];
const SHIP: &[u32] = &[0x5c, 0x75, 0x70, 0x6f, 0x23, 0x24, 0x49, 0x4d, 0x16];
const STATION: &[u32] = &[0x62, 0x70, 0x6f, 0x23, 0x24, 0x49, 0x4d, 0x16];
const PART: &[&[u32]] = &[
    &[0x6b, 0x24, 0x49, 0x4d, 0x16],
    &[0x68, 0x24, 0x49, 0x4d, 0x16],
    &[0x73, 0x24, 0x4d, 0x16],
    &[0x28, 0x4d, 0x16],
    &[0x48, 0x2c, 0x4d, 0x16],
];

fn classes(list: &[u32]) -> u128 {
    list.iter().fold(0u128, |a, &c| a | 1u128 << c)
}

/// Generates an engine-like universe: galaxy -> clusters -> sectors -> zones -> ships and
/// stations -> parts, with the engine's specification kinds (galaxy, active sectors and
/// zones, highways, one-shot own/map entries, timers), random own flags, dirty bits and
/// clear masks, attachments (with holes and buckets) and two cache indices.
pub fn generate(seed: u64, shape: &Shape) -> Scenario {
    use crate::cache::{bits, kinds, CLASS_NONE, COMPONENT_MASK_INIT};
    let mut rng = Rng(seed);
    let mut nodes: Vec<Node> = Vec::new();
    let add = |nodes: &mut Vec<Node>, parent: Option<usize>, cls: &[u32], rng: &mut Rng| -> usize {
        let i = nodes.len();
        let mut n = Node { id: 0x1000 + i as u64, parent, classes: classes(cls), refs: 1000, ..Node::default() };
        n.registered = rng.chance(0.9);
        n.attention = rng.below(12) as i32;
        n.vc_attention = [rng.below(12) as i32, rng.below(12) as i32];
        n.vc_parent = [parent, parent];
        if rng.chance(0.05) {
            // A cached parent that differs from the live one (left over from a reparent).
            n.vc_parent[rng.below(2) as usize] =
                if i > 0 && rng.chance(0.5) { Some(rng.below(i as u64) as usize) } else { None };
        }
        nodes.push(n);
        if let Some(p) = parent {
            nodes[p].children[0].push(i);
            nodes[p].children[1].push(i);
        }
        i
    };
    let root = add(&mut nodes, None, GALAXY, &mut rng);
    let mut objects = Vec::new();
    let mut zones = Vec::new();
    let mut sectors = Vec::new();
    for _ in 0..shape.clusters {
        let c = add(&mut nodes, Some(root), CLUSTER, &mut rng);
        for _ in 0..1 + rng.below(shape.sectors as u64) {
            let s = add(&mut nodes, Some(c), SECTOR, &mut rng);
            sectors.push(s);
            for _ in 0..1 + rng.below(shape.zones as u64) {
                let z = add(&mut nodes, Some(s), ZONE, &mut rng);
                zones.push(z);
                for _ in 0..rng.below(shape.objects as u64 + 1) {
                    let o = add(&mut nodes, Some(z), if rng.chance(0.8) { SHIP } else { STATION }, &mut rng);
                    objects.push(o);
                    let mut level = vec![o];
                    for _ in 0..rng.below(shape.depth as u64 + 1) {
                        let mut next = Vec::new();
                        for &p in &level {
                            for _ in 0..rng.below(4) {
                                let k = rng.below(PART.len() as u64) as usize;
                                next.push(add(&mut nodes, Some(p), PART[k], &mut rng));
                            }
                        }
                        level = next;
                    }
                }
            }
        }
    }
    let total = nodes.len();
    // Dirty bits, masks, own flags.
    let mut allowed = bits::PARENT
        | bits::ATTACH
        | bits::TRANSFORM
        | bits::VELOCITY
        | bits::CONNECTIONS
        | bits::ATTENTION
        | bits::POSE_CHANGED
        | bits::CONNECTIONS_FORCED;
    if !shape.oracle_safe {
        allowed |= bits::CHILDREN | bits::DROP_POSE_BLOCK;
    }
    for n in nodes.iter_mut() {
        n.mask = if rng.chance(0.5) { COMPONENT_MASK_INIT } else { COMPONENT_MASK_INIT & !(rng.next() as u32 & 0x7f) };
        for k in 0..2 {
            n.dirty[k] = rng.next() as u32 & allowed & if rng.chance(0.2) { 0 } else { u32::MAX };
        }
        if rng.chance(0.05) {
            n.flags = rng.next() as u32 & 0x7f;
        }
        n.mode = [0, 0, 1, 2][rng.below(4) as usize];
    }
    // Live child containers (child refresh source), possibly differing from the vectors.
    if !shape.oracle_safe {
        for i in 0..total {
            if rng.chance(0.7) {
                let mut live = nodes[i].children[0].clone();
                if !live.is_empty() && rng.chance(0.2) {
                    live.remove(rng.below(live.len() as u64) as usize);
                }
                nodes[i].live_children = Some(live);
            }
        }
        if shape.stale {
            // A few child vectors still list a component that moved elsewhere.
            for _ in 0..1 + total / 200 {
                let a = objects.get(rng.below(objects.len().max(1) as u64) as usize).copied();
                let z = zones.get(rng.below(zones.len().max(1) as u64) as usize).copied();
                if let (Some(a), Some(z)) = (a, z) {
                    let k = rng.below(2) as usize;
                    if !nodes[z].children[k].contains(&a) {
                        nodes[z].children[k].push(a);
                    }
                }
            }
        }
    }
    // Attachments: some objects and parts carry attachment containers (leaf parts).
    let mut layout: Vec<Option<Vec<u32>>> = vec![None; total];
    for i in 0..total {
        if !nodes[i].children[0].is_empty() || !rng.chance(0.15) || i == root {
            continue;
        }
        let parent = nodes[i].parent;
        let mut slots = Vec::new();
        let mut elems = Vec::new();
        for _ in 0..1 + rng.below(3) {
            if !slots.is_empty() {
                slots.push(ATTACH_BUCKET);
            }
            for _ in 0..rng.below(4) {
                if rng.chance(0.25) {
                    slots.push(ATTACH_HOLE);
                } else {
                    let a = nodes.len();
                    let mut n = Node {
                        id: 0x1000 + a as u64,
                        parent: Some(i),
                        classes: classes(PART[1]),
                        refs: 1000,
                        ..Node::default()
                    };
                    n.mask = COMPONENT_MASK_INIT;
                    n.dirty = [rng.next() as u32 & allowed, rng.next() as u32 & allowed];
                    n.vc_parent = [Some(i), Some(i)];
                    nodes.push(n);
                    layout.push(None);
                    slots.push(a as u32);
                    elems.push(a);
                }
            }
        }
        let _ = parent;
        nodes[i].attach = Some(elems);
        layout[i] = Some(slots);
    }
    // Specifications.
    let entry = |kind, flags, class, interval, last| SpecEntry { kind, flags, class, truncated: false, interval, last };
    let time0 = 100.0 + rng.below(1000) as f64 * 0.125;
    nodes[root].spec = Some(vec![entry(kinds::GALAXY, 0x6b, 0x58, 0.0, time0 - 0.016)]);
    for &s in &sectors {
        if rng.chance(0.4) {
            nodes[s].spec = Some(vec![entry(kinds::SECTOR, 0x4f, 0x6d, 0.0, time0 - 0.016)]);
        }
    }
    for &z in &zones {
        if rng.chance(0.3) {
            nodes[z].spec.get_or_insert_with(Vec::new).push(entry(kinds::ZONE, 0x7f, CLASS_NONE, 0.0, time0 - 0.016));
        }
        if rng.chance(0.1) {
            let iv = [0.25, 0.5, 1.0][rng.below(3) as usize];
            nodes[z].spec.get_or_insert_with(Vec::new).push(entry(
                kinds::HIGHWAY,
                0x7f,
                CLASS_NONE,
                iv,
                time0 - rng.unit() * iv,
            ));
        }
    }
    let candidates: Vec<usize> = (1..total).collect();
    for _ in 0..1 + total / 25 {
        let n = candidates[rng.below(candidates.len() as u64) as usize];
        let flags = [0x19, 0x2, 0x4, 0x20, 0x18, 0x09][rng.below(6) as usize];
        let (kind, class) = if rng.chance(0.8) { (kinds::OWN, CLASS_NONE) } else { (kinds::MAP, 0x49) };
        let interval = if rng.chance(0.85) { -1.0 } else { -1.5 };
        let list = nodes[n].spec.get_or_insert_with(Vec::new);
        if list.iter().all(|e| e.kind != kind) {
            list.push(entry(kind, flags, class, interval, time0));
            if kind == kinds::OWN {
                nodes[n].flags = flags;
            }
        }
    }
    for _ in 0..total / 60 {
        let n = candidates[rng.below(candidates.len() as u64) as usize];
        let class = [CLASS_NONE, 0x49, 0x28, 0x6d, 0x73][rng.below(5) as usize];
        let iv = [0.0, 0.03, 0.1, 2.0][rng.below(4) as usize];
        let flags = (rng.next() as u32 & 0x7f).max(1);
        nodes[n].spec.get_or_insert_with(Vec::new).push(entry(
            kinds::VENTURE,
            flags,
            class,
            iv,
            time0 - rng.unit() * 0.1,
        ));
    }
    if shape.oracle_safe {
        // Keep one entry that never expires in every list.
        for n in nodes.iter_mut() {
            if let Some(l) = &mut n.spec {
                if l.iter().all(|e| e.interval < 0.0) {
                    l.insert(0, entry(kinds::HIGHWAY_ZONES, 0x08, 0x6d, 0.0, time0 - 0.016));
                }
            }
        }
    }
    let mut tree = CacheTree { nodes, drop_pose_blocks: true };
    crate::cache::recount(&mut tree);
    // Occasionally a wrong count, to exercise the gate on its own.
    if rng.chance(0.3) {
        let n = rng.below(tree.nodes.len() as u64) as usize;
        tree.nodes[n].count += 1;
    }
    Scenario {
        tree,
        attach_layout: layout,
        root,
        frames: shape.frames,
        first_idx: rng.below(2) as u32,
        time0,
        dt: 0.016 + rng.unit() * 0.02,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scenario_round_trip() {
        let s = generate(7, &Shape { oracle_safe: false, ..Shape::default() });
        let b = s.to_bytes();
        let t = Scenario::from_bytes(&b).unwrap();
        assert_eq!(t.to_bytes(), b);
        let (obs, _) = s.run_model().unwrap();
        let parsed = parse_result(&obs.to_bytes(), s.tree.nodes.len()).unwrap();
        assert_eq!(parsed, obs);
    }
}
