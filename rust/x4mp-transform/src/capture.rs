//! View-cache captures (`X4XFCAP1`, written by `native/engine/xform_capture.h` in game or in the
//! offline oracle) and their replay with the model.
//!
//! A capture holds, for one frame, everything `BuildCache(galaxy, idx, time)` can read (the
//! components its walk reaches, their child vectors, child containers where a child refresh is
//! pending, attachments, specification lists, own flags, counts, masks, dirty bits, cached
//! attention and parent, reference counts, live parent chains, and `IsClass` for every
//! truncation class present), then the same components' state after the original build, and
//! optionally a trace of the build's refresh calls and recursion.
//!
//! ```text
//! "X4XFCAP1" u32 version(1) u32 number u64 frame f64 time u32 idx u32 flags(1 drop, 2 trace)
//! u64 walker_visits u64 walker_opens u32 nclasses u32 class[nclasses] u32 nodes
//! pre, per node: u64 id u64 ptr i32 parent u32 flags u32 mask u32 count u32 dirty[2]
//!   u64 classes_lo u64 classes_hi u32 registered u32 mode u32 attention u32 vc_attention[2]
//!   i32 vc_parent[2] u32 refs u32 open
//!   i32 n kids[n] | i32 n live[n] | i32 n attach_slots[n] | i32 n spec_entry[n]   (n = -1: absent)
//!   references: index, -1 null, -2 outside the capture; list items: index, 0xfffffffd outside,
//!   attachment slots also 0xffffffff empty and 0xfffffffe bucket boundary
//! post, per node: u32 flags u32 count u32 dirty[2] u32 vc_attention[2] i32 vc_parent[2] u32 refs
//!   i32 n spec_entry[n]
//! trace (flag 2): u32 n, n x {u32 kind, u32 node, u32 a, u32 b}: 6 refresh (a flags, b dirty
//!   before), 8 refresh done (a dirty after), 7 BuildCache entered below the root
//! spec_entry = i32 kind u32 flags u32 class u32 truncated u64 interval_bits u64 last_bits
//! ```
//!
//! References outside the capture map to one extra "outside" node at the end of the tree, so
//! reference-count and count updates that leave the capture are absorbed there.

use crate::cache::{CacheTree, Event, Getter, NoGetters, NoSplit, Node, SpecEntry, Traversal};
use crate::scenario::{ATTACH_BUCKET, ATTACH_HOLE};
use std::io;

const OUTSIDE_ITEM: u32 = 0xffff_fffd;
/// Dirty bits that the attachment refresh (vfunc 622) manages itself; not modelled.
pub const CONNECTION_MANAGED: u32 = 0x8_0000 | 0x10_0000 | 0x40_0000;

/// State of one component after the build.
#[derive(Clone, Debug)]
pub struct Post {
    /// +0x1e0.
    pub flags: u32,
    /// +0x1e8.
    pub count: i32,
    /// vc+0x3c of both caches.
    pub dirty: [u32; 2],
    /// vc+0x38 of both caches.
    pub vc_attention: [i32; 2],
    /// vc+0x00 of both caches (index, -1 null, -2 outside).
    pub vc_parent: [i32; 2],
    /// +0x40.
    pub refs: i32,
    /// +0xd8.
    pub spec: Option<Vec<SpecEntry>>,
}

/// A parsed capture.
#[derive(Clone, Debug)]
pub struct Capture {
    /// Capture number within the run.
    pub number: u32,
    /// Hook call count at the capture.
    pub frame: u64,
    /// BuildCache time argument.
    pub time: f64,
    /// Cache index written.
    pub idx: usize,
    /// Global 0x2f4b9bc.
    pub drop: bool,
    /// Walker counters (components entered, gates open).
    pub walker_visits: u64,
    /// Gates the walker found open.
    pub walker_opens: u64,
    /// Truncation classes evaluated.
    pub classes: Vec<u32>,
    /// UniverseIDs per node.
    pub ids: Vec<u64>,
    /// Gate open in the walker.
    pub open: Vec<bool>,
    /// Child vector recorded (false for components whose gate the walker found closed).
    pub kids_known: Vec<bool>,
    /// The tree (captured nodes, then the outside node).
    pub tree: CacheTree,
    /// State after the build, per captured node.
    pub post: Vec<Post>,
    /// Trace events, if traced.
    pub trace: Option<Vec<[u32; 4]>>,
    /// Byte offset of the post-state section in the file (tests and tools).
    pub post_at: usize,
}

