//! The fast switch: `BuildCache` as an exact walk over engine memory.
//!
//! Where [`crate::engine`] copies everything the walk *could* reach and runs the model on the
//! copy, this walker reads engine memory directly and visits exactly what the original visits,
//! in the same order, with the same decisions (gate, specification stack with private truncation
//! flags, `IsClass` through vtable slot 573, timers), so it touches about the cache lines the
//! original touches. It plans instead of acting: refresh calls and end-loop list updates are
//! recorded in walk order and executed afterwards on the MoveWorker through the engine's own
//! `PropagateCacheFlags` and `RemoveCacheSpecification` (as in [`crate::engine::execute_steps`]).
//! With `threads > 1` the subtrees of sectors (IsClass 0x58) are walked as tasks on worker threads
//! (read-only), and their steps are spliced in at the position where the sequential walk would
//! have entered the sector.
//!
//! Planning reads the state before the build, so it is exact as long as nothing the walk reads
//! is changed by an earlier step of the same frame. What earlier steps change, and how that is
//! covered:
//! - a refresh with the child bit (0x2) rebuilds the component's child vector from its child
//!   container (+0xa8): emulated (the walk then iterates the container, as the original iterates
//!   the rebuilt vector); also for attachments, whose rebuilt vectors are remembered per walker;
//! - the end loop changes the owner's timers, its own flags (kind 7) and the counts on its live
//!   parent chain: read again if a component is visited twice in one frame. Refused: repeated
//!   open visits (including a duplicate entry in the same child vector), duplicate sector task
//!   roots, any open component (owners are always open) or rebuilt vector reached from a
//!   component that is not its live parent (+0x70), and any attachment with a rebuilt vector
//!   outside the walker's part of the tree. Closed visits change nothing the walk reads;
//! - dirty bits change, but whether a component is refreshed depends only on the specification
//!   stack and its +0x1e0, and only the child bit decides what is walked.
//!
//! Further refusals (nothing executed, the original runs): stack overruns (the original aborts),
//! a list with a repeated kind or more than 64 entries, a tree deeper than 64.

use crate::engine::{
    container, read_entry, spec_entries, BuildStats, EngineApi, Ptr, Step, CLASS_NONE, CLASS_SECTOR, C_ATTACH, C_COUNT,
    C_FLAGS, C_LIVE, C_PARENT, C_SPEC, C_VC, MAX_DEPTH, MAX_ENTRIES, MAX_STACK, VC_DIRTY, VC_KIDS, VC_SIZE,
};
use crate::pool;
use std::collections::HashSet;
use x4mp_transform::cache::SpecEntry;

const CHILDREN: u32 = 0x2;
const ALL: u32 = 0x7f;

