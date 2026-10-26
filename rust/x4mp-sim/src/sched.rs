//! Scheduler: persistent worker pool with a frame barrier, the [`System`] trait and the
//! per-sector job context.
//!
//! Determinism does not depend on the pool: every job mutates only its own world and outbox,
//! reads other sectors only through the phase snapshot, and the barrier consumes outboxes in
//! sector order. Which thread runs which sector, and in which order, only affects timing.

use std::any::Any;
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;

use crate::bus::{Envelope, Outbox};
use crate::comp::RngState;
use crate::id::{EntityId, IdMap, SectorIx};
use crate::rng::EngineLcg;
use crate::snapshot::Snapshot;
use crate::universe::Universe;
use crate::world::World;

/// Frame timing as the engine provides it (TLS game time +0x300, previous +0x328).
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct FrameInfo {
    pub frame: u64,
    pub game_time: f64,
    pub dt: f64,
}

/// A replaced engine system. `run_sector` runs once per sector per frame, on any thread, in
/// any order relative to other sectors; `barrier` runs once afterwards on the calling thread.
pub trait System: Send + Sync {
    fn name(&self) -> &'static str;
    fn run_sector(&self, ctx: &mut SectorCtx<'_>);
    /// Serial hook after the barrier; receives the `Target::Barrier` messages addressed to
    /// this system in total order (sector order, then emission order).
    fn barrier(&self, _ctx: &mut BarrierCtx<'_>) {}
}

/// What a sector job may touch.
pub struct SectorCtx<'a> {
    pub frame: &'a FrameInfo,
    pub sector: SectorIx,
    /// This sector's world (values may be mutated; structure only through `out`).
    pub world: &'a mut World,
    /// Locations at phase start (unchanged during the phase).
    pub ids: &'a IdMap,
    /// Published columns of all sectors at phase start.
    pub snapshot: &'a Snapshot,
    /// Messages addressed to this system in this sector, routed by barriers since its last
    /// run, in total order.
    pub inbox: &'a [Envelope],
    pub out: &'a mut Outbox,
    pub session_seed: u64,
}

impl SectorCtx<'_> {
    /// Row of an entity if it lives in this sector.
    pub fn row_of(&self, e: EntityId) -> Option<usize> {
        self.ids.loc(e).filter(|l| l.sector == self.sector).map(|l| l.row as usize)
    }

    /// The per-entity stream of a row (requires the `RngState` component).
    pub fn rng(&mut self, row: usize) -> &mut EngineLcg {
        self.world.col_mut::<RngState>()[row].lcg()
    }

    /// The inbox messages of type `M`, in delivery order.
    pub fn messages<'s, M: crate::bus::Message>(&'s self) -> impl Iterator<Item = (&'s Envelope, &'s M)> + 's {
        self.inbox.iter().filter_map(|e| e.get::<M>().map(|m| (e, m)))
    }
}

/// Serial barrier context.
pub struct BarrierCtx<'a> {
    pub frame: &'a FrameInfo,
    pub universe: &'a mut Universe,
    /// `Target::Barrier` messages addressed to this system, in total order.
    pub messages: &'a [Envelope],
}

/// Per-phase statistics. Timing fields are informational and never affect results.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct PhaseReport {
    pub system: &'static str,
    pub phase: u64,
    pub sectors: usize,
    pub parallel_ns: u64,
    pub barrier_ns: u64,
    pub sector_sum_ns: u64,
    pub sector_max_ns: u64,
    pub messages: u64,
    pub commands: u64,
    pub spawned: u64,
    pub despawned: u64,
    pub migrated: u64,
    pub command_errors: u64,
    pub dropped_messages: u64,
}

impl PhaseReport {
    /// sum / max of per-sector job time: the speedup an ideal scheduler could reach.
    pub fn ideal_speedup(&self) -> f64 {
        if self.sector_max_ns == 0 {
            0.0
        } else {
            self.sector_sum_ns as f64 / self.sector_max_ns as f64
        }
    }
}

/// Result of one `Universe::step`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FrameReport {
    pub frame: u64,
    pub phases: Vec<PhaseReport>,
    /// First few command errors (deterministic text).
    pub errors: Vec<String>,
}

// ---------------------------------------------------------------------------------------
// Worker pool

#[derive(Clone, Copy)]
struct Task {
    f: *const (dyn Fn(usize) + Sync),
    jobs: *const usize,
    njobs: usize,
}

// SAFETY: the pointers are only dereferenced while `run` keeps the referents alive and every
// worker has acknowledged the epoch (see `Pool::run_with`).
unsafe impl Send for Task {}
unsafe impl Sync for Task {}

struct Ctrl {
    epoch: u64,
    task: Option<Task>,
    shutdown: bool,
    panic: Option<Box<dyn Any + Send>>,
}

struct Shared {
    ctrl: Mutex<Ctrl>,
    start: Condvar,
    done: Condvar,
    epoch: AtomicU64,
    next: AtomicUsize,
    active: AtomicUsize,
    busy: std::sync::atomic::AtomicBool,
    spin: u32,
}

