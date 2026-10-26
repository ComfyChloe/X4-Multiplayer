//! Engine-memory side of the switch: `BuildCache` done by us.
//!
//! 1. **Load** (read-only): the components the walk can reach are copied from engine memory into a
//!    compact [`CacheTree`], with the same superset rules as the capture core
//!    (`native/engine/xform_capture.h`): the gate, the specification stack with private
//!    truncation flags (truncation through the engine's own `IsClass`, vtable slot 573, which is
//!    a pure table lookup in every implementation), child containers where a child refresh is
//!    pending, attachments, live parent chains. With `threads > 1` the subtrees below sectors
//!    load on worker threads; copies are merged by component address, so a component reached
//!    twice (stale child vectors) is one node: no hazard, and nothing was written.
//! 2. **Plan**: the model ([`x4mp_transform::cache::Traversal`], bit-exact against the original
//!    code) runs `BuildCache(galaxy)` on the copy and yields the refresh calls and every
//!    component's final specification list, in walk order.
//! 3. **Execute** (MoveWorker): the plan is applied in walk order through the engine's own
//!    functions: `PropagateCacheFlags` for every refresh, and at each list's last end-of-subtree
//!    point the timer values are written and expired kinds removed with
//!    `RemoveCacheSpecification` (which also does the count and kind-7 flag updates exactly as
//!    `BuildCache`'s end loop does; kinds are unique per list because `AddCacheSpecification`
//!    merges same-kind entries).
//!
//! Nothing is executed unless loading and planning succeeded; then the caller runs the original.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use x4mp_transform::cache::{CacheTree, Event, NoGetters, NoSplit, Node, SpecEntry, Traversal};

const C_ID: usize = 0x08;
const C_REFS: usize = 0x40;
const C_MODE: usize = 0x68;
const C_PARENT: usize = 0x70;
const C_ATTACH: usize = 0xa0;
const C_LIVE: usize = 0xa8;
const C_REGISTERED: usize = 0xd1;
const C_SPEC: usize = 0xd8;
const C_VC: usize = 0xe0;
const VC_SIZE: usize = 0x80;
const C_FLAGS: usize = 0x1e0;
const C_MASK: usize = 0x1e4;
const C_COUNT: usize = 0x1e8;
const C_ATTENTION: usize = 0x1ec;
const VC_PARENT: usize = 0x00;
const VC_KIDS: usize = 0x10;
const VC_ATT: usize = 0x38;
const VC_DIRTY: usize = 0x3c;
const SLOT_IS_CLASS: usize = 573;
const CLASS_NONE: u32 = 0x79;
const CLASS_SECTOR: u32 = 0x58;
const MAX_STACK: usize = 99;
const MAX_ENTRIES: usize = 64;
const MAX_DEPTH: usize = 64;

/// Engine functions the executor calls (C layout, filled by the probe).
#[repr(C)]
#[derive(Clone, Copy)]
pub struct EngineApi {
    /// `PropagateCacheFlags` 0x3d46b0.
    pub propagate: unsafe extern "C" fn(c: *mut u8, idx: u32, flags: *const u32),
    /// `RemoveCacheSpecification` 0x3d5440.
    pub remove_spec: unsafe extern "C" fn(c: *mut u8, kind: i32),
}

/// A component address. Engine memory is only read while the MoveWorker waits for the load.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
struct Ptr(usize);
unsafe impl Send for Ptr {}
unsafe impl Sync for Ptr {}

impl Ptr {
    fn null(self) -> bool {
        self.0 == 0
    }
    unsafe fn at(self, off: usize) -> *const u8 {
        unsafe { (self.0 as *const u8).add(off) }
    }
    unsafe fn u32(self, off: usize) -> u32 {
        unsafe { std::ptr::read_unaligned(self.at(off) as *const u32) }
    }
    unsafe fn u64(self, off: usize) -> u64 {
        unsafe { std::ptr::read_unaligned(self.at(off) as *const u64) }
    }
    unsafe fn ptr(self, off: usize) -> Ptr {
        Ptr(unsafe { self.u64(off) } as usize)
    }
    unsafe fn byte(self, off: usize) -> u8 {
        unsafe { *self.at(off) }
    }
    unsafe fn is_class(self, cls: u32) -> bool {
        unsafe {
            let vtbl = self.ptr(0);
            let f: unsafe extern "C" fn(*const u8, i32) -> u8 = std::mem::transmute(vtbl.u64(SLOT_IS_CLASS * 8) as usize);
            f(self.0 as *const u8, cls as i32) != 0
        }
    }
}

