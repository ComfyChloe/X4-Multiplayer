//! Fork experiment, phase 1: measure what `fork()` costs the running server.
//!
//! A trigger file next to the agent log (`<log>.fork`, containing a count, default 1)
//! requests forks. Each fork happens on the MoveWorker thread inside the frame callback,
//! the same place a later snapshot fork would happen. The child calls `_exit(0)` at
//! once: it takes no locks, writes nothing and runs no exit handlers. The parent records
//! the fork duration; the worker thread reaps the child and logs one FORKPROBE line with
//! the duration, the process RSS and the copy-on-write fault cost over the next second.

use std::sync::atomic::{AtomicI32, AtomicU32, AtomicU64, Ordering};

extern "C" {
    fn fork() -> i32;
    fn _exit(code: i32) -> !;
    fn waitpid(pid: i32, status: *mut i32, options: i32) -> i32;
}

const WNOHANG: i32 = 1;

/// Forks still requested by the trigger file; consumed by the frame callback.
static PENDING: AtomicU32 = AtomicU32::new(0);
/// Result of the last fork done in the frame callback, for the worker to log.
static LAST_CHILD: AtomicI32 = AtomicI32::new(0);
static LAST_FORK_NS: AtomicU64 = AtomicU64::new(0);
static FORK_SEQ: AtomicU64 = AtomicU64::new(0);
static FORK_FAILURES: AtomicU64 = AtomicU64::new(0);

/// Parse the trigger file body: empty means one fork; otherwise 1..=1000.
pub fn parse_count(text: &str) -> Option<u32> {
    let t = text.trim();
    if t.is_empty() {
        return Some(1);
    }
    t.parse::<u32>().ok().filter(|n| (1..=1000).contains(n))
}

pub fn request(count: u32) {
    PENDING.store(count, Ordering::SeqCst);
}

pub fn pending() -> u32 {
    PENDING.load(Ordering::SeqCst)
}

/// Frame callback (MoveWorker thread, not loading): perform at most one requested fork.
pub fn on_frame() {
    if PENDING.load(Ordering::Relaxed) == 0 {
        return;
    }
    if PENDING.fetch_sub(1, Ordering::SeqCst) == 0 {
        PENDING.store(0, Ordering::SeqCst);
        return;
    }
    let started = std::time::Instant::now();
    // SAFETY: the child only calls the async-signal-safe `_exit`, so the state of any
    // lock or allocator copied from other threads is irrelevant to it.
    let pid = unsafe { fork() };
    if pid == 0 {
        unsafe { _exit(0) }
    }
    let ns = started.elapsed().as_nanos() as u64;
    if pid < 0 {
        FORK_FAILURES.fetch_add(1, Ordering::SeqCst);
        return;
    }
    LAST_FORK_NS.store(ns, Ordering::SeqCst);
    LAST_CHILD.store(pid, Ordering::SeqCst);
    FORK_SEQ.fetch_add(1, Ordering::SeqCst);
}

/// Process statistics from /proc/self: (RSS in MiB, minor faults).
pub fn proc_stats() -> (u64, u64) {
    let rss_pages = std::fs::read_to_string("/proc/self/statm")
        .ok()
        .and_then(|s| s.split_whitespace().nth(1).and_then(|v| v.parse::<u64>().ok()))
        .unwrap_or(0);
    let minflt = std::fs::read_to_string("/proc/self/stat")
        .ok()
        .and_then(|s| {
            // Field 10 (minflt), counted after the parenthesised command name.
            let rest = &s[s.rfind(')')? + 2..];
            rest.split_whitespace().nth(7).and_then(|v| v.parse::<u64>().ok())
        })
        .unwrap_or(0);
    (rss_pages * 4096 / (1024 * 1024), minflt)
}

/// Worker-side state: reaps children and builds log lines.
#[derive(Default)]
pub struct Reporter {
    seen: u64,
    failures: u64,
    awaiting: Vec<(u64, i32, u64, u64, u64, std::time::Instant)>,
}

impl Reporter {
    /// Called about once per worker tick; returns lines to log.
    pub fn poll(&mut self) -> Vec<String> {
        let mut lines = Vec::new();
        let failures = FORK_FAILURES.load(Ordering::SeqCst);
        if failures != self.failures {
            self.failures = failures;
            lines.push(format!("X4MP_AGENT FORKPROBE failed=1 failures_total={failures}"));
        }
        let seq = FORK_SEQ.load(Ordering::SeqCst);
        if seq != self.seen {
            self.seen = seq;
            let (rss, minflt) = proc_stats();
            self.awaiting.push((
                seq,
                LAST_CHILD.load(Ordering::SeqCst),
                LAST_FORK_NS.load(Ordering::SeqCst),
                rss,
                minflt,
                std::time::Instant::now(),
            ));
        }
        let mut keep = Vec::new();
        for (seq, pid, ns, rss, minflt, at) in self.awaiting.drain(..) {
            if at.elapsed().as_secs_f64() < 1.0 {
                keep.push((seq, pid, ns, rss, minflt, at));
                continue;
            }
            let mut status = 0;
            // SAFETY: reaps only the specific child this probe created.
            let reaped = unsafe { waitpid(pid, &mut status, WNOHANG) };
            let (_, minflt_after) = proc_stats();
            lines.push(format!(
                "X4MP_AGENT FORKPROBE n={seq} child={pid} fork_ms={:.3} rss_mb={rss} cow_minflt_1s={} child_reaped={} child_status={status} pending={}",
                ns as f64 / 1e6,
                minflt_after.saturating_sub(minflt),
                u8::from(reaped == pid),
                pending()
            ));
        }
        self.awaiting = keep;
        lines
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trigger_counts_are_bounded() {
        assert_eq!(parse_count(""), Some(1));
        assert_eq!(parse_count(" 25\n"), Some(25));
        assert_eq!(parse_count("0"), None);
        assert_eq!(parse_count("1001"), None);
        assert_eq!(parse_count("x"), None);
    }

    #[test]
    fn fork_in_a_test_process_exits_child_and_reports() {
        request(1);
        on_frame();
        assert_eq!(pending(), 0);
        assert!(FORK_SEQ.load(Ordering::SeqCst) >= 1);
        let mut reporter = Reporter::default();
        assert!(reporter.poll().is_empty());
        std::thread::sleep(std::time::Duration::from_millis(1100));
        let lines = reporter.poll();
        assert_eq!(lines.len(), 1, "{lines:?}");
        assert!(lines[0].contains("child_reaped=1 child_status=0"), "{}", lines[0]);
        let (rss, _) = proc_stats();
        assert!(rss > 0);
    }
}