#[derive(Clone, Copy)]
struct Level {
    entries: Ptr,
    n: usize,
    /// private truncation flags (bit per entry)
    trunc: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kids {
    /// the cached child vector of the cache being built (vc+0x10)
    Cached,
    /// the child container (+0xa8): the vector was rebuilt by a refresh earlier in this frame
    Live,
}

/// A sector subtree to walk on a worker thread, with the walk state where the sequential walk
/// would have entered it.
struct TaskSpec {
    root: Ptr,
    parent: Ptr,
    kids: Kids,
    levels: Vec<Level>,
    total: usize,
    truncs: Vec<(u16, u16)>,
    depth: usize,
}

/// What a walk saw (summed over the walkers).
#[derive(Default, Clone, Copy, Debug, PartialEq, Eq)]
pub struct WalkStats {
    /// Components visited (gate evaluated).
    pub visits: usize,
    /// Gates found open.
    pub opens: usize,
    /// Refresh calls planned.
    pub refreshes: usize,
    /// Lists whose end loop changed something.
    pub lists: usize,
    /// Entries expired.
    pub removed: usize,
    /// Sector tasks.
    pub tasks: usize,
}

impl WalkStats {
    fn add(&mut self, o: &WalkStats) {
        self.visits += o.visits;
        self.opens += o.opens;
        self.refreshes += o.refreshes;
        self.lists += o.lists;
        self.removed += o.removed;
        self.tasks += o.tasks;
    }
}

struct Walker {
    vc: usize,
    time: f64,
    base: u32,
    levels: Vec<Level>,
    total: usize,
    truncs: Vec<(u16, u16)>,
    steps: Vec<Step>,
    /// attachments whose child vector a refresh rebuilt (later visits iterate the container)
    rebuilt: Vec<Ptr>,
    /// Reject repeated open visits: pre-frame timers/counts are no longer valid on a revisit.
    opened: HashSet<Ptr>,
    /// sector this walker is confined to (tasks), None for the sequential part
    region: Option<Ptr>,
    split: bool,
    tasks: Vec<TaskSpec>,
    /// position in `steps` of each task's steps
    task_at: Vec<usize>,
    stats: WalkStats,
    error: Option<&'static str>,
}

impl Walker {
    fn new(idx: usize, time: f64, base: u32, split: bool, region: Option<Ptr>) -> Walker {
        Walker {
            vc: C_VC + idx * VC_SIZE,
            time,
            base,
            levels: Vec::with_capacity(16),
            total: 0,
            truncs: Vec::with_capacity(16),
            steps: Vec::new(),
            rebuilt: Vec::new(),
            opened: HashSet::new(),
            region,
            split,
            tasks: Vec::new(),
            task_at: Vec::new(),
            stats: WalkStats::default(),
            error: None,
        }
    }

    fn fail(&mut self, why: &'static str) {
        if self.error.is_none() {
            self.error = Some(why);
        }
    }

    /// `BuildCache(c)`, reached from `parent` (null for the root) with child vector `kids`.
    unsafe fn build(&mut self, c: Ptr, parent: Ptr, kids: Kids, depth: usize) {
        if self.error.is_some() {
            return;
        }
        if depth > MAX_DEPTH {
            return self.fail("tree deeper than 64");
        }
        self.stats.visits += 1;
        let count = unsafe { c.u32(C_COUNT) };
        if self.total == self.truncs.len() && count == 0 {
            return;
        }
        self.stats.opens += 1;
        if !self.opened.insert(c) {
            return self.fail("open component visited more than once");
        }
        if !parent.null() && unsafe { c.ptr(C_PARENT) } != parent {
            return self.fail("open component reached outside its live parent");
        }
        let spec = unsafe { c.ptr(C_SPEC) };
        let own = if spec.null() { None } else { Some(unsafe { spec_entries(spec) }) };
        if let Some((entries, n)) = own {
            if self.levels.len() == MAX_STACK {
                return self.fail("specification stack overrun");
            }
            if n > MAX_ENTRIES {
                return self.fail("specification list longer than 64");
            }
            let mut trunc = 0u64;
            for i in 0..n {
                if unsafe { Ptr(entries.0 + i * 0x20).byte(0xc) } != 0 {
                    trunc |= 1 << i;
                }
            }
            self.levels.push(Level { entries, n, trunc });
            self.total += n;
        }
        // the loop over every entry on the stack (0x3d4bdd..0x3d4cc0)
        let saved = self.truncs.len();
        let mut acc = self.base;
        for s in 0..self.levels.len() {
            let Level { entries, n, .. } = self.levels[s];
            for e in 0..n {
                if self.levels[s].trunc & (1 << e) != 0 {
                    continue;
                }
                let ent = Ptr(entries.0 + e * 0x20);
                let class = unsafe { ent.u32(8) };
                if class != CLASS_NONE && unsafe { c.is_class(class) } {
                    if self.truncs.len() == MAX_STACK {
                        return self.fail("truncation stack overrun");
                    }
                    self.levels[s].trunc |= 1 << e;
                    self.truncs.push((s as u16, e as u16));
                }
                let flags = unsafe { ent.u32(4) };
                if acc | flags != acc {
                    let interval = f64::from_bits(unsafe { ent.u64(0x10) });
                    let last = f64::from_bits(unsafe { ent.u64(0x18) });
                    if self.time >= last + interval {
                        acc |= flags;
                    }
                }
            }
        }
        // children: refresh, then recurse
        match kids {
            Kids::Cached => {
                let v = Ptr(c.0 + self.vc);
                let (b, e) = unsafe { (v.u64(VC_KIDS) as usize, v.u64(VC_KIDS + 8) as usize) };
                let mut q = b;
                while q < e {
                    let ch = unsafe { Ptr(q).ptr(0) };
                    unsafe { self.child(c, ch, acc, depth) };
                    if self.error.is_some() {
                        return;
                    }
                    q += 8;
                }
            }
            Kids::Live => {
                for ch in unsafe { container(c.ptr(C_LIVE), false) }.unwrap_or_default() {
                    unsafe { self.child(c, ch, acc, depth) };
                    if self.error.is_some() {
                        return;
                    }
                }
            }
        }
        for k in saved..self.truncs.len() {
            let (s, e) = self.truncs[k];
            self.levels[s as usize].trunc &= !(1 << e);
        }
        self.truncs.truncate(saved);
        // attachments get every refresh bit
        if let Some(att) = unsafe { container(c.ptr(C_ATTACH), true) } {
            for a in att {
                self.steps.push(Step::Refresh { ptr: a.0, flags: ALL });
                self.stats.refreshes += 1;
                if unsafe { Ptr(a.0 + self.vc).u32(VC_DIRTY) } & CHILDREN != 0 {
                    if !unsafe { self.in_region(a) } {
                        return self.fail("attachment vector rebuilt outside the walker's part of the tree");
                    }
                    self.rebuilt.push(a);
                }
            }
        }
        if let Some((entries, n)) = own {
            self.levels.pop();
            self.total -= n;
            unsafe { self.end_loop(c, entries, n) };
        }
    }