/// Specification list: (entries, count). Inline when the capacity (+0x80) is 4.
unsafe fn spec_entries(spec: Ptr) -> (Ptr, usize) {
    unsafe {
        let cap = spec.u64(0x80);
        let n = spec.u64(0x88) as usize;
        (if cap == 4 { spec } else { spec.ptr(0) }, n)
    }
}

unsafe fn read_entry(e: Ptr) -> SpecEntry {
    unsafe {
        SpecEntry {
            kind: e.u32(0) as i32,
            flags: e.u32(4),
            class: e.u32(8),
            truncated: e.byte(0xc) != 0,
            interval: f64::from_bits(e.u64(0x10)),
            last: f64::from_bits(e.u64(0x18)),
        }
    }
}

/// Non-null entries of a bucketed component container; `attach` = the attachment form
/// (iterated only when its count is non-zero, like 0x314fa0).
unsafe fn container(cont: Ptr, attach: bool) -> Option<Vec<Ptr>> {
    if cont.null() {
        return None;
    }
    unsafe {
        let count = cont.u32(0x20);
        let (b, e) = (cont.u64(0x08) as usize, cont.u64(0x10) as usize);
        let mut out = Vec::new();
        if attach && count == 0 {
            return Some(out);
        }
        let mut bucket = b;
        while bucket < e {
            let (p, end) = (Ptr(bucket).u64(0) as usize, Ptr(bucket).u64(8) as usize);
            let mut q = p;
            while q < end {
                let c = Ptr(q).ptr(0);
                if !c.null() {
                    out.push(c);
                }
                q += 8;
            }
            bucket += 0x20;
        }
        Some(out)
    }
}

#[derive(Clone)]
struct Level {
    entries: Ptr,
    n: usize,
    trunc: Vec<bool>,
}

#[derive(Default)]
struct Raw {
    kids: Option<Vec<Ptr>>,
    live: Option<Vec<Ptr>>,
    attach: Option<Vec<Ptr>>,
}

struct Task {
    root: Ptr,
    levels: Vec<Level>,
    total: usize,
    trunc_total: usize,
    depth: usize,
}

/// One loader (the sequential part, or one sector task).
struct Loader {
    idx: usize,
    order: Vec<Ptr>,
    raw: HashMap<Ptr, Raw>,
    levels: Vec<Level>,
    total: usize,
    trunc_total: usize,
    split: bool,
    tasks: Vec<Task>,
    error: Option<&'static str>,
}

impl Loader {
    fn new(idx: usize, split: bool) -> Loader {
        Loader {
            idx,
            order: Vec::new(),
            raw: HashMap::new(),
            levels: Vec::new(),
            total: 0,
            trunc_total: 0,
            split,
            tasks: Vec::new(),
            error: None,
        }
    }

    fn record(&mut self, c: Ptr) {
        if !self.raw.contains_key(&c) {
            self.raw.insert(c, Raw::default());
            self.order.push(c);
        }
    }

