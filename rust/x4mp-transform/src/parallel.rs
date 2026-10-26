//! Per-sector parallel view-cache build with an exact merge (candidate A3 of
//! `x4-re/coord/reports/parallel-sim.md`, made exact).
//!
//! The engine's `BuildCache` is a single depth-first walk over global stacks. It can be
//! split per subtree without changing any result because, inside a subtree:
//!
//! - the inherited specification entries are only *read* (their truncation flags are
//!   path-local and restored on the way back up), so each task gets private copies taken
//!   at the moment the sequential walk would enter the subtree;
//! - every write is to components of the subtree, except two commutative counters on
//!   other components: reference counts (+0x40, interlocked in the engine) and the
//!   specification counts (+0x1e8) of the subtree root's ancestors when an entry expires.
//!   Both are deferred and added after the join.
//!
//! The split is exact only if no component is reachable from two tasks, or touched by the
//! sequential part after the point where the sequential walk would have run the task
//! (child vectors can be stale for a frame after a reparent, so the live tree and the
//! cached tree can disagree), and only if expiries inside a task change counts along the
//! task root's own ancestor chain. Each case is detected and reported as a [`Hazard`]; the
//! frame then runs sequentially on an untouched copy.
//!
//! The merged event log is identical to the sequential one: task logs are spliced in at the
//! positions the sequential walk would have produced them. `tests/parallel.rs` checks this,
//! and the final component state, for 1..8 threads on random universes.
//!
//! This is a model: the engine-side version runs the same plan with the engine's own
//! `PropagateCacheFlags` on worker threads (see `reports/REWRITE-TRANSFORMS.md` for what
//! that additionally requires: thread-local game time, deferred reference releases).

use crate::cache::{
    frame, CacheTree, Event, NoGetters, NoSplit, Node, Overrun, Split, Stacks, Store, StoreView, Traversal,
    GALAXY_ROOT_FLAGS,
};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

/// Why a parallel frame fell back to the sequential walk.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Hazard {
    /// A component is reachable from two tasks.
    Shared { node: usize },
    /// A component of a task is touched by the sequential part after the task's position.
    Late { node: usize },
    /// An expiry inside a task changes the count of a component outside the task root's
    /// ancestor chain.
    ForeignCount { node: usize },
    /// The engine would abort (MAX_CACHE_SPECIFICATIONS / MAX_CACHE_TRUNCATIONS).
    Overrun(Overrun),
}

/// Result of [`frame_parallel`].
#[derive(Clone, Debug)]
pub struct ParallelFrame {
    /// The event log, identical to the sequential walk's.
    pub log: Vec<Event>,
    /// Subtrees that ran as tasks (0 after a fallback).
    pub tasks: usize,
    /// Why the frame fell back, if it did.
    pub fallback: Option<Hazard>,
}

/// Splits at components of a class. The decision is the scheduler's, so no engine
/// `IsClass` call is logged for it.
struct SplitAt(u32);
impl Split for SplitAt {
    fn split(&mut self, store: &dyn StoreView, node: usize, _: &Stacks) -> bool {
        store.view(node).is_class(self.0)
    }
}

/// The whole tree, recording every component access in order (for the hazard check).
/// Reference-count updates are commutative and not recorded.
struct Recording<'a> {
    tree: &'a mut CacheTree,
    seen: RefCell<Vec<usize>>,
}
impl Store for Recording<'_> {
    fn node(&self, i: usize) -> &Node {
        self.seen.borrow_mut().push(i);
        &self.tree.nodes[i]
    }
    fn node_mut(&mut self, i: usize) -> &mut Node {
        self.seen.get_mut().push(i);
        &mut self.tree.nodes[i]
    }
    fn add_up(&mut self, start: usize, delta: i32) {
        let mut c = Some(start);
        while let Some(n) = c {
            self.seen.get_mut().push(n);
            let node = &mut self.tree.nodes[n];
            node.count = node.count.wrapping_add(delta);
            c = node.parent;
        }
    }
    fn add_refs(&mut self, node: usize, delta: i32) {
        self.tree.nodes[node].refs += delta;
    }
    fn drop_pose_blocks(&self) -> bool {
        self.tree.drop_pose_blocks
    }
    fn access_mark(&self) -> usize {
        self.seen.borrow().len()
    }
}