struct R<'a>(&'a [u8], usize);
impl R<'_> {
    fn take(&mut self, n: usize) -> io::Result<&[u8]> {
        if self.1 + n > self.0.len() {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "truncated capture"));
        }
        let s = &self.0[self.1..self.1 + n];
        self.1 += n;
        Ok(s)
    }
    fn u32(&mut self) -> io::Result<u32> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn i32(&mut self) -> io::Result<i32> {
        Ok(self.u32()? as i32)
    }
    fn u64(&mut self) -> io::Result<u64> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
    fn entry(&mut self) -> io::Result<SpecEntry> {
        Ok(SpecEntry {
            kind: self.i32()?,
            flags: self.u32()?,
            class: self.u32()?,
            truncated: self.u32()? != 0,
            interval: f64::from_bits(self.u64()?),
            last: f64::from_bits(self.u64()?),
        })
    }
    fn spec(&mut self) -> io::Result<Option<Vec<SpecEntry>>> {
        let n = self.i32()?;
        if n < 0 {
            return Ok(None);
        }
        (0..n).map(|_| self.entry()).collect::<io::Result<Vec<_>>>().map(Some)
    }
    fn items(&mut self) -> io::Result<Option<Vec<u32>>> {
        let n = self.i32()?;
        if n < 0 {
            return Ok(None);
        }
        (0..n).map(|_| self.u32()).collect::<io::Result<Vec<_>>>().map(Some)
    }
}

fn bad(what: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, what.to_string())
}

impl Capture {
    /// Parses a capture file.
    pub fn parse(b: &[u8]) -> io::Result<Capture> {
        let mut r = R(b, 0);
        if r.take(8)? != b"X4XFCAP1" {
            return Err(bad("not a view-cache capture"));
        }
        if r.u32()? != 1 {
            return Err(bad("unsupported capture version"));
        }
        let number = r.u32()?;
        let frame = r.u64()?;
        let time = f64::from_bits(r.u64()?);
        let idx = r.u32()? as usize;
        let flags = r.u32()?;
        let walker_visits = r.u64()?;
        let walker_opens = r.u64()?;
        let nc = r.u32()? as usize;
        let classes = (0..nc).map(|_| r.u32()).collect::<io::Result<Vec<_>>>()?;
        let n = r.u32()? as usize;
        if idx > 1 || n == 0 {
            return Err(bad("bad capture header"));
        }
        let outside = n;
        let map = |v: i32| -> Option<usize> {
            match v {
                -1 => None,
                -2 => Some(outside),
                v if v >= 0 && (v as usize) < outside => Some(v as usize),
                _ => Some(outside),
            }
        };
        let item = |v: u32| -> usize {
            if v == OUTSIDE_ITEM || v as usize >= outside {
                outside
            } else {
                v as usize
            }
        };
        let mut nodes = Vec::with_capacity(n + 1);
        let (mut ids, mut open, mut kids_known) = (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
        for _ in 0..n {
            let id = r.u64()?;
            let _ptr = r.u64()?;
            let mut node =
                Node { id, parent: map(r.i32()?), flags: r.u32()?, mask: r.u32()?, count: r.i32()?, ..Node::default() };
            node.dirty = [r.u32()?, r.u32()?];
            node.classes = r.u64()? as u128 | (r.u64()? as u128) << 64;
            node.registered = r.u32()? != 0;
            node.mode = r.i32()?;
            node.attention = r.i32()?;
            node.vc_attention = [r.i32()?, r.i32()?];
            node.vc_parent = [map(r.i32()?), map(r.i32()?)];
            node.refs = r.i32()?;
            open.push(r.u32()? != 0);
            let kids = r.items()?;
            kids_known.push(kids.is_some());
            node.children[idx] = kids.unwrap_or_default().into_iter().map(item).collect();
            node.live_children = r.items()?.map(|v| v.into_iter().map(item).collect());
            node.attach = r
                .items()?
                .map(|v| v.into_iter().filter(|&x| x != ATTACH_HOLE && x != ATTACH_BUCKET).map(item).collect());
            node.spec = r.spec()?;
            ids.push(id);
            nodes.push(node);
        }
        nodes.push(Node { id: u64::MAX, refs: 0, ..Node::default() });
        let post_at = r.1;
        let mut post = Vec::with_capacity(n);
        for _ in 0..n {
            post.push(Post {
                flags: r.u32()?,
                count: r.i32()?,
                dirty: [r.u32()?, r.u32()?],
                vc_attention: [r.i32()?, r.i32()?],
                vc_parent: [r.i32()?, r.i32()?],
                refs: r.i32()?,
                spec: r.spec()?,
            });
        }
        let trace = if flags & 2 != 0 {
            let m = r.u32()? as usize;
            let mut t = Vec::with_capacity(m);
            for _ in 0..m {
                t.push([r.u32()?, r.u32()?, r.u32()?, r.u32()?]);
            }
            Some(t)
        } else {
            None
        };
        if r.1 != b.len() {
            return Err(bad("trailing bytes in capture"));
        }
        Ok(Capture {
            number,
            frame,
            time,
            idx,
            drop: flags & 1 != 0,
            walker_visits,
            walker_opens,
            classes,
            ids,
            open,
            kids_known,
            tree: CacheTree { nodes, drop_pose_blocks: flags & 1 != 0 },
            post,
            trace,
            post_at,
        })
    }

    /// Number of captured components (without the outside node).
    pub fn len(&self) -> usize {
        self.post.len()
    }

    /// Whether the capture is empty.
    pub fn is_empty(&self) -> bool {
        self.post.is_empty()
    }
}

/// Field of a mismatch.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Field {
    /// Own flags.
    Flags,
    /// Specification count.
    Count,
    /// Dirty bits of the written cache.
    Dirty,
    /// Dirty bits of the other cache.
    OtherDirty,
    /// Cached attention.
    Attention,
    /// Cached parent.
    Parent,
    /// Specification list.
    Spec,
    /// Reference count (may also be changed by other threads; reported separately).
    Refs,
    /// The model opened a gate the walker recorded as closed (capture incomplete there).
    Gate,
    /// Trace: refresh call sequence differs.
    TraceRefresh,
    /// Trace: visit sequence differs.
    TraceVisit,
}