/// Persistent worker pool. `threads` counts the calling thread: `Pool::new(1)` runs
/// everything inline, `Pool::new(16)` starts 15 workers.
pub struct Pool {
    threads: usize,
    shared: Arc<Shared>,
    handles: Vec<JoinHandle<()>>,
}

impl Pool {
    /// `threads == 0` uses the machine's available parallelism.
    pub fn new(threads: usize) -> Pool {
        Pool::with_spin(threads, 2_000)
    }

    /// Like `new`, with the number of spin iterations a worker polls before sleeping.
    pub fn with_spin(threads: usize, spin: u32) -> Pool {
        let threads =
            if threads == 0 { std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1) } else { threads };
        let shared = Arc::new(Shared {
            ctrl: Mutex::new(Ctrl { epoch: 0, task: None, shutdown: false, panic: None }),
            start: Condvar::new(),
            done: Condvar::new(),
            epoch: AtomicU64::new(0),
            next: AtomicUsize::new(0),
            active: AtomicUsize::new(0),
            busy: std::sync::atomic::AtomicBool::new(false),
            spin,
        });
        let handles = (1..threads)
            .map(|i| {
                let s = Arc::clone(&shared);
                std::thread::Builder::new()
                    .name(format!("x4mp-sim-{i}"))
                    .spawn(move || worker(s))
                    .expect("spawn x4mp-sim worker")
            })
            .collect();
        Pool { threads, shared, handles }
    }

    pub fn threads(&self) -> usize {
        self.threads
    }

    /// Runs `f(j)` exactly once for every `j` in `pinned` (on the calling thread, in order)
    /// and in `jobs` (any thread, claimed in slice order). Returns after all have finished.
    /// A panic in a job is re-raised here after every job has finished.
    /// Not reentrant: calling it from inside a job (or from two threads at once) panics.
    pub fn run_with(&self, pinned: &[usize], jobs: &[usize], f: &(dyn Fn(usize) + Sync)) {
        if self.shared.busy.swap(true, Ordering::Acquire) {
            panic!("Pool::run_with is not reentrant");
        }
        struct Release<'a>(&'a std::sync::atomic::AtomicBool);
        impl Drop for Release<'_> {
            fn drop(&mut self) {
                self.0.store(false, Ordering::Release);
            }
        }
        let _release = Release(&self.shared.busy);
        if self.handles.is_empty() || jobs.len() <= 1 {
            for &j in pinned.iter().chain(jobs) {
                f(j);
            }
            return;
        }
        // SAFETY: erase the borrow lifetimes; `f` and `jobs` outlive this call, and the call
        // returns only after every worker has finished this epoch (`active == 0`), so no
        // worker dereferences the task afterwards.
        let f_static: &'static (dyn Fn(usize) + Sync) = unsafe { std::mem::transmute(f) };
        let task = Task { f: f_static as *const _, jobs: jobs.as_ptr(), njobs: jobs.len() };
        self.shared.next.store(0, Ordering::Relaxed);
        self.shared.active.store(self.handles.len(), Ordering::Relaxed);
        {
            let mut g = self.shared.ctrl.lock().unwrap_or_else(|p| p.into_inner());
            g.task = Some(task);
            g.epoch += 1;
            self.shared.epoch.store(g.epoch, Ordering::Release);
        }
        self.shared.start.notify_all();
        let mut first_panic: Option<Box<dyn Any + Send>> = None;
        for &j in pinned {
            if let Err(p) = catch_unwind(AssertUnwindSafe(|| f(j))) {
                first_panic.get_or_insert(p);
            }
        }
        if let Some(p) = claim_jobs(&self.shared, task) {
            first_panic.get_or_insert(p);
        }
        let mut g = self.shared.ctrl.lock().unwrap_or_else(|p| p.into_inner());
        while self.shared.active.load(Ordering::Acquire) != 0 {
            g = self.shared.done.wait(g).unwrap_or_else(|p| p.into_inner());
        }
        g.task = None;
        let worker_panic = g.panic.take();
        drop(g);
        if let Some(p) = first_panic.or(worker_panic) {
            resume_unwind(p);
        }
    }

    /// Runs `f` on every item exactly once: `pinned` indices on the calling thread first,
    /// then `order` across the pool. `pinned ++ order` must be a permutation of the indices.
    pub fn for_each_mut<T: Send>(
        &self,
        items: &mut [T],
        pinned: &[usize],
        order: &[usize],
        f: &(dyn Fn(&mut T) + Sync),
    ) {
        let n = items.len();
        assert_eq!(pinned.len() + order.len(), n, "job lists must cover every item once");
        let mut seen = vec![false; n];
        for &i in pinned.iter().chain(order) {
            assert!(i < n && !seen[i], "job index {i} out of range or repeated");
            seen[i] = true;
        }
        let base = SendPtr(items.as_mut_ptr());
        let run = move |i: usize| {
            let base = base;
            // SAFETY: every index is in range and handed out exactly once (checked above),
            // so each &mut is disjoint; `items` outlives `run_with`.
            let item = unsafe { &mut *base.0.add(i) };
            f(item);
        };
        self.run_with(pinned, order, &run);
    }
}