    unsafe fn walk(&mut self, c: Ptr, depth: usize, top: bool) {
        if self.error.is_some() {
            return;
        }
        if depth > MAX_DEPTH {
            self.error = Some("tree deeper than 64");
            return;
        }
        if self.split && !top && unsafe { c.is_class(CLASS_SECTOR) } {
            self.record(c);
            self.tasks.push(Task {
                root: c,
                levels: self.levels.clone(),
                total: self.total,
                trunc_total: self.trunc_total,
                depth,
            });
            return;
        }
        self.record(c);
        let count = unsafe { c.u32(C_COUNT) } as i32;
        if self.total == self.trunc_total && count == 0 {
            return;
        }
        let spec = unsafe { c.ptr(C_SPEC) };
        let pushed = !spec.null();
        if pushed {
            if self.levels.len() == MAX_STACK {
                self.error = Some("specification stack overrun");
                return;
            }
            let (entries, n) = unsafe { spec_entries(spec) };
            if n > MAX_ENTRIES {
                self.error = Some("specification list longer than 64");
                return;
            }
            let trunc = (0..n).map(|i| unsafe { Ptr(entries.0 + i * 0x20).byte(0xc) != 0 }).collect();
            self.levels.push(Level { entries, n, trunc });
            self.total += n;
        }
        let mut mine = Vec::new();
        for s in 0..self.levels.len() {
            for i in 0..self.levels[s].n {
                if self.levels[s].trunc[i] {
                    continue;
                }
                let cls = unsafe { Ptr(self.levels[s].entries.0 + i * 0x20).u32(8) };
                if cls != CLASS_NONE && unsafe { c.is_class(cls) } {
                    self.levels[s].trunc[i] = true;
                    self.trunc_total += 1;
                    mine.push((s, i));
                }
            }
        }
        let vc = Ptr(c.0 + C_VC + self.idx * VC_SIZE);
        let kids: Vec<Ptr> = unsafe {
            let (b, e) = (vc.u64(VC_KIDS) as usize, vc.u64(VC_KIDS + 8) as usize);
            (b..e).step_by(8).map(|q| Ptr(q).ptr(0)).collect()
        };
        let live = if unsafe { vc.u32(VC_DIRTY) } & 0x2 != 0 { unsafe { container(c.ptr(C_LIVE), false) } } else { None };
        let attach = unsafe { container(c.ptr(C_ATTACH), true) };
        let walk: Vec<Ptr> = kids.iter().chain(live.iter().flatten()).copied().collect();
        let attached: Vec<Ptr> = attach.iter().flatten().copied().collect();
        if let Some(r) = self.raw.get_mut(&c) {
            r.kids = Some(kids);
            r.live = live;
            r.attach = attach;
        }
        for k in walk {
            unsafe { self.walk(k, depth + 1, false) };
        }
        for a in attached {
            self.record(a);
        }
        for (s, i) in mine {
            self.levels[s].trunc[i] = false;
            self.trunc_total -= 1;
        }
        if pushed {
            let l = self.levels.pop().unwrap();
            self.total -= l.n;
        }
    }
}

/// The loaded copy: tree (index = position in `ptrs`, plus an "outside" node at the end).
pub struct Snapshot {
    /// The copy.
    pub tree: CacheTree,
    /// Engine address per node (without the outside node).
    pub ptrs: Vec<usize>,
    /// Sector tasks used.
    pub tasks: usize,
}