/// One difference between the model's replay and the engine's post-state.
#[derive(Clone, Debug)]
pub struct Mismatch {
    /// Captured component.
    pub node: usize,
    /// Field.
    pub field: Field,
    /// Model value (formatted).
    pub model: String,
    /// Engine value (formatted).
    pub engine: String,
}

/// Counters from the replayed frame.
#[derive(Clone, Debug, Default)]
pub struct Census {
    /// Components captured.
    pub components: usize,
    /// BuildCache entries (open + closed) and open gates.
    pub visits: usize,
    /// Open gates.
    pub open: usize,
    /// Refresh calls (children loop) and attachment refreshes.
    pub refreshes: usize,
    /// Attachment refreshes.
    pub attachment_refreshes: usize,
    /// Refresh calls whose flags met a dirty bit (work to do).
    pub refreshes_with_work: usize,
    /// Pose getter calls (slot 648).
    pub poses: usize,
    /// Velocity getter calls (slot 650).
    pub velocities: usize,
    /// Attachment getter calls (slot 622).
    pub connections: usize,
    /// IsClass calls.
    pub is_class: usize,
    /// Expired specification entries.
    pub expired: usize,
    /// Specification entries by kind (index = kind, 0..=9) among captured components.
    pub spec_kinds: [usize; 10],
}

/// Result of a replay.
#[derive(Clone, Debug)]
pub struct Replay {
    /// The model's event log.
    pub log: Vec<Event>,
    /// Differences (empty = the model reproduced the engine).
    pub mismatches: Vec<Mismatch>,
    /// Counters.
    pub census: Census,
}