struct SendPtr<T>(*mut T);
impl<T> Clone for SendPtr<T> {
    fn clone(&self) -> Self {
        *self
    }
}
impl<T> Copy for SendPtr<T> {}
// SAFETY: used only for disjoint per-index access in `for_each_mut`.
unsafe impl<T: Send> Send for SendPtr<T> {}
unsafe impl<T: Send> Sync for SendPtr<T> {}

fn claim_jobs(shared: &Shared, task: Task) -> Option<Box<dyn Any + Send>> {
    let mut first = None;
    loop {
        let i = shared.next.fetch_add(1, Ordering::Relaxed);
        if i >= task.njobs {
            break;
        }
        // SAFETY: see `Task`; i < njobs.
        let (f, job) = unsafe { (&*task.f, *task.jobs.add(i)) };
        if let Err(p) = catch_unwind(AssertUnwindSafe(|| f(job))) {
            first.get_or_insert(p);
        }
    }
    first
}

fn worker(shared: Arc<Shared>) {
    let mut seen = 0u64;
    loop {
        // Short spin before sleeping keeps per-phase wake-up latency low.
        let mut spins = 0u32;
        while shared.epoch.load(Ordering::Acquire) == seen && spins < shared.spin {
            std::hint::spin_loop();
            spins += 1;
        }
        let task = {
            let mut g = shared.ctrl.lock().unwrap_or_else(|p| p.into_inner());
            while g.epoch == seen && !g.shutdown {
                g = shared.start.wait(g).unwrap_or_else(|p| p.into_inner());
            }
            if g.shutdown {
                return;
            }
            seen = g.epoch;
            g.task.expect("task published with the epoch")
        };
        if let Some(p) = claim_jobs(&shared, task) {
            let mut g = shared.ctrl.lock().unwrap_or_else(|p| p.into_inner());
            g.panic.get_or_insert(p);
        }
        if shared.active.fetch_sub(1, Ordering::AcqRel) == 1 {
            let _g = shared.ctrl.lock().unwrap_or_else(|p| p.into_inner());
            shared.done.notify_all();
        }
    }
}

impl Drop for Pool {
    fn drop(&mut self) {
        {
            let mut g = self.shared.ctrl.lock().unwrap_or_else(|p| p.into_inner());
            g.shutdown = true;
        }
        self.shared.start.notify_all();
        for h in self.handles.drain(..) {
            let _ = h.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicU32;

    #[test]
    fn every_job_runs_exactly_once() {
        for threads in [1, 2, 4, 16] {
            let pool = Pool::new(threads);
            for round in 0..50 {
                let n = 1 + round % 37;
                let hits: Vec<AtomicU32> = (0..n).map(|_| AtomicU32::new(0)).collect();
                let jobs: Vec<usize> = (0..n).rev().collect();
                pool.run_with(&[], &jobs, &|j| {
                    hits[j].fetch_add(1, Ordering::Relaxed);
                });
                assert!(hits.iter().all(|h| h.load(Ordering::Relaxed) == 1), "threads={threads} n={n}");
            }
        }
    }

    #[test]
    fn pinned_jobs_run_on_the_calling_thread() {
        let pool = Pool::new(4);
        let me = std::thread::current().id();
        let mut items: Vec<(usize, Option<std::thread::ThreadId>)> = (0..12).map(|i| (i, None)).collect();
        pool.for_each_mut(&mut items, &[3, 7], &[0, 1, 2, 4, 5, 6, 8, 9, 10, 11], &|it| {
            it.1 = Some(std::thread::current().id());
        });
        assert_eq!(items[3].1, Some(me));
        assert_eq!(items[7].1, Some(me));
        assert!(items.iter().all(|it| it.1.is_some()));
    }

    #[test]
    fn job_panic_is_reraised_after_all_jobs() {
        let pool = Pool::new(4);
        let count = AtomicU32::new(0);
        let jobs: Vec<usize> = (0..20).collect();
        let r = catch_unwind(AssertUnwindSafe(|| {
            pool.run_with(&[], &jobs, &|j| {
                count.fetch_add(1, Ordering::Relaxed);
                if j == 5 {
                    panic!("job 5");
                }
            });
        }));
        assert!(r.is_err());
        assert_eq!(count.load(Ordering::Relaxed), 20, "other jobs still ran");
        // The pool stays usable.
        let again = AtomicU32::new(0);
        pool.run_with(&[], &jobs, &|_| {
            again.fetch_add(1, Ordering::Relaxed);
        });
        assert_eq!(again.load(Ordering::Relaxed), 20);
    }

    #[test]
    #[should_panic(expected = "repeated")]
    fn for_each_mut_rejects_duplicate_indices() {
        let pool = Pool::new(2);
        let mut items = vec![0u32; 3];
        pool.for_each_mut(&mut items, &[], &[0, 1, 1], &|v| *v += 1);
    }
}