/// A task's own components; writes to anything else are deferred.
struct TaskStore {
    nodes: HashMap<usize, Node>,
    counts: Vec<(usize, i32)>,
    refs: Vec<(usize, i32)>,
    drop: bool,
}
impl Store for TaskStore {
    fn node(&self, i: usize) -> &Node {
        self.nodes.get(&i).expect("task read a component outside its closure")
    }
    fn node_mut(&mut self, i: usize) -> &mut Node {
        self.nodes.get_mut(&i).expect("task wrote a component outside its closure")
    }
    fn add_up(&mut self, start: usize, delta: i32) {
        let mut c = Some(start);
        while let Some(n) = c {
            match self.nodes.get_mut(&n) {
                Some(node) => {
                    node.count = node.count.wrapping_add(delta);
                    c = node.parent;
                }
                None => {
                    self.counts.push((n, delta));
                    return;
                }
            }
        }
    }
    fn add_refs(&mut self, node: usize, delta: i32) {
        match self.nodes.get_mut(&node) {
            Some(n) => n.refs += delta,
            None => self.refs.push((node, delta)),
        }
    }
    fn drop_pose_blocks(&self) -> bool {
        self.drop
    }
}

/// Every component a task can read or write: everything reachable through the child
/// vector of `idx` or the live child container (a child refresh switches to it), plus the
/// attachments of those components (refreshed, not descended into).
fn closure(tree: &CacheTree, root: usize, idx: usize) -> Vec<usize> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let mut stack = vec![root];
    while let Some(n) = stack.pop() {
        if !seen.insert(n) {
            continue;
        }
        out.push(n);
        let node = &tree.nodes[n];
        stack.extend(node.children[idx].iter().rev().copied());
        if let Some(live) = &node.live_children {
            stack.extend(live.iter().rev().copied());
        }
        if let Some(att) = &node.attach {
            for &a in att {
                if seen.insert(a) {
                    out.push(a);
                }
            }
        }
    }
    out
}

struct TaskResult {
    nodes: HashMap<usize, Node>,
    counts: Vec<(usize, i32)>,
    refs: Vec<(usize, i32)>,
    log: Vec<Event>,
    error: Option<Overrun>,
}

/// One view-cache frame (galaxy root refresh, then `BuildCache`), with the subtrees rooted
/// at components of `split_class` (0x58 = sector) run as tasks on up to `threads` threads.
/// The log and the final state are identical to [`frame`]'s; on a [`Hazard`] the frame is
/// run sequentially instead and the hazard is reported.
pub fn frame_parallel(
    tree: &mut CacheTree,
    root: usize,
    idx: usize,
    time: f64,
    threads: usize,
    split_class: u32,
) -> ParallelFrame {
    let backup = tree.clone();
    match try_parallel(tree, root, idx, time, threads.max(1), split_class) {
        Ok((log, tasks)) => ParallelFrame { log, tasks, fallback: None },
        Err(h) => {
            *tree = backup;
            let log = frame(tree, root, idx, time, &mut NoGetters).unwrap_or_default();
            ParallelFrame { log, tasks: 0, fallback: Some(h) }
        }
    }
}

