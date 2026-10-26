//! A small persistent worker pool (std only) for the walk's sector tasks: threads are created on
//! first use and kept, so a frame pays a wake-up, not thread creation.

use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock};

/// The job of one generation; only dereferenced while the caller of [`run`] waits for it.
#[derive(Clone, Copy)]
struct JobPtr(*const (dyn Fn() + Sync));
unsafe impl Send for JobPtr {}

struct State {
    generation: u64,
    job: Option<JobPtr>,
    /// helpers taking part in the current generation (ids below this)
    wanted: usize,
    /// helpers still inside the current job
    running: usize,
    workers: usize,
}

struct Shared {
    state: Mutex<State>,
    wake: Condvar,
    done: Condvar,
}

static POOL: OnceLock<Arc<Shared>> = OnceLock::new();
/// One job at a time (the MoveWorker is the only caller in game; tests run in parallel).
static RUN: Mutex<()> = Mutex::new(());

fn shared() -> &'static Arc<Shared> {
    POOL.get_or_init(|| {
        Arc::new(Shared {
            state: Mutex::new(State { generation: 0, job: None, wanted: 0, running: 0, workers: 0 }),
            wake: Condvar::new(),
            done: Condvar::new(),
        })
    })
}

fn worker(sh: Arc<Shared>, id: usize) {
    let mut seen = 0;
    loop {
        let job = {
            let mut st = sh.state.lock().unwrap_or_else(|e| e.into_inner());
            loop {
                if st.generation != seen {
                    seen = st.generation;
                    if id < st.wanted {
                        break st.job;
                    }
                }
                st = sh.wake.wait(st).unwrap_or_else(|e| e.into_inner());
            }
        };
        if let Some(JobPtr(f)) = job {
            // a panic inside the job leaves its result slot empty; the caller reports it
            let _ = catch_unwind(AssertUnwindSafe(|| unsafe { (*f)() }));
        }
        let mut st = sh.state.lock().unwrap_or_else(|e| e.into_inner());
        st.running -= 1;
        if st.running == 0 {
            sh.done.notify_all();
        }
    }
}

/// `f` over `items` on up to `threads` threads (the caller is one of them); results in item
/// order. `None` if a call panicked on a helper thread. Calls must not themselves call `run`.
pub fn run<T: Sync, R: Send>(threads: usize, items: &[T], f: impl Fn(&T) -> R + Sync) -> Option<Vec<R>> {
    let n = items.len();
    let helpers = threads.saturating_sub(1).min(n.saturating_sub(1));
    if helpers == 0 {
        return Some(items.iter().map(&f).collect());
    }
    let _one = RUN.lock().unwrap_or_else(|e| e.into_inner());
    let slots: Vec<Mutex<Option<R>>> = (0..n).map(|_| Mutex::new(None)).collect();
    let next = AtomicUsize::new(0);
    let body = || loop {
        let i = next.fetch_add(1, Ordering::Relaxed);
        if i >= n {
            return;
        }
        let r = f(&items[i]);
        *slots[i].lock().unwrap_or_else(|e| e.into_inner()) = Some(r);
    };
    let job: &(dyn Fn() + Sync) = &body;
    // SAFETY of the lifetime erasure: the helpers only call the job between the generation bump
    // below and `running` reaching 0, and this function does not return (or unwind) before that.
    let job: JobPtr = JobPtr(unsafe { std::mem::transmute::<&(dyn Fn() + Sync), &'static (dyn Fn() + Sync)>(job) });
    let sh = shared();
    {
        let mut st = sh.state.lock().unwrap_or_else(|e| e.into_inner());
        while st.workers < helpers {
            let id = st.workers;
            let s = Arc::clone(sh);
            if std::thread::Builder::new().name(format!("x4mp-xform-{id}")).spawn(move || worker(s, id)).is_err() {
                break;
            }
            st.workers += 1;
        }
        let helpers = helpers.min(st.workers);
        st.job = Some(job);
        st.wanted = helpers;
        st.running = helpers;
        st.generation += 1;
    }
    sh.wake.notify_all();
    let mine = catch_unwind(AssertUnwindSafe(&body));
    {
        let mut st = sh.state.lock().unwrap_or_else(|e| e.into_inner());
        while st.running > 0 {
            st = sh.done.wait(st).unwrap_or_else(|e| e.into_inner());
        }
        st.job = None;
    }
    if let Err(p) = mine {
        resume_unwind(p);
    }
    slots.into_iter().map(|s| s.into_inner().unwrap_or_else(|e| e.into_inner())).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runs_in_order_across_generations() {
        for threads in [1usize, 2, 4, 8] {
            for round in 0..50usize {
                let items: Vec<usize> = (0..(round % 13) + 1).collect();
                let out = run(threads, &items, |&x| x * 3 + round).unwrap();
                assert_eq!(out, items.iter().map(|&x| x * 3 + round).collect::<Vec<_>>());
            }
        }
    }

    #[test]
    fn a_panic_on_a_helper_is_reported() {
        let helper_started = std::sync::atomic::AtomicBool::new(false);
        let items: Vec<usize> = (0..64).collect();
        let out = run(4, &items, |&x| {
            if std::thread::current().name().is_some_and(|n| n.starts_with("x4mp-xform-")) {
                helper_started.store(true, Ordering::Release);
                panic!("boom");
            }
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while !helper_started.load(Ordering::Acquire) {
                assert!(std::time::Instant::now() < deadline, "helper did not start");
                std::thread::yield_now();
            }
            x
        });
        assert!(out.is_none(), "a helper must execute and its missing result must be reported");
        // the pool still works
        assert_eq!(run(4, &items, |&x| x).unwrap(), items);
    }

    #[test]
    fn caller_panic_waits_for_helpers_before_borrowed_job_expires() {
        use std::sync::atomic::AtomicBool;
        let active = AtomicUsize::new(0);
        let helper_started = AtomicBool::new(false);
        let items: Vec<usize> = (0..64).collect();
        let result = catch_unwind(AssertUnwindSafe(|| run(2, &items, |&x| {
            if std::thread::current().name().is_some_and(|n| n.starts_with("x4mp-xform-")) {
                active.fetch_add(1, Ordering::SeqCst);
                helper_started.store(true, Ordering::Release);
                std::thread::sleep(std::time::Duration::from_millis(1));
                active.fetch_sub(1, Ordering::SeqCst);
                x
            } else {
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                while !helper_started.load(Ordering::Acquire) {
                    assert!(std::time::Instant::now() < deadline, "helper did not start");
                    std::thread::yield_now();
                }
                panic!("caller failed while helper was using the borrowed job");
            }
        })));
        assert!(result.is_err());
        assert!(helper_started.load(Ordering::Acquire));
        assert_eq!(active.load(Ordering::SeqCst), 0);
        assert_eq!(run(2, &items, |&x| x).unwrap(), items);
    }
}