/// Replays `BuildCache(galaxy)` with the model on the captured pre-state and compares the
/// result with the captured post-state (and the trace, if present).
pub fn replay(c: &Capture) -> Replay {
    let mut tree = c.tree.clone();
    let mut getters = NoGetters;
    let (log, error) = {
        let mut t = Traversal::new(&mut tree, &mut getters, NoSplit, c.idx, c.time);
        let e = t.build_cache(0).err();
        (t.log, e)
    };
    let mut mismatches = Vec::new();
    if let Some(e) = error {
        mismatches.push(Mismatch { node: 0, field: Field::Gate, model: format!("{e:?}"), engine: "completed".into() });
    }
    let n = c.len();
    let outside = n;
    let mut connections = vec![false; n + 1];
    let mut census = Census { components: n, ..Census::default() };
    for e in &log {
        match *e {
            Event::Visit { node, open } => {
                census.visits += 1;
                if open {
                    census.open += 1;
                    let i = node as usize;
                    if i < n && !c.kids_known[i] {
                        mismatches.push(Mismatch {
                            node: i,
                            field: Field::Gate,
                            model: "open".into(),
                            engine: "closed (walker)".into(),
                        });
                    }
                }
            }
            Event::Propagate { node, flags, attach } => {
                census.refreshes += 1;
                if attach {
                    census.attachment_refreshes += 1;
                }
                let i = node as usize;
                if i < n && c.tree.nodes[i].dirty[c.idx] & flags != 0 {
                    census.refreshes_with_work += 1;
                }
            }
            Event::Get { node, getter } => match getter {
                Getter::Pose => census.poses += 1,
                Getter::Velocity { .. } => census.velocities += 1,
                Getter::Connections { .. } => {
                    census.connections += 1;
                    connections[node as usize] = true;
                }
                Getter::Scale => {}
            },
            Event::IsClass { .. } => census.is_class += 1,
            Event::Expire { .. } => census.expired += 1,
        }
    }
    for node in &c.tree.nodes[..n] {
        for e in node.spec.iter().flatten() {
            if (0..10).contains(&e.kind) {
                census.spec_kinds[e.kind as usize] += 1;
            }
        }
    }
    let ref_of = |v: Option<usize>| -> i32 {
        match v {
            None => -1,
            Some(x) if x == outside => -2,
            Some(x) => x as i32,
        }
    };
    let idx = c.idx;
    for i in 0..n {
        let m = &tree.nodes[i];
        let p = &c.post[i];
        let mut push =
            |field: Field, model: String, engine: String| mismatches.push(Mismatch { node: i, field, model, engine });
        if m.flags != p.flags {
            push(Field::Flags, format!("{:#x}", m.flags), format!("{:#x}", p.flags));
        }
        if m.count != p.count {
            push(Field::Count, m.count.to_string(), p.count.to_string());
        }
        let mask = if connections[i] { !CONNECTION_MANAGED } else { u32::MAX };
        if m.dirty[idx] & mask != p.dirty[idx] & mask {
            push(Field::Dirty, format!("{:#x}", m.dirty[idx]), format!("{:#x}", p.dirty[idx]));
        }
        if m.dirty[1 - idx] != p.dirty[1 - idx] {
            push(Field::OtherDirty, format!("{:#x}", m.dirty[1 - idx]), format!("{:#x}", p.dirty[1 - idx]));
        }
        if m.vc_attention != p.vc_attention {
            push(Field::Attention, format!("{:?}", m.vc_attention), format!("{:?}", p.vc_attention));
        }
        let mp = [ref_of(m.vc_parent[0]), ref_of(m.vc_parent[1])];
        if mp != p.vc_parent {
            push(Field::Parent, format!("{mp:?}"), format!("{:?}", p.vc_parent));
        }
        let same_spec = match (&m.spec, &p.spec) {
            (None, None) => true,
            (Some(a), Some(b)) => a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x.same(y)),
            _ => false,
        };
        if !same_spec {
            push(Field::Spec, format!("{:?}", m.spec), format!("{:?}", p.spec));
        }
        if m.refs != p.refs {
            push(Field::Refs, m.refs.to_string(), p.refs.to_string());
        }
    }
    if let Some(trace) = &c.trace {
        let engine_refreshes: Vec<(u32, u32)> = trace.iter().filter(|e| e[0] == 6).map(|e| (e[1], e[2])).collect();
        let model_refreshes: Vec<(u32, u32)> = log
            .iter()
            .filter_map(|e| if let Event::Propagate { node, flags, .. } = e { Some((*node, *flags)) } else { None })
            .collect();
        if engine_refreshes != model_refreshes {
            let at = engine_refreshes.iter().zip(&model_refreshes).position(|(a, b)| a != b).unwrap_or(0);
            mismatches.push(Mismatch {
                node: model_refreshes.get(at).map_or(0, |x| x.0 as usize),
                field: Field::TraceRefresh,
                model: format!("#{at} of {}: {:?}", model_refreshes.len(), model_refreshes.get(at)),
                engine: format!("#{at} of {}: {:?}", engine_refreshes.len(), engine_refreshes.get(at)),
            });
        }
        let engine_visits: Vec<u32> = trace.iter().filter(|e| e[0] == 7).map(|e| e[1]).collect();
        let model_visits: Vec<u32> = log
            .iter()
            .filter_map(|e| if let Event::Visit { node, .. } = e { Some(*node) } else { None })
            .skip(1)
            .collect();
        if engine_visits != model_visits {
            let at = engine_visits.iter().zip(&model_visits).position(|(a, b)| a != b).unwrap_or(0);
            mismatches.push(Mismatch {
                node: 0,
                field: Field::TraceVisit,
                model: format!("#{at} of {}: {:?}", model_visits.len(), model_visits.get(at)),
                engine: format!("#{at} of {}: {:?}", engine_visits.len(), engine_visits.get(at)),
            });
        }
    }
    Replay { log, mismatches, census }
}

/// Mismatch counts by field.
pub fn summarize(m: &[Mismatch]) -> Vec<(Field, usize)> {
    let mut counts: std::collections::BTreeMap<Field, usize> = Default::default();
    for x in m {
        *counts.entry(x.field).or_default() += 1;
    }
    counts.into_iter().collect()
}