/// Loads the copy (see the module docs). `threads <= 1` loads sequentially.
///
/// # Safety
/// `galaxy` must be the engine's galaxy passed to `BuildCache`, on the MoveWorker inside the hook,
/// with no engine thread writing component state for the duration.
pub unsafe fn load(galaxy: usize, idx: usize, threads: usize, drop_pose_blocks: bool) -> Result<Snapshot, &'static str> {
    let root = Ptr(galaxy);
    let mut main = Loader::new(idx, threads > 1);
    unsafe { main.walk(root, 0, true) };
    if let Some(e) = main.error {
        return Err(e);
    }
    // sector tasks
    let tasks = std::mem::take(&mut main.tasks);
    let results: Vec<Mutex<Option<Loader>>> = (0..tasks.len()).map(|_| Mutex::new(None)).collect();
    let next = AtomicUsize::new(0);
    let run = || loop {
        let t = next.fetch_add(1, Ordering::Relaxed);
        if t >= tasks.len() {
            return;
        }
        let task = &tasks[t];
        let mut l = Loader::new(idx, false);
        l.levels = task.levels.clone();
        l.total = task.total;
        l.trunc_total = task.trunc_total;
        unsafe { l.walk(task.root, task.depth, true) };
        *results[t].lock().unwrap() = Some(l);
    };
    if threads <= 1 || tasks.len() <= 1 {
        run();
    } else {
        std::thread::scope(|s| {
            for _ in 0..threads.min(tasks.len()) {
                s.spawn(&run);
            }
        });
    }
    let mut order = main.order;
    let mut raw = main.raw;
    for r in results {
        let l = r.into_inner().unwrap().unwrap();
        if let Some(e) = l.error {
            return Err(e);
        }
        for p in l.order {
            let mut lr = l.raw.get(&p).map(|x| Raw { kids: x.kids.clone(), live: x.live.clone(), attach: x.attach.clone() });
            match raw.get_mut(&p) {
                None => {
                    raw.insert(p, lr.take().unwrap_or_default());
                    order.push(p);
                }
                Some(existing) => {
                    if existing.kids.is_none() {
                        if let Some(x) = lr {
                            *existing = x;
                        }
                    }
                }
            }
        }
    }
    // live parent chains
    let mut i = 0;
    while i < order.len() {
        let mut p = unsafe { order[i].ptr(C_PARENT) };
        let mut guard = 0;
        while !p.null() && guard < MAX_DEPTH && !raw.contains_key(&p) {
            raw.insert(p, Raw::default());
            order.push(p);
            p = unsafe { p.ptr(C_PARENT) };
            guard += 1;
        }
        i += 1;
    }
    // the copy
    let index: HashMap<Ptr, usize> = order.iter().enumerate().map(|(i, p)| (*p, i)).collect();
    let n = order.len();
    let outside = n;
    let map = |p: Ptr| -> Option<usize> { if p.null() { None } else { Some(*index.get(&p).unwrap_or(&outside)) } };
    let mut classes = HashSet::new();
    let mut nodes = Vec::with_capacity(n + 1);
    for &c in &order {
        let r = &raw[&c];
        let mut node = unsafe {
            Node {
                id: c.u64(C_ID),
                parent: map(c.ptr(C_PARENT)),
                flags: c.u32(C_FLAGS),
                mask: c.u32(C_MASK),
                count: c.u32(C_COUNT) as i32,
                registered: c.byte(C_REGISTERED) != 0,
                mode: c.u32(C_MODE) as i32,
                attention: c.u32(C_ATTENTION) as i32,
                refs: c.u32(C_REFS) as i32,
                ..Node::default()
            }
        };
        for k in 0..2 {
            let vc = Ptr(c.0 + C_VC + k * VC_SIZE);
            unsafe {
                node.dirty[k] = vc.u32(VC_DIRTY);
                node.vc_attention[k] = vc.u32(VC_ATT) as i32;
                node.vc_parent[k] = map(vc.ptr(VC_PARENT));
            }
        }
        node.children[idx] = r.kids.iter().flatten().map(|p| map(*p).unwrap_or(outside)).collect();
        node.live_children = r.live.as_ref().map(|v| v.iter().map(|p| map(*p).unwrap_or(outside)).collect());
        node.attach = r.attach.as_ref().map(|v| v.iter().map(|p| map(*p).unwrap_or(outside)).collect());
        let spec = unsafe { c.ptr(C_SPEC) };
        if !spec.null() {
            let (entries, n) = unsafe { spec_entries(spec) };
            let list: Vec<SpecEntry> = (0..n).map(|i| unsafe { read_entry(Ptr(entries.0 + i * 0x20)) }).collect();
            for e in &list {
                if e.class != CLASS_NONE && e.class < 128 {
                    classes.insert(e.class);
                }
            }
            node.spec = Some(list);
        }
        nodes.push(node);
    }
    for (i, &c) in order.iter().enumerate() {
        for &cls in &classes {
            if unsafe { c.is_class(cls) } {
                nodes[i].classes |= 1u128 << cls;
            }
        }
    }
    nodes.push(Node { id: u64::MAX, ..Node::default() });
    let ptrs = order.iter().map(|p| p.0).collect();
    Ok(Snapshot { tree: CacheTree { nodes, drop_pose_blocks }, ptrs, tasks: tasks.len() })
}

/// What [`build`] did.
#[derive(Default, Debug, Clone, Copy)]
pub struct BuildStats {
    /// Components copied.
    pub components: usize,
    /// Sector tasks used for loading.
    pub tasks: usize,
    /// Refresh calls executed.
    pub refreshes: usize,
    /// Specification lists whose timers were written.
    pub timers: usize,
    /// Entries removed.
    pub removed: usize,
}

/// The plan of one frame: refresh calls and list updates, in walk order.
pub struct Plan {
    steps: Vec<Step>,
    stats: BuildStats,
}

enum Step {
    Refresh { ptr: usize, flags: u32 },
    List { ptr: usize, keep: Vec<SpecEntry>, remove: Vec<i32> },
}