    /// One child of `c`: `f = acc | child+0x1e0`, refresh if non-zero, recurse (or split).
    unsafe fn child(&mut self, c: Ptr, ch: Ptr, acc: u32, depth: usize) {
        let f = acc | unsafe { ch.u32(C_FLAGS) };
        let mut kids = Kids::Cached;
        if f != 0 {
            self.steps.push(Step::Refresh { ptr: ch.0, flags: f });
            self.stats.refreshes += 1;
            if f & CHILDREN != 0 && unsafe { Ptr(ch.0 + self.vc).u32(VC_DIRTY) } & CHILDREN != 0 {
                if unsafe { ch.ptr(C_PARENT) } != c {
                    return self.fail("child vector rebuilt for a component reached outside its live parent");
                }
                kids = Kids::Live;
            }
        }
        if kids == Kids::Cached && !self.rebuilt.is_empty() && self.rebuilt.contains(&ch) {
            kids = Kids::Live;
        }
        if self.split && unsafe { ch.is_class(CLASS_SECTOR) } {
            self.task_at.push(self.steps.len());
            self.tasks.push(TaskSpec {
                root: ch,
                parent: c,
                kids,
                levels: self.levels.clone(),
                total: self.total,
                truncs: self.truncs.clone(),
                depth: depth + 1,
            });
            return;
        }
        unsafe { self.build(ch, c, kids, depth + 1) };
    }

    /// Is `a` inside this walker's part of the tree (its sector for a task, outside every sector
    /// for the sequential part)?
    unsafe fn in_region(&self, a: Ptr) -> bool {
        if !self.split && self.region.is_none() {
            return true; // one walker for the whole tree
        }
        let mut p = a;
        for _ in 0..=MAX_DEPTH {
            if p.null() {
                return self.region.is_none();
            }
            match self.region {
                Some(r) if p == r => return true,
                None if unsafe { p.is_class(CLASS_SECTOR) } => return false,
                _ => {}
            }
            p = unsafe { p.ptr(C_PARENT) };
        }
        false
    }