fn try_parallel(
    tree: &mut CacheTree,
    root: usize,
    idx: usize,
    time: f64,
    threads: usize,
    split_class: u32,
) -> Result<(Vec<Event>, usize), Hazard> {
    // Phase 1: the sequential walk, handing off the split subtrees.
    let mut getters = NoGetters;
    let mut rec = Recording { tree, seen: RefCell::new(Vec::new()) };
    let (log, slots) = {
        let mut t = Traversal::new(&mut rec, &mut getters, SplitAt(split_class), idx, time);
        let f = t.store.node(root).flags | GALAXY_ROOT_FLAGS;
        if f != 0 {
            t.propagate(root, f, false);
        }
        t.build_cache(root).map_err(Hazard::Overrun)?;
        (std::mem::take(&mut t.log), std::mem::take(&mut t.tasks))
    };
    let seen = rec.seen.into_inner();
    let tree = rec.tree;

    // Hazards: components shared between tasks, and sequential accesses after a task's
    // position to a component of that task.
    let mut owner: HashMap<usize, usize> = HashMap::new();
    let mut closures = Vec::with_capacity(slots.len());
    for (t, slot) in slots.iter().enumerate() {
        let c = closure(tree, slot.node, idx);
        for &m in &c {
            if owner.insert(m, t).is_some() {
                return Err(Hazard::Shared { node: m });
            }
        }
        closures.push(c);
    }
    for (t, slot) in slots.iter().enumerate() {
        if let Some(&m) = seen[slot.access_mark..].iter().find(|m| owner.get(m) == Some(&t)) {
            return Err(Hazard::Late { node: m });
        }
    }

    // Phase 2: run the tasks on worker threads; each owns its components.
    let drop = tree.drop_pose_blocks;
    let stores: Vec<Mutex<Option<TaskStore>>> = closures
        .iter()
        .map(|c| {
            let nodes = c.iter().map(|&m| (m, std::mem::take(&mut tree.nodes[m]))).collect();
            Mutex::new(Some(TaskStore { nodes, counts: Vec::new(), refs: Vec::new(), drop }))
        })
        .collect();
    let results: Vec<Mutex<Option<TaskResult>>> = (0..slots.len()).map(|_| Mutex::new(None)).collect();
    let next = AtomicUsize::new(0);
    let run = || loop {
        let t = next.fetch_add(1, Ordering::Relaxed);
        if t >= slots.len() {
            return;
        }
        let mut store = stores[t].lock().unwrap().take().unwrap();
        let mut g = NoGetters;
        let (log, error) = {
            let mut tr = Traversal::new(&mut store, &mut g, NoSplit, idx, time);
            tr.stacks = slots[t].stacks.clone();
            let error = tr.build_cache(slots[t].node).err();
            (tr.log, error)
        };
        *results[t].lock().unwrap() =
            Some(TaskResult { nodes: store.nodes, counts: store.counts, refs: store.refs, log, error });
    };
    if threads == 1 || slots.len() <= 1 {
        run();
    } else {
        std::thread::scope(|s| {
            for _ in 0..threads.min(slots.len()) {
                s.spawn(&run);
            }
        });
    }

    // Join: components back, deferred counters, logs spliced in walk order.
    let mut out = Vec::with_capacity(log.len());
    let mut from = 0;
    let mut deferred = Vec::new();
    let mut error = None;
    let mut foreign = None;
    for (t, slot) in slots.iter().enumerate() {
        let r = results[t].lock().unwrap().take().unwrap();
        let root_parent = r.nodes[&slot.node].parent;
        for (m, node) in r.nodes {
            tree.nodes[m] = node;
        }
        error = error.or(r.error);
        out.extend_from_slice(&log[from..slot.log_pos]);
        out.extend_from_slice(&r.log);
        from = slot.log_pos;
        for (m, d) in r.refs {
            tree.nodes[m].refs += d;
        }
        for (m, d) in r.counts {
            if Some(m) != root_parent {
                foreign = foreign.or(Some(m));
            }
            deferred.push((m, d));
        }
    }
    if let Some(e) = error {
        return Err(Hazard::Overrun(e));
    }
    if let Some(m) = foreign {
        return Err(Hazard::ForeignCount { node: m });
    }
    out.extend_from_slice(&log[from..]);
    for (m, d) in deferred {
        tree.add_up(m, d);
    }
    Ok((out, slots.len()))
}