/// Loads and plans (no engine writes).
///
/// # Safety
/// As [`load`].
pub unsafe fn plan(galaxy: usize, idx: usize, time: f64, threads: usize, drop_pose_blocks: bool) -> Result<Plan, &'static str> {
    let snap = unsafe { load(galaxy, idx, threads, drop_pose_blocks)? };
    let mut tree = snap.tree.clone();
    let mut g = NoGetters;
    let log = {
        let mut t = Traversal::new(&mut tree, &mut g, NoSplit, idx, time);
        t.build_cache(0).map_err(|_| "model stack overrun (the original aborts the same way)")?;
        t.log
    };
    let mut last_end: HashMap<u32, usize> = HashMap::new();
    for (i, e) in log.iter().enumerate() {
        if let Event::EndLoop { node } = e {
            last_end.insert(*node, i);
        }
    }
    let n = snap.ptrs.len();
    let mut steps = Vec::new();
    let mut stats = BuildStats { components: n, tasks: snap.tasks, ..BuildStats::default() };
    for (i, e) in log.iter().enumerate() {
        match *e {
            Event::Propagate { node, flags, .. } => {
                let node = node as usize;
                if node >= n {
                    return Err("refresh of a component outside the copy");
                }
                steps.push(Step::Refresh { ptr: snap.ptrs[node], flags });
                stats.refreshes += 1;
            }
            Event::EndLoop { node } if last_end.get(&node) == Some(&i) => {
                let c = node as usize;
                if c >= n {
                    return Err("list of a component outside the copy");
                }
                let before = snap.tree.nodes[c].spec.clone().unwrap_or_default();
                let after = tree.nodes[c].spec.clone().unwrap_or_default();
                // Execution addresses entries by kind. Engine lists hold one entry per kind
                // (AddCacheSpecification merges), so a repeated kind, or any change beyond timers and
                // removals, is refused and the original runs.
                let mut kinds: Vec<i32> = before.iter().map(|b| b.kind).collect();
                kinds.sort_unstable();
                if kinds.windows(2).any(|w| w[0] == w[1]) {
                    return Err("specification list with a repeated kind");
                }
                let kept: Vec<&SpecEntry> = before.iter().filter(|b| after.iter().any(|a| a.kind == b.kind)).collect();
                if kept.len() != after.len()
                    || kept.iter().zip(&after).any(|(b, a)| {
                        b.kind != a.kind || b.flags != a.flags || b.class != a.class || b.truncated != a.truncated
                    })
                {
                    return Err("specification list changed beyond timers and removals");
                }
                let remove: Vec<i32> = before.iter().filter(|b| !after.iter().any(|a| a.kind == b.kind)).map(|b| b.kind).collect();
                stats.timers += 1;
                stats.removed += remove.len();
                steps.push(Step::List { ptr: snap.ptrs[c], keep: after, remove });
            }
            _ => {}
        }
    }
    Ok(Plan { steps, stats })
}

