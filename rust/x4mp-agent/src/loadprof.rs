//! Load profiler, layer 2: per-thread CPU, process I/O, memory and the engine loading
//! flag, sampled by the agent's worker thread from process start until the engine has
//! left loading (plus a tail). Platform independent: the worker reads `/proc` and the
//! loading flag and feeds [`ProcSample`]s in; this module produces the log lines.

use std::collections::{BTreeMap, HashMap};

use crate::procstat::{IoStat, Memory};

/// Engine state at a sample, as far as the agent may read it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LoadState {
    /// Not observed (build not verified yet, or reading not allowed).
    Unknown,
    /// Verified build, but the VBI table does not exist yet (engine still starting).
    NotReady,
    Idle,
    Loading,
}

impl LoadState {
    pub fn code(self) -> &'static str {
        match self {
            LoadState::Unknown => "-",
            LoadState::NotReady => "n",
            LoadState::Idle => "0",
            LoadState::Loading => "1",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ThreadSample {
    pub tid: u32,
    pub name: String,
    /// utime + stime in clock ticks, cumulative since the thread started.
    pub ticks: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ProcSample {
    /// Seconds since the process started (from `/proc/self/stat` starttime).
    pub t: f64,
    /// CLOCK_MONOTONIC seconds (the clock `perf record -k CLOCK_MONOTONIC` uses).
    pub mono: f64,
    /// Whole-process utime + stime in ticks (includes threads that already exited).
    pub proc_ticks: u64,
    pub threads: Vec<ThreadSample>,
    pub io: IoStat,
    pub mem: Memory,
    pub minflt: u64,
    pub majflt: u64,
    pub state: LoadState,
    /// Game time from the TimerBase, when readable.
    pub game_time: Option<f64>,
    /// Slot 3 holds the pinned GameClient object.
    pub game_client: bool,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Settings {
    /// Clock ticks per second (`sysconf(_SC_CLK_TCK)`, normally 100).
    pub clk_tck: f64,
    /// Keep sampling this long after the engine leaves loading.
    pub tail_s: f64,
    /// Stop regardless after this process age.
    pub max_s: f64,
    /// Threads listed per row.
    pub top: usize,
}

#[derive(Debug, Clone, Default)]
struct ThreadAcc {
    name: String,
    ticks: u64,
    load_ticks: u64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct Mark {
    t: f64,
    mono: f64,
}

#[derive(Debug)]
pub struct LoadProfiler {
    s: Settings,
    prev: Option<ProcSample>,
    prev_ticks: HashMap<u32, u64>,
    threads: BTreeMap<u32, ThreadAcc>,
    first: Option<Mark>,
    table: Option<Mark>,
    menu: Option<Mark>,
    load_start: Option<Mark>,
    load_end: Option<Mark>,
    first_sim: Option<Mark>,
    game_time_at_end: Option<f64>,
    load_wall: f64,
    load_proc_ticks: u64,
    peak_rss_kb: u64,
    samples: u64,
    done: bool,
}

/// Thread name usable inside one whitespace-separated log token.
pub fn token(name: &str) -> String {
    let t: String = name
        .chars()
        .map(|c| if c.is_whitespace() || matches!(c, ',' | '=' | ':' | '/') || c.is_control() { '_' } else { c })
        .collect();
    if t.is_empty() {
        "?".into()
    } else {
        t
    }
}

/// `JobThread 12` -> `JobThread`: a trailing number (and its separator) is removed. A
/// number glued to a short prefix stays (`X4`, `gl3`), so the process name survives.
pub fn thread_group(name: &str) -> String {
    let trimmed = name.trim_end_matches(|c: char| c.is_ascii_digit());
    if trimmed.len() == name.len() {
        return name.trim().to_string();
    }
    let group = trimmed.trim_end_matches([' ', '#', '_', ':', '-']).trim();
    let separated = group.len() < trimmed.len();
    if group.is_empty() || (!separated && group.chars().count() < 3) {
        name.trim().to_string()
    } else {
        group.to_string()
    }
}

fn opt(mark: Option<Mark>) -> String {
    mark.map_or("none".into(), |m| format!("{:.2}", m.t))
}

fn opt_mono(mark: Option<Mark>) -> String {
    mark.map_or("none".into(), |m| format!("{:.3}", m.mono))
}

const MB: f64 = 1024.0 * 1024.0;

impl LoadProfiler {
    pub fn new(s: Settings) -> Self {
        LoadProfiler {
            s,
            prev: None,
            prev_ticks: HashMap::new(),
            threads: BTreeMap::new(),
            first: None,
            table: None,
            menu: None,
            load_start: None,
            load_end: None,
            first_sim: None,
            game_time_at_end: None,
            load_wall: 0.0,
            load_proc_ticks: 0,
            peak_rss_kb: 0,
            samples: 0,
            done: false,
        }
    }

    pub fn done(&self) -> bool {
        self.done
    }

    /// Measured duration of the (last) load: loading flag set until it cleared.
    pub fn load_seconds(&self) -> Option<f64> {
        Some(self.load_end?.t - self.load_start?.t)
    }

    pub fn load_ended(&self) -> bool {
        self.load_end.is_some() && self.prev.as_ref().is_some_and(|p| p.state != LoadState::Loading)
    }

    /// Feed one sample; returns the `LOADPROF` row. After the end condition the
    /// profiler is done and further samples are ignored.
    pub fn sample(&mut self, cur: ProcSample) -> Option<String> {
        if self.done {
            return None;
        }
        self.samples += 1;
        let mark = Mark { t: cur.t, mono: cur.mono };
        self.first.get_or_insert(mark);
        let (prev_t, prev_proc, prev_io, prev_min, prev_maj, prev_state) = match &self.prev {
            Some(p) => (p.t, p.proc_ticks, p.io, p.minflt, p.majflt, p.state),
            // First sample: everything since process start.
            None => (0.0, 0, IoStat::default(), 0, 0, LoadState::Unknown),
        };
        let dt = (cur.t - prev_t).max(1e-6);
        let dproc = cur.proc_ticks.saturating_sub(prev_proc);

        // Per-thread deltas.
        let mut deltas: Vec<(u64, u32, &str)> = Vec::with_capacity(cur.threads.len());
        let mut next_ticks = HashMap::with_capacity(cur.threads.len());
        for th in &cur.threads {
            let before = self.prev_ticks.get(&th.tid).copied().unwrap_or(0);
            let d = th.ticks.saturating_sub(before);
            next_ticks.insert(th.tid, th.ticks);
            let acc = self.threads.entry(th.tid).or_default();
            acc.name.clone_from(&th.name);
            acc.ticks = acc.ticks.max(th.ticks);
            if cur.state == LoadState::Loading {
                acc.load_ticks += d;
            }
            deltas.push((d, th.tid, th.name.as_str()));
        }
        self.prev_ticks = next_ticks;
        deltas.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        let top: Vec<String> = deltas
            .iter()
            .take(self.s.top)
            .filter(|d| d.0 > 0)
            .map(|(d, tid, name)| format!("{}/{tid}:{:.2}", token(name), *d as f64 / self.s.clk_tck / dt))
            .collect();

        // Milestones.
        if !matches!(cur.state, LoadState::Unknown | LoadState::NotReady) && self.table.is_none() {
            self.table = Some(mark);
        }
        if cur.state == LoadState::Idle && cur.game_client && self.load_start.is_none() && self.menu.is_none() {
            self.menu = Some(mark);
        }
        if cur.state == LoadState::Loading {
            self.load_start.get_or_insert(mark);
            self.load_wall += dt;
            self.load_proc_ticks += dproc;
            // A new load cancels a pending end and first-frame mark.
            if prev_state != LoadState::Loading && self.load_end.is_some() {
                self.load_end = None;
                self.first_sim = None;
            }
        }
        if prev_state == LoadState::Loading && cur.state == LoadState::Idle {
            self.load_end = Some(mark);
            self.game_time_at_end = cur.game_time;
            self.first_sim = None;
        }
        if cur.state == LoadState::Idle && self.load_end.is_some() && self.first_sim.is_none() {
            if let (Some(g0), Some(g)) = (self.game_time_at_end, cur.game_time) {
                if g > g0 {
                    self.first_sim = Some(mark);
                }
            }
        }
        self.peak_rss_kb = self.peak_rss_kb.max(cur.mem.hwm_kb).max(cur.mem.rss_kb);

        let row = format!(
            "X4MP_AGENT LOADPROF t={:.3} mono={:.3} dt={:.3} load={} cpu={:.2} rchar_mb={:.1} rd_mb={:.1} wr_mb={:.1} rss_mb={} minflt={} majflt={} thr={} top={}",
            cur.t,
            cur.mono,
            dt,
            cur.state.code(),
            dproc as f64 / self.s.clk_tck / dt,
            cur.io.rchar.saturating_sub(prev_io.rchar) as f64 / MB,
            cur.io.read_bytes.saturating_sub(prev_io.read_bytes) as f64 / MB,
            cur.io.write_bytes.saturating_sub(prev_io.write_bytes) as f64 / MB,
            cur.mem.rss_kb / 1024,
            cur.minflt.saturating_sub(prev_min),
            cur.majflt.saturating_sub(prev_maj),
            cur.threads.len(),
            if top.is_empty() { "-".into() } else { top.join(",") },
        );
        let end = self.load_end.filter(|_| cur.state == LoadState::Idle).map(|m| m.t + self.s.tail_s);
        if end.is_some_and(|e| cur.t >= e) || cur.t >= self.s.max_s {
            self.done = true;
        }
        self.prev = Some(cur);
        Some(row)
    }

    /// Final lines: one `LOADPROF SUMMARY` and one `LOADPROF GROUP` per thread group.
    /// `reason` says why profiling ended (`complete`, `max_s`, `refused`, ...).
    pub fn summary(&mut self, reason: &str) -> Vec<String> {
        self.done = true;
        let Some(last) = &self.prev else {
            return vec![format!("X4MP_AGENT LOADPROF SUMMARY end={reason} samples=0")];
        };
        let tck = self.s.clk_tck;
        let wall = last.t.max(1e-6);
        let cpu = last.proc_ticks as f64 / tck;
        let load_cpu = self.load_proc_ticks as f64 / tck;
        let mut lines = vec![format!(
            "X4MP_AGENT LOADPROF SUMMARY end={reason} wall_s={wall:.2} first_sample_s={} table_s={} menu_s={} load_start_s={} load_end_s={} load_s={} first_sim_s={} load_start_mono={} load_end_mono={} first_sim_mono={} cpu_s={cpu:.2} parallelism={:.2} load_cpu_s={load_cpu:.2} load_parallelism={} rchar_mb={:.1} read_mb={:.1} write_mb={:.1} peak_rss_mb={} minflt={} majflt={} threads_seen={} samples={}",
            opt(self.first),
            opt(self.table),
            opt(self.menu),
            opt(self.load_start),
            opt(self.load_end),
            self.load_seconds().map_or("none".into(), |s| format!("{s:.2}")),
            opt(self.first_sim),
            opt_mono(self.load_start),
            opt_mono(self.load_end),
            opt_mono(self.first_sim),
            cpu / wall,
            if self.load_wall > 0.0 { format!("{:.2}", load_cpu / self.load_wall) } else { "none".into() },
            last.io.rchar as f64 / MB,
            last.io.read_bytes as f64 / MB,
            last.io.write_bytes as f64 / MB,
            self.peak_rss_kb / 1024,
            last.minflt,
            last.majflt,
            self.threads.len(),
            self.samples,
        )];
        // Groups: (threads, ticks, load_ticks, busiest thread ticks).
        let mut groups: BTreeMap<String, (u32, u64, u64, u64)> = BTreeMap::new();
        for acc in self.threads.values() {
            let g = groups.entry(thread_group(&acc.name)).or_default();
            g.0 += 1;
            g.1 += acc.ticks;
            g.2 += acc.load_ticks;
            g.3 = g.3.max(acc.ticks);
        }
        let total: u64 = groups.values().map(|g| g.1).sum::<u64>().max(1);
        let mut sorted: Vec<_> = groups.into_iter().filter(|g| g.1 .1 > 0).collect();
        sorted.sort_by(|a, b| b.1 .1.cmp(&a.1 .1).then(a.0.cmp(&b.0)));
        for (name, (n, ticks, load_ticks, busiest)) in sorted {
            lines.push(format!(
                "X4MP_AGENT LOADPROF GROUP name={} threads={n} cpu_s={:.2} load_cpu_s={:.2} share={:.3} busiest_thread_cpu_s={:.2}",
                token(&name),
                ticks as f64 / tck,
                load_ticks as f64 / tck,
                ticks as f64 / total as f64,
                busiest as f64 / tck,
            ));
        }
        lines
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings() -> Settings {
        Settings { clk_tck: 100.0, tail_s: 10.0, max_s: 900.0, top: 3 }
    }

    fn th(tid: u32, name: &str, ticks: u64) -> ThreadSample {
        ThreadSample { tid, name: name.into(), ticks }
    }

    fn sample(t: f64, state: LoadState, proc_ticks: u64, threads: Vec<ThreadSample>) -> ProcSample {
        ProcSample {
            t,
            mono: 1000.0 + t,
            proc_ticks,
            threads,
            io: IoStat { rchar: (t * 10.0 * MB) as u64, read_bytes: (t * MB) as u64, ..Default::default() },
            mem: Memory { rss_kb: 1024 * (100 + t as u64), hwm_kb: 1024 * (100 + t as u64) },
            minflt: (t * 100.0) as u64,
            majflt: 0,
            state,
            game_time: Some(0.0),
            game_client: !matches!(state, LoadState::Unknown | LoadState::NotReady),
        }
    }

    #[test]
    fn groups_and_tokens() {
        assert_eq!(thread_group("JobThread 12"), "JobThread");
        assert_eq!(thread_group("JobThread#3"), "JobThread");
        assert_eq!(thread_group("Movement worker"), "Movement worker");
        assert_eq!(thread_group("X4"), "X4");
        assert_eq!(thread_group("Worker12"), "Worker");
        assert_eq!(thread_group("gl3"), "gl3");
        assert_eq!(thread_group("1234"), "1234");
        assert_eq!(token("Movement worker"), "Movement_worker");
        assert_eq!(token("a,b=c:d/e"), "a_b_c_d_e");
        assert_eq!(token(""), "?");
    }

    #[test]
    fn rows_report_interval_deltas_and_top_threads() {
        let mut p = LoadProfiler::new(settings());
        let row =
            p.sample(sample(0.5, LoadState::Unknown, 40, vec![th(1, "X4", 30), th(2, "x4mp-agent", 10)])).unwrap();
        // First row covers process start .. 0.5 s.
        assert!(row.starts_with("X4MP_AGENT LOADPROF t=0.500 mono=1000.500 dt=0.500 load=- cpu=0.80 "), "{row}");
        assert!(row.contains(" top=X4/1:0.60,x4mp-agent/2:0.20"), "{row}");
        let row = p
            .sample(sample(
                0.75,
                LoadState::Loading,
                90,
                vec![th(1, "X4", 40), th(2, "x4mp-agent", 10), th(3, "JobThread 1", 40)],
            ))
            .unwrap();
        assert!(row.contains(" dt=0.250 load=1 cpu=2.00 rchar_mb=2.5 "), "{row}");
        assert!(row.contains(" thr=3 top=JobThread_1/3:1.60,X4/1:0.40"), "{row}");
        assert!(!row.contains("x4mp-agent/2"), "idle threads are not listed: {row}");
    }

    #[test]
    fn milestones_and_end_after_tail() {
        let mut p = LoadProfiler::new(settings());
        let mut ticks = 0;
        let mut t = 0.0;
        let mut feed = |p: &mut LoadProfiler, state: LoadState, game: f64| {
            t += 0.25;
            ticks += 40; // 1.6 cores busy
            let mut s = sample(
                t,
                state,
                ticks,
                vec![th(1, "X4", ticks / 2), th(5, "JobThread 0", ticks / 4), th(6, "JobThread 1", ticks / 4)],
            );
            s.game_time = Some(game);
            p.sample(s);
        };
        for _ in 0..4 {
            feed(&mut p, LoadState::NotReady, 0.0);
        }
        for _ in 0..4 {
            feed(&mut p, LoadState::Idle, 0.0); // menu
        }
        for _ in 0..40 {
            feed(&mut p, LoadState::Loading, 0.0); // 10 s of loading
        }
        assert!(!p.load_ended());
        feed(&mut p, LoadState::Idle, 5.0); // load ends; game time 5 at end
        assert!(p.load_ended());
        assert_eq!(p.load_seconds(), Some(10.0));
        feed(&mut p, LoadState::Idle, 5.0);
        feed(&mut p, LoadState::Idle, 5.1); // first simulated frame
        for _ in 0..38 {
            assert!(!p.done());
            feed(&mut p, LoadState::Idle, 6.0);
        }
        assert!(p.done(), "10 s tail after the load end at 12.25 s");
        let lines = p.summary("complete");
        let s = &lines[0];
        assert!(
            s.contains(" table_s=1.25 menu_s=1.25 load_start_s=2.25 load_end_s=12.25 load_s=10.00 first_sim_s=12.75 "),
            "{s}"
        );
        assert!(s.contains(" load_start_mono=1002.250 load_end_mono=1012.250 "), "{s}");
        assert!(s.contains(" cpu_s=35.60 parallelism=1.60 load_cpu_s=16.00 load_parallelism=1.60 "), "{s}");
        assert!(s.contains(" peak_rss_mb=122 ") && s.contains(" threads_seen=3 samples=89"), "{s}");
        assert_eq!(lines[1], "X4MP_AGENT LOADPROF GROUP name=JobThread threads=2 cpu_s=17.80 load_cpu_s=8.00 share=0.500 busiest_thread_cpu_s=8.90");
        assert_eq!(lines[2], "X4MP_AGENT LOADPROF GROUP name=X4 threads=1 cpu_s=17.80 load_cpu_s=8.00 share=0.500 busiest_thread_cpu_s=17.80");
        assert_eq!(p.sample(sample(99.0, LoadState::Idle, 1, vec![])), None);
    }

    #[test]
    fn a_second_load_postpones_the_end_and_max_s_caps() {
        let mut p = LoadProfiler::new(Settings { tail_s: 1.0, max_s: 5.0, ..settings() });
        p.sample(sample(0.25, LoadState::Loading, 1, vec![]));
        p.sample(sample(0.5, LoadState::Idle, 1, vec![]));
        p.sample(sample(1.0, LoadState::Loading, 1, vec![]));
        p.sample(sample(1.25, LoadState::Idle, 1, vec![]));
        p.sample(sample(2.0, LoadState::Idle, 1, vec![]));
        assert!(!p.done());
        p.sample(sample(2.25, LoadState::Idle, 1, vec![]));
        assert!(p.done());
        assert!(p.summary("complete")[0].contains(" load_start_s=0.25 load_end_s=1.25 load_s=1.00 "));
        let mut q = LoadProfiler::new(Settings { max_s: 5.0, ..settings() });
        q.sample(sample(4.0, LoadState::NotReady, 1, vec![]));
        assert!(!q.done());
        q.sample(sample(5.0, LoadState::NotReady, 1, vec![]));
        assert!(q.done());
        assert!(q.summary("max_s")[0].contains("end=max_s"));
        assert_eq!(
            LoadProfiler::new(settings()).summary("refused"),
            vec!["X4MP_AGENT LOADPROF SUMMARY end=refused samples=0"]
        );
    }
}