    /// The end loop over `c`'s own entries (0x3d4f5e..0x3d50de), planned.
    unsafe fn end_loop(&mut self, c: Ptr, entries: Ptr, n: usize) {
        let time = self.time;
        let mut keep: Vec<SpecEntry> = Vec::with_capacity(n);
        let mut remove = Vec::new();
        let mut changed = false;
        for i in 0..n {
            let e = unsafe { read_entry(Ptr(entries.0 + i * 0x20)) };
            if keep.iter().any(|k| k.kind == e.kind) || remove.contains(&e.kind) {
                return self.fail("specification list with a repeated kind");
            }
            let mut out = e;
            if !(e.interval + e.last > time) {
                out.last = time;
            }
            if e.interval >= -0.5 {
                if e.interval >= 0.0 {
                    changed |= out.last.to_bits() != e.last.to_bits();
                    keep.push(out);
                } else {
                    remove.push(e.kind);
                }
            } else {
                out.interval = e.interval + 0.5;
                changed = true;
                keep.push(out);
            }
        }
        if changed || !remove.is_empty() {
            self.stats.lists += 1;
            self.stats.removed += remove.len();
            self.steps.push(Step::List { ptr: c.0, keep, remove });
        }
    }
}

/// A planned frame: the sequential part's steps with the tasks' steps spliced in.
pub struct FastPlan {
    main: Vec<Step>,
    task_at: Vec<usize>,
    tasks: Vec<Vec<Step>>,
    /// What the walk saw.
    pub stats: WalkStats,
}

impl FastPlan {
    /// The steps in execution order.
    pub(crate) fn steps(&self) -> impl Iterator<Item = &Step> {
        let mut segs: Vec<&[Step]> = Vec::with_capacity(2 * self.tasks.len() + 1);
        let mut from = 0;
        for (k, &at) in self.task_at.iter().enumerate() {
            segs.push(&self.main[from..at]);
            segs.push(&self.tasks[k]);
            from = at;
        }
        segs.push(&self.main[from..]);
        segs.into_iter().flatten()
    }
}

/// Plans `BuildCache(galaxy, idx, time)` (see the module docs). Reads only.
///
/// # Safety
/// `galaxy` must be the engine's galaxy passed to `BuildCache`, on the MoveWorker inside the hook,
/// with no engine thread writing component state while it runs.
pub unsafe fn plan(galaxy: usize, idx: usize, time: f64, threads: usize, base_flags: u32) -> Result<FastPlan, &'static str> {
    let split = threads > 1;
    let mut main = Walker::new(idx, time, base_flags, split, None);
    unsafe { main.build(Ptr(galaxy), Ptr(0), Kids::Cached, 0) };
    if let Some(e) = main.error {
        return Err(e);
    }
    let specs = std::mem::take(&mut main.tasks);
    let mut roots = HashSet::with_capacity(specs.len());
    if specs.iter().any(|task| !roots.insert(task.root)) {
        return Err("sector task root visited more than once");
    }
    let mut stats = main.stats;
    stats.tasks = specs.len();
    let walk_task = |t: &TaskSpec| -> Walker {
        let mut w = Walker::new(idx, time, base_flags, false, Some(t.root));
        w.levels = t.levels.clone();
        w.total = t.total;
        w.truncs = t.truncs.clone();
        unsafe { w.build(t.root, t.parent, t.kids, t.depth) };
        w
    };
    let walked: Vec<Walker> = pool::run(threads, &specs, walk_task).ok_or("walk task panicked")?;
    let mut tasks = Vec::with_capacity(walked.len());
    for w in walked {
        if let Some(e) = w.error {
            return Err(e);
        }
        stats.add(&w.stats);
        tasks.push(w.steps);
    }
    Ok(FastPlan { main: main.steps, task_at: main.task_at, tasks, stats })
}

/// Executes a fast plan through the engine, in walk order.
///
/// # Safety
/// As [`crate::engine::execute`].
pub unsafe fn execute(p: &FastPlan, idx: usize, api: &EngineApi) -> BuildStats {
    for s in p.steps() {
        unsafe { crate::engine::execute_steps(std::slice::from_ref(s), idx, api) };
    }
    BuildStats {
        components: p.stats.visits,
        tasks: p.stats.tasks,
        refreshes: p.stats.refreshes,
        timers: p.stats.lists,
        removed: p.stats.removed,
    }
}