/// Executes a plan through the engine (see the module docs).
///
/// # Safety
/// On the MoveWorker, at the point where the original `BuildCache` would run, for the frame the
/// plan was made for.
pub unsafe fn execute(p: &Plan, idx: usize, api: &EngineApi) -> BuildStats {
    for s in &p.steps {
        match s {
            Step::Refresh { ptr, flags } => {
                let f = *flags;
                unsafe { (api.propagate)(*ptr as *mut u8, idx as u32, &f) };
            }
            Step::List { ptr, keep, remove } => {
                let c = Ptr(*ptr);
                unsafe {
                    let spec = c.ptr(C_SPEC);
                    if !spec.null() {
                        let (entries, n) = spec_entries(spec);
                        for i in 0..n {
                            let e = Ptr(entries.0 + i * 0x20);
                            let kind = e.u32(0) as i32;
                            if let Some(k) = keep.iter().find(|k| k.kind == kind) {
                                std::ptr::write_unaligned(e.at(0x10) as *mut u64, k.interval.to_bits());
                                std::ptr::write_unaligned(e.at(0x18) as *mut u64, k.last.to_bits());
                            }
                        }
                    }
                    for &kind in remove {
                        (api.remove_spec)(*ptr as *mut u8, kind);
                    }
                }
            }
        }
    }
    p.stats
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Fake components with the engine layout: 0x400 bytes, vtable at +0, class mask at +0x3f0.
    struct Fake {
        mem: Vec<Box<[u64; 128]>>,
        vtbl: Box<[usize; 640]>,
        lists: Vec<Box<[u64; 64]>>,
    }

    unsafe extern "C" fn fake_is_class(this: *const u8, cls: i32) -> u8 {
        let mask = unsafe { std::ptr::read_unaligned(this.add(0x3f0) as *const u128) };
        ((mask >> (cls as u32 & 127)) & 1) as u8
    }

    impl Fake {
        fn new() -> Fake {
            let mut vtbl = Box::new([0usize; 640]);
            vtbl[SLOT_IS_CLASS] = fake_is_class as usize;
            Fake { mem: Vec::new(), vtbl, lists: Vec::new() }
        }
        fn add(&mut self, parent: Option<usize>, classes: &[u32]) -> usize {
            let mut c = Box::new([0u64; 128]);
            c[0] = self.vtbl.as_ptr() as u64;
            let mask: u128 = classes.iter().fold(0, |a, &k| a | 1u128 << k);
            c[0x3f0 / 8] = mask as u64;
            c[0x3f8 / 8] = (mask >> 64) as u64;
            self.mem.push(c);
            let i = self.mem.len() - 1;
            if let Some(p) = parent {
                let pp = self.ptr(p);
                self.set(i, C_PARENT, pp);
            }
            i
        }
        fn ptr(&self, i: usize) -> u64 {
            self.mem[i].as_ptr() as u64
        }
        fn set(&mut self, i: usize, off: usize, v: u64) {
            self.mem[i][off / 8] = v;
        }
        fn set32(&mut self, i: usize, off: usize, v: u32) {
            let w = &mut self.mem[i][off / 8];
            let shift = (off % 8) * 8;
            *w = (*w & !(0xffff_ffffu64 << shift)) | ((v as u64) << shift);
        }
        /// child vector of cache idx
        fn kids(&mut self, i: usize, idx: usize, kids: &[usize]) {
            let mut list = Box::new([0u64; 64]);
            for (k, &c) in kids.iter().enumerate() {
                list[k] = self.ptr(c);
            }
            let b = list.as_ptr() as u64;
            self.lists.push(list);
            let vc = C_VC + idx * VC_SIZE;
            self.set(i, vc + VC_KIDS, b);
            self.set(i, vc + VC_KIDS + 8, b + kids.len() as u64 * 8);
        }
        /// one-entry inline specification list
        fn spec(&mut self, i: usize, kind: i32, flags: u32, class: u32) {
            let mut list = Box::new([0u64; 64]);
            list[0] = (kind as u32 as u64) | ((flags as u64) << 32);
            list[1] = class as u64;
            list[2] = 0f64.to_bits();
            list[3] = 0f64.to_bits();
            list[0x80 / 8] = 4;
            list[0x88 / 8] = 1;
            let b = list.as_ptr() as u64;
            self.lists.push(list);
            self.set(i, C_SPEC, b);
        }
    }

    #[test]
    fn loads_and_plans_a_small_universe() {
        let mut f = Fake::new();
        let g = f.add(None, &[0x2f]);
        let cl = f.add(Some(g), &[0x0f]);
        let s = f.add(Some(cl), &[0x58]);
        let z = f.add(Some(s), &[0x6d]);
        let ship = f.add(Some(z), &[0x49]);
        f.kids(g, 0, &[cl]);
        f.kids(cl, 0, &[s]);
        f.kids(s, 0, &[z]);
        f.kids(z, 0, &[ship]);
        f.kids(ship, 0, &[]);
        f.spec(g, 1, 0x6b, 0x58);
        f.spec(z, 3, 0x7f, CLASS_NONE);
        for (i, c) in [(g, 2), (cl, 2), (s, 2), (z, 1)] {
            f.set32(i, C_COUNT, c);
        }
        for i in [g, cl, s, z, ship] {
            f.set32(i, C_VC + VC_DIRTY, 0x8);
        }
        for threads in [1usize, 4] {
            let p = unsafe { plan(f.ptr(g) as usize, 0, 1.0, threads, true) }.expect("plan");
            assert_eq!(p.stats.components, 5, "threads {threads}");
            // galaxy entry flags reach cluster, sector, zone; the zone entry reaches the ship
            assert_eq!(p.stats.refreshes, 4, "threads {threads}");
            assert_eq!(p.stats.timers, 2);
        }
    }
}
