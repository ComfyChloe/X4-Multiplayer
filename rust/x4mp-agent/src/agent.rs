//! In-process side of the agent (Linux only): the ELF constructor, the worker thread that
//! verifies the build and defers registration, and the per-frame callback in VBI slot 13.
//!
//! The only engine write is one pointer into a verified-empty per-frame slot of the VBI
//! table (compare-exchange from null), and clearing it again on disarm. No code bytes are
//! patched. Everything else is read-only.

use std::cell::Cell;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use crate::config::{self, Config, Parsed};
use crate::export::{self, FrameSample};
use crate::loadprof::{LoadProfiler, LoadState, ProcSample, Settings, ThreadSample};
use crate::logic::{cadence_line, ArmGate, Disarm, Gate, Observation, Snapshot};
use crate::pinned::{self, Image};
use crate::{loadstages, procstat, sha256};

#[used]
#[link_section = ".init_array"]
static INIT: extern "C" fn() = constructor;

extern "C" fn constructor() {
    // Never unwind into the dynamic loader; never block X4's startup.
    let _ = catch_unwind(|| {
        let parsed = config::parse(|key| std::env::var(key).ok(), std::process::id());
        match parsed {
            Parsed::Disabled => {}
            Parsed::Invalid(reason) => eprintln!("X4MP_AGENT REFUSED config: {reason}"),
            Parsed::Enabled(config) => {
                let _ = std::thread::Builder::new().name("x4mp-agent".into()).spawn(move || worker(config));
            }
        }
    });
}

// ----------------------------------------------------------------------------------
// Live process memory

/// Readable `[start, end)` ranges from `/proc/self/maps`.
fn readable_ranges() -> Vec<(u64, u64)> {
    let Ok(maps) = fs::read_to_string("/proc/self/maps") else { return Vec::new() };
    maps.lines()
        .filter_map(|line| {
            let mut parts = line.split_whitespace();
            let range = parts.next()?;
            let perms = parts.next()?;
            if !perms.starts_with('r') {
                return None;
            }
            let (a, b) = range.split_once('-')?;
            Some((u64::from_str_radix(a, 16).ok()?, u64::from_str_radix(b, 16).ok()?))
        })
        .collect()
}

fn covered(ranges: &[(u64, u64)], va: u64, len: usize) -> bool {
    let Some(end) = va.checked_add(len as u64) else { return false };
    ranges.iter().any(|&(a, b)| a <= va && end <= b)
}

/// The running executable, read in place after checking `/proc/self/maps`.
struct LiveImage {
    ranges: Vec<(u64, u64)>,
}

impl Image for LiveImage {
    fn read(&self, va: u64, len: usize) -> Option<Vec<u8>> {
        if !covered(&self.ranges, va, len) {
            return None;
        }
        // SAFETY: the whole range is mapped readable in this process.
        Some(unsafe { std::slice::from_raw_parts(va as *const u8, len) }.to_vec())
    }
}

unsafe fn read_u64(va: u64) -> u64 {
    std::ptr::read_volatile(va as *const u64)
}
unsafe fn read_f64(va: u64) -> f64 {
    std::ptr::read_volatile(va as *const f64)
}
unsafe fn read_i32(va: u64) -> i32 {
    std::ptr::read_volatile(va as *const i32)
}

/// One per-frame slot of the VBI table, as an atomic word.
unsafe fn slot(table: u64, index: usize) -> &'static AtomicUsize {
    &*((table + 8 * index as u64) as *const AtomicUsize)
}

/// MoveWorker TLS game clock: current and previous `CurTime` (local-exec static TLS of
/// the ET_EXEC image, fixed fs-relative offsets).
#[inline]
unsafe fn tls_dt() -> f64 {
    let cur: u64;
    let prev: u64;
    std::arch::asm!(
        "mov {cur}, qword ptr fs:[{c}]",
        "mov {prev}, qword ptr fs:[{p}]",
        cur = out(reg) cur, prev = out(reg) prev,
        c = const pinned::TLS_CUR_TIME, p = const pinned::TLS_PREV_TIME,
        options(nostack, readonly, preserves_flags)
    );
    f64::from_bits(cur) - f64::from_bits(prev)
}

fn movement_worker_running() -> bool {
    let Ok(tasks) = fs::read_dir("/proc/self/task") else { return false };
    tasks.flatten().any(|task| {
        fs::read(task.path().join("comm"))
            .map(|name| name.strip_suffix(b"\n").unwrap_or(&name) == pinned::MOVE_WORKER_THREAD_NAME)
            .unwrap_or(false)
    })
}

// ----------------------------------------------------------------------------------
// The registered handler and its shared state

/// The object placed in the slot: the engine calls `(*vtable)[2](this)` (vtable +0x10).
#[repr(C)]
struct Handler {
    vtable: *const [usize; 4],
}
// SAFETY: immutable after construction; only its address is shared with the engine.
unsafe impl Sync for Handler {}

extern "C" fn no_op(_this: *mut Handler) {}
/// The callable vtable is filled at runtime (fn pointers are not usize constants).
static mut VTABLE_LIVE: [usize; 4] = [0; 4];
static mut HANDLER_LIVE: Handler = Handler { vtable: std::ptr::null() };

static TABLE: AtomicU64 = AtomicU64::new(0);
static ARMED: AtomicBool = AtomicBool::new(false);
static DISARM: AtomicU32 = AtomicU32::new(0);
static OWNER_CLAIMED: AtomicBool = AtomicBool::new(false);
static IN_CALLBACK: AtomicBool = AtomicBool::new(false);

static FRAMES: AtomicU64 = AtomicU64::new(0);
static LOADING_SKIPS: AtomicU64 = AtomicU64::new(0);
static PAUSED_FRAMES: AtomicU64 = AtomicU64::new(0);
static GAME_TIME: AtomicU64 = AtomicU64::new(0);
static REAL_TIME: AtomicU64 = AtomicU64::new(0);
static SPEED: AtomicU64 = AtomicU64::new(0);
static TLS_DT: AtomicU64 = AtomicU64::new(0);
static PAUSED: AtomicBool = AtomicBool::new(false);
static MAX_GAP_NS: AtomicU64 = AtomicU64::new(0);
static LAST_NS: AtomicU64 = AtomicU64::new(0);

thread_local! {
    static IS_OWNER: Cell<bool> = const { Cell::new(false) };
}

fn epoch() -> &'static Instant {
    static EPOCH: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    EPOCH.get_or_init(Instant::now)
}

fn handler_address() -> usize {
    // The address of the static is stable; its contents are initialised by `arm`.
    std::ptr::addr_of!(HANDLER_LIVE) as usize
}

/// Fail closed: record the first reason and withdraw the slot if it is still ours.
fn disarm(reason: Disarm) {
    let _ = DISARM.compare_exchange(0, reason as u32, Ordering::SeqCst, Ordering::SeqCst);
    if ARMED.swap(false, Ordering::SeqCst) {
        let table = TABLE.load(Ordering::SeqCst);
        // SAFETY: the table pointer was verified readable/writable at arm time and the
        // engine keeps the table alive for the process lifetime once created.
        if table != 0 && unsafe { read_u64(pinned::VBI_TABLE_GLOBAL) } == table {
            let _ = unsafe { slot(table, pinned::AGENT_SLOT) }.compare_exchange(
                handler_address(),
                0,
                Ordering::SeqCst,
                Ordering::SeqCst,
            );
        }
    }
}

extern "C" fn on_move(_this: *mut Handler) {
    if !ARMED.load(Ordering::Acquire) {
        return;
    }
    if IN_CALLBACK.swap(true, Ordering::Acquire) {
        disarm(Disarm::Reentrant);
        return;
    }
    let result = catch_unwind(AssertUnwindSafe(frame));
    IN_CALLBACK.store(false, Ordering::Release);
    match result {
        Ok(Ok(())) => {}
        Ok(Err(reason)) => disarm(reason),
        Err(_) => disarm(Disarm::Panic),
    }
}

fn frame() -> Result<(), Disarm> {
    // Thread ownership: the first callback must be on the `Movement worker` thread, and
    // every later callback on that same thread.
    if !IS_OWNER.with(Cell::get) {
        if OWNER_CLAIMED.swap(true, Ordering::SeqCst) {
            return Err(Disarm::ForeignThread);
        }
        let name = fs::read("/proc/thread-self/comm").unwrap_or_default();
        if name.strip_suffix(b"\n").unwrap_or(&name) != pinned::MOVE_WORKER_THREAD_NAME {
            return Err(Disarm::NotMovementWorker);
        }
        IS_OWNER.with(|owner| owner.set(true));
    }
    let table = TABLE.load(Ordering::Relaxed);
    // SAFETY: fixed .bss global of the pinned executable; table verified at arm time.
    unsafe {
        if read_u64(pinned::VBI_TABLE_GLOBAL) != table {
            return Err(Disarm::TableChanged);
        }
        if slot(table, pinned::AGENT_SLOT).load(Ordering::Relaxed) != handler_address() {
            return Err(Disarm::SlotChanged);
        }
        let client = slot(table, pinned::GAME_CLIENT_SLOT).load(Ordering::Relaxed) as u64;
        if client == 0 || read_u64(client) != pinned::GAME_CLIENT_VTABLE {
            return Err(Disarm::GameClientChanged);
        }
    }
    let now = epoch().elapsed().as_nanos() as u64;
    let last = LAST_NS.swap(now, Ordering::Relaxed);
    if last != 0 {
        MAX_GAP_NS.fetch_max(now.saturating_sub(last), Ordering::Relaxed);
    }
    // SAFETY: loading flag is an int inside the verified table.
    if unsafe { read_i32(table + pinned::VBI_LOADING_FLAG as u64) } != 0 {
        LOADING_SKIPS.fetch_add(1, Ordering::Relaxed);
        return Ok(());
    }
    // SAFETY: TimerBase fields are fixed .bss doubles/ints of the pinned executable. The
    // engine writes them under its mutex on the main thread before releasing this frame.
    let (game_time, real_time, speed, paused, dt) = unsafe {
        let paused = read_i32(pinned::TIMER_PAUSE_COUNT) != 0 || read_i32(pinned::TIMER_TEMP_PAUSE_COUNT) != 0;
        (
            read_f64(pinned::TIMER_GAME_TIME),
            read_f64(pinned::TIMER_REAL_TIME),
            read_f64(pinned::TIMER_SPEED),
            paused,
            tls_dt(),
        )
    };
    if !game_time.is_finite() || !real_time.is_finite() || !speed.is_finite() || !(0.0..=64.0).contains(&speed) {
        return Err(Disarm::InvalidTime);
    }
    let frame = FRAMES.fetch_add(1, Ordering::Relaxed) + 1;
    if paused {
        PAUSED_FRAMES.fetch_add(1, Ordering::Relaxed);
    }
    GAME_TIME.store(game_time.to_bits(), Ordering::Relaxed);
    REAL_TIME.store(real_time.to_bits(), Ordering::Relaxed);
    SPEED.store(speed.to_bits(), Ordering::Relaxed);
    TLS_DT.store(dt.to_bits(), Ordering::Relaxed);
    PAUSED.store(paused, Ordering::Relaxed);
    crate::forkprobe::on_frame();
    let sample = FrameSample { frame, game_time, real_time, speed, tls_dt: dt, paused, monotonic_ns: now };
    export::on_frame(&sample).map_err(|_| Disarm::ExportFailed)
}

fn snapshot() -> Snapshot {
    Snapshot {
        frames: FRAMES.load(Ordering::Relaxed),
        loading_skips: LOADING_SKIPS.load(Ordering::Relaxed),
        paused_frames: PAUSED_FRAMES.load(Ordering::Relaxed),
        game_time: f64::from_bits(GAME_TIME.load(Ordering::Relaxed)),
        real_time: f64::from_bits(REAL_TIME.load(Ordering::Relaxed)),
        speed: f64::from_bits(SPEED.load(Ordering::Relaxed)),
        tls_dt: f64::from_bits(TLS_DT.load(Ordering::Relaxed)),
        paused: PAUSED.load(Ordering::Relaxed),
        max_gap_ns: MAX_GAP_NS.swap(0, Ordering::Relaxed),
        monotonic_ns: epoch().elapsed().as_nanos() as u64,
    }
}

// ----------------------------------------------------------------------------------
// Load profiling (layers 1 and 2). Only the worker thread runs this, and it only reads
// /proc/self, the profile's progressbar.xml and the loading flag already read by
// observe(): nothing in the engine is written or called.

#[repr(C)]
struct Timespec {
    tv_sec: i64,
    tv_nsec: i64,
}

extern "C" {
    fn sysconf(name: i32) -> i64;
    fn clock_gettime(clock: i32, tp: *mut Timespec) -> i32;
}

const SC_CLK_TCK: i32 = 2;
const CLOCK_MONOTONIC: i32 = 1;

/// CLOCK_MONOTONIC in seconds, the clock of `perf record -k CLOCK_MONOTONIC`.
fn mono_now() -> f64 {
    let mut ts = Timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: plain libc call with a valid out pointer.
    if unsafe { clock_gettime(CLOCK_MONOTONIC, &mut ts) } != 0 {
        return 0.0;
    }
    ts.tv_sec as f64 + ts.tv_nsec as f64 / 1e9
}

fn read_text(path: &str, buf: &mut String) -> bool {
    buf.clear();
    File::open(path).and_then(|mut f| std::io::Read::read_to_string(&mut f, buf)).is_ok()
}

/// Process age = time since `execve`, from `/proc/self/stat` starttime and `/proc/uptime`.
fn process_age(clk_tck: f64) -> f64 {
    let start = fs::read_to_string("/proc/self/stat").ok().and_then(|t| procstat::parse_stat(&t)).map(|s| s.starttime);
    let uptime = fs::read_to_string("/proc/uptime").ok().and_then(|t| procstat::parse_uptime(&t));
    match (start, uptime) {
        (Some(start), Some(up)) => (up - start as f64 / clk_tck).max(0.0),
        _ => 0.0,
    }
}

struct Profiling {
    profiler: LoadProfiler,
    clk_tck: f64,
    age0: f64,
    at0: Instant,
    buf: String,
    path: String,
}

impl Profiling {
    fn new(c: config::LoadProf) -> Profiling {
        // SAFETY: sysconf has no preconditions.
        let tck = unsafe { sysconf(SC_CLK_TCK) };
        let clk_tck = if tck > 0 { tck as f64 } else { 100.0 };
        Profiling {
            profiler: LoadProfiler::new(Settings { clk_tck, tail_s: c.tail_s as f64, max_s: c.max_s as f64, top: c.top as usize }),
            clk_tck,
            age0: process_age(clk_tck),
            at0: Instant::now(),
            buf: String::with_capacity(1024),
            path: String::with_capacity(64),
        }
    }

    fn read(&mut self, o: &Observation) -> ProcSample {
        use std::fmt::Write as _;
        let t = self.age0 + self.at0.elapsed().as_secs_f64();
        let mono = mono_now();
        let mut threads = Vec::with_capacity(128);
        if let Ok(tasks) = fs::read_dir("/proc/self/task") {
            for task in tasks.flatten() {
                let Some(tid) = task.file_name().to_str().and_then(|n| n.parse::<u32>().ok()) else { continue };
                self.path.clear();
                let _ = write!(self.path, "/proc/self/task/{tid}/stat");
                if !read_text(&self.path, &mut self.buf) {
                    continue; // thread exited meanwhile
                }
                if let Some(st) = procstat::parse_stat(&self.buf) {
                    let ticks = st.cpu_ticks();
                    threads.push(ThreadSample { tid, name: st.comm, ticks });
                }
            }
        }
        let process =
            if read_text("/proc/self/stat", &mut self.buf) { procstat::parse_stat(&self.buf) } else { None }.unwrap_or_default();
        let io = if read_text("/proc/self/io", &mut self.buf) { procstat::parse_io(&self.buf) } else { None }.unwrap_or_default();
        let mem = if read_text("/proc/self/status", &mut self.buf) { procstat::parse_status_memory(&self.buf) } else { None }
            .unwrap_or_default();
        let state = if !o.table_ready {
            LoadState::NotReady
        } else if o.loading {
            LoadState::Loading
        } else {
            LoadState::Idle
        };
        ProcSample {
            t,
            mono,
            proc_ticks: process.cpu_ticks(),
            threads,
            io,
            mem,
            minflt: process.minflt,
            majflt: process.majflt,
            state,
            game_time: o.game_time.is_finite().then_some(o.game_time),
            game_client: o.game_client_ok,
        }
    }

    /// One sample and its row; the summary when the end condition is reached.
    fn tick(&mut self, log: &mut Log, o: &Observation) {
        if self.profiler.done() {
            return;
        }
        let started = Instant::now();
        let sample = self.read(o);
        let cost_us = started.elapsed().as_micros();
        if let Some(row) = self.profiler.sample(sample) {
            log.line(&format!("{row} cost_us={cost_us}"));
        }
        if self.profiler.done() {
            let reason = if self.profiler.load_ended() { "complete" } else { "max_s" };
            self.finish(log, reason);
        }
    }

    fn finish(&mut self, log: &mut Log, reason: &str) {
        for line in self.profiler.summary(reason) {
            log.line(&line);
        }
    }
}

/// `LOADSTAGES`: progressbar.xml before the load (worker start) and after (once armed).
struct Stages {
    path: PathBuf,
    before: Option<String>,
    candidate: Option<String>,
    deadline: Option<Instant>,
    done: bool,
}

const LOADSTAGES_WAIT: Duration = Duration::from_secs(120);

impl Stages {
    fn resolve(config: &Config) -> Result<PathBuf, &'static str> {
        if let Some(path) = &config.loadstages_file {
            return Ok(path.clone());
        }
        let cmdline = fs::read("/proc/self/cmdline").map_err(|_| "cmdline_unreadable")?;
        let id = procstat::personal_folder_id(&cmdline).ok_or("no_personalfolderid_set_X4MP_LOADSTAGES_FILE")?;
        let base = match std::env::var_os("XDG_CONFIG_HOME").filter(|v| Path::new(v).is_absolute()) {
            Some(dir) => PathBuf::from(dir),
            None => PathBuf::from(std::env::var_os("HOME").ok_or("no_home")?).join(".config"),
        };
        Ok(base.join("EgoSoft/X4").join(id).join("progressbar.xml"))
    }

    fn start(config: &Config, log: &mut Log) -> Option<Stages> {
        if !config.loadstages {
            return None;
        }
        match Stages::resolve(config) {
            Ok(path) => {
                let before = fs::read_to_string(&path).ok();
                let types = before.as_deref().map(loadstages::parse).unwrap_or_default();
                let summary: Vec<String> =
                    types.iter().map(|t| format!("{}:{}", crate::loadprof::token(&t.name), t.loaded)).collect();
                log.line(&format!(
                    "X4MP_AGENT LOADSTAGES before file={} exists={} types={}",
                    path.display(),
                    u8::from(before.is_some()),
                    if summary.is_empty() { "-".into() } else { summary.join(",") }
                ));
                Some(Stages { path, before, candidate: None, deadline: None, done: false })
            }
            Err(reason) => {
                log.line(&format!("X4MP_AGENT LOADSTAGES unavailable reason={reason}"));
                None
            }
        }
    }

    /// Called about once per second after arming. Logs once the file has changed and
    /// reads the same on two consecutive polls (so it is not caught mid-write).
    fn poll(&mut self, log: &mut Log, measured: Option<f64>) {
        if self.done {
            return;
        }
        let deadline = *self.deadline.get_or_insert_with(|| Instant::now() + LOADSTAGES_WAIT);
        let now = fs::read_to_string(&self.path).ok();
        if now.is_some() && now != self.before {
            if now == self.candidate {
                let before = self.before.as_deref().map(loadstages::parse).unwrap_or_default();
                let after = loadstages::parse(now.as_deref().unwrap_or_default());
                let deltas = loadstages::diff(&before, &after);
                if deltas.is_empty() {
                    log.line("X4MP_AGENT LOADSTAGES changed_without_stage_delta (layout not recognised; see reports/LOAD-PROFILING.md)");
                }
                for d in &deltas {
                    log.line(&loadstages::line(d, measured));
                }
                self.done = true;
                return;
            }
            self.candidate = now;
        }
        if Instant::now() >= deadline {
            log.line(&format!("X4MP_AGENT LOADSTAGES unchanged_after_s={} file={}", LOADSTAGES_WAIT.as_secs(), self.path.display()));
            self.done = true;
        }
    }
}

// ----------------------------------------------------------------------------------
// Worker thread

struct Log(Option<File>);

impl Log {
    fn open(path: &Path) -> Log {
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        Log(OpenOptions::new().create(true).append(true).open(path).ok())
    }
    fn line(&mut self, text: &str) {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        match &mut self.0 {
            Some(file) => {
                let _ = writeln!(file, "{stamp:.3} {text}");
                let _ = file.flush();
            }
            None => eprintln!("{text}"),
        }
    }
}

fn verify_build(log: &mut Log) -> bool {
    let exe = Path::new("/proc/self/exe");
    match fs::metadata(exe) {
        Ok(meta) if meta.len() == pinned::FILE_SIZE => {}
        Ok(meta) => {
            log.line(&format!("X4MP_AGENT REFUSED executable size {} (pinned {})", meta.len(), pinned::FILE_SIZE));
            return false;
        }
        Err(e) => {
            log.line(&format!("X4MP_AGENT REFUSED cannot stat executable: {e}"));
            return false;
        }
    }
    match sha256::file_hex(exe) {
        Ok(digest) if digest == pinned::FILE_SHA256 => {}
        Ok(digest) => {
            log.line(&format!("X4MP_AGENT REFUSED executable sha256 {digest}"));
            return false;
        }
        Err(e) => {
            log.line(&format!("X4MP_AGENT REFUSED cannot hash executable: {e}"));
            return false;
        }
    }
    match pinned::verify_sites(&LiveImage { ranges: readable_ranges() }) {
        Ok(count) => {
            log.line(&format!(
                "X4MP_AGENT VERIFIED build={} version={} sites={count} pid={}",
                pinned::STEAM_BUILD,
                pinned::GAME_VERSION,
                std::process::id()
            ));
            true
        }
        Err(reason) => {
            log.line(&format!("X4MP_AGENT REFUSED live signature {reason}"));
            false
        }
    }
}

fn observe(start: Instant) -> Observation {
    let ranges = readable_ranges();
    let now_ms = start.elapsed().as_millis() as u64;
    let mut o = Observation { now_ms, mover_thread: movement_worker_running(), ..Default::default() };
    if !covered(&ranges, pinned::VBI_TABLE_GLOBAL, 8) || !covered(&ranges, pinned::TIMER_BASE, 0x70) {
        return o;
    }
    // SAFETY: every address below is checked against /proc/self/maps first.
    unsafe {
        let table = read_u64(pinned::VBI_TABLE_GLOBAL);
        o.game_time = read_f64(pinned::TIMER_GAME_TIME);
        if table == 0 || !covered(&ranges, table, pinned::VBI_TABLE_SIZE) {
            return o;
        }
        o.table_ready = true;
        o.loading = read_i32(table + pinned::VBI_LOADING_FLAG as u64) != 0;
        o.slot_free = slot(table, pinned::AGENT_SLOT).load(Ordering::SeqCst) == 0
            && slot(table, pinned::VBI_SLOTS + pinned::AGENT_SLOT).load(Ordering::SeqCst) == 0;
        let client = slot(table, pinned::GAME_CLIENT_SLOT).load(Ordering::SeqCst) as u64;
        o.game_client_ok = client != 0 && covered(&ranges, client, 8) && read_u64(client) == pinned::GAME_CLIENT_VTABLE;
    }
    o
}

fn arm(table: u64) -> bool {
    // SAFETY: single-threaded initialisation before the pointer is published.
    unsafe {
        VTABLE_LIVE = [no_op as *const () as usize, no_op as *const () as usize, on_move as *const () as usize, no_op as *const () as usize];
        HANDLER_LIVE = Handler { vtable: std::ptr::addr_of!(VTABLE_LIVE) };
    }
    epoch();
    TABLE.store(table, Ordering::SeqCst);
    ARMED.store(true, Ordering::SeqCst);
    // SAFETY: table verified mapped; the slot was observed null. Compare-exchange so a
    // concurrent writer is never overwritten.
    let won = unsafe { slot(table, pinned::AGENT_SLOT) }
        .compare_exchange(0, handler_address(), Ordering::SeqCst, Ordering::SeqCst)
        .is_ok();
    if !won {
        ARMED.store(false, Ordering::SeqCst);
    }
    won
}

fn worker(config: Config) {
    let mut log = Log::open(&config.log);
    log.line(&format!("X4MP_AGENT START pid={} log={}", std::process::id(), config.log.display()));
    if !verify_build(&mut log) {
        return;
    }
    // Captured right after verification (well under a second after exec), long before
    // the first load can finish and update the file.
    let mut stages = Stages::start(&config, &mut log);
    let mut prof = config.loadprof.map(Profiling::new);
    if let Some(p) = &prof {
        log.line(&format!(
            "X4MP_AGENT LOADPROF start process_age_s={:.3} mono={:.3} interval_ms=250 clk_tck={}",
            p.age0,
            mono_now(),
            p.clk_tck
        ));
    }
    let reason = run(&config, &mut log, &mut prof, &mut stages);
    if let Some(p) = prof.as_mut().filter(|p| !p.profiler.done()) {
        p.finish(&mut log, reason);
    }
}

/// Wait for the game, arm, then report. Returns why it stopped.
fn run(config: &Config, log: &mut Log, prof: &mut Option<Profiling>, stages: &mut Option<Stages>) -> &'static str {
    let start = Instant::now();
    let mut gate = ArmGate::new(config.settle_ms);
    let mut last_wait = "";
    let mut next_diag = Instant::now();
    loop {
        if Instant::now() >= next_diag {
            next_diag = Instant::now() + Duration::from_secs(30);
            let ranges = readable_ranges();
            let global = if covered(&ranges, pinned::VBI_TABLE_GLOBAL, 8) {
                // SAFETY: checked readable above.
                format!("0x{:x}", unsafe { read_u64(pinned::VBI_TABLE_GLOBAL) })
            } else {
                "unreadable".to_string()
            };
            log.line(&format!(
                "X4MP_AGENT WAITDIAG vbi_global={global} mover_thread={} uptime_s={}",
                u8::from(movement_worker_running()),
                start.elapsed().as_secs()
            ));
        }
        if config.disarm_file.exists() {
            log.line("X4MP_AGENT NOT_ARMED reason=disarm_file_present");
            return "disarm_file";
        }
        let o = observe(start);
        if let Some(p) = prof.as_mut() {
            p.tick(log, &o);
        }
        match gate.observe(&o) {
            Gate::Wait(reason) => {
                if reason != last_wait {
                    log.line(&format!("X4MP_AGENT WAIT reason={reason}"));
                    last_wait = reason;
                }
            }
            Gate::Refuse(reason) => {
                log.line(&format!("X4MP_AGENT REFUSED reason={reason}"));
                return "refused";
            }
            Gate::Arm => {
                // SAFETY: observe() just verified the table is mapped.
                let table = unsafe { read_u64(pinned::VBI_TABLE_GLOBAL) };
                if arm(table) {
                    log.line(&format!("X4MP_AGENT ARMED slot={} table=0x{table:x}", pinned::AGENT_SLOT));
                    break;
                }
                log.line("X4MP_AGENT REFUSED reason=agent_slot_taken_during_arm");
                return "refused";
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    let mut prev = snapshot();
    let mut prev_at = Instant::now();
    let fork_trigger = {
        let mut name = config.log.clone().into_os_string();
        name.push(".fork");
        std::path::PathBuf::from(name)
    };
    let mut fork_reporter = crate::forkprobe::Reporter::default();
    log.line(&format!("X4MP_AGENT FORKPROBE ready trigger={}", fork_trigger.display()));
    let cadence = Duration::from_millis(config.cadence_ms);
    let mut next_cadence = Instant::now() + cadence;
    loop {
        // While the load profiler still runs (its tail after loading), wake every 250 ms
        // for its samples; the cadence work below keeps its own period.
        let profiling = prof.as_ref().is_some_and(|p| !p.profiler.done());
        let wait = next_cadence.saturating_duration_since(Instant::now());
        std::thread::sleep(if profiling { wait.min(Duration::from_millis(250)) } else { wait });
        if profiling {
            let o = observe(start);
            if let Some(p) = prof.as_mut() {
                p.tick(log, &o);
            }
        }
        if Instant::now() < next_cadence {
            continue;
        }
        next_cadence = Instant::now() + cadence;
        if let Some(s) = stages.as_mut() {
            s.poll(log, prof.as_ref().and_then(|p| p.profiler.load_seconds()));
        }
        if let Ok(text) = fs::read_to_string(&fork_trigger) {
            let _ = fs::remove_file(&fork_trigger);
            match crate::forkprobe::parse_count(&text) {
                Some(count) => {
                    crate::forkprobe::request(count);
                    log.line(&format!("X4MP_AGENT FORKPROBE requested count={count}"));
                }
                None => log.line("X4MP_AGENT FORKPROBE refused trigger (expected 1..1000)"),
            }
        }
        for line in fork_reporter.poll() {
            log.line(&line);
        }
        if config.disarm_file.exists() {
            disarm(Disarm::DisarmFile);
        }
        let cur = snapshot();
        let wall_ms = prev_at.elapsed().as_millis() as u64;
        log.line(&cadence_line(&prev, &cur, wall_ms));
        prev = cur;
        prev_at = Instant::now();
        let code = DISARM.load(Ordering::SeqCst);
        if code != 0 {
            log.line(&format!("X4MP_AGENT DISARMED reason={}", Disarm::from_code(code).reason()));
            return "disarmed";
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn covered_requires_one_range_to_contain_the_whole_span() {
        let ranges = [(0x1000, 0x2000), (0x2000, 0x3000)];
        assert!(covered(&ranges, 0x1000, 0x1000));
        assert!(!covered(&ranges, 0x1ff8, 0x10)); // spans two mappings: refuse
        assert!(!covered(&ranges, 0x3000, 1));
        assert!(!covered(&ranges, u64::MAX - 2, 8));
    }

    /// Cost of one load-profiler sample with ~100 threads (X4 runs about that many).
    /// `cargo test --release -p x4mp-agent -- --ignored --nocapture loadprof_sample_cost`
    #[test]
    #[ignore]
    fn loadprof_sample_cost() {
        let stop = std::sync::Arc::new(AtomicBool::new(false));
        let handles: Vec<_> = (0..100)
            .map(|i| {
                let stop = stop.clone();
                std::thread::Builder::new()
                    .name(format!("JobThread {i}"))
                    .spawn(move || {
                        while !stop.load(Ordering::Relaxed) {
                            std::thread::sleep(Duration::from_millis(5));
                        }
                    })
                    .unwrap()
            })
            .collect();
        let mut p = Profiling::new(config::LoadProf { tail_s: 10, max_s: 900, top: 6 });
        let o = Observation { table_ready: true, loading: true, game_time: 0.0, ..Default::default() };
        let rounds = 200;
        let t0 = Instant::now();
        let mut threads = 0;
        for _ in 0..rounds {
            threads = p.read(&o).threads.len();
        }
        let per = t0.elapsed().as_secs_f64() / rounds as f64;
        let cpu_before = std::time::Instant::now();
        let mut rows = 0;
        for _ in 0..rounds {
            let s = p.read(&o);
            rows += p.profiler.sample(s).map_or(0, |r| r.len());
        }
        let with_rows = cpu_before.elapsed().as_secs_f64() / rounds as f64;
        stop.store(true, Ordering::Relaxed);
        for h in handles {
            h.join().unwrap();
        }
        println!(
            "LOADPROF sample: {threads} threads, read {:.0} us, read+row {:.0} us, at 4 Hz = {:.3}% of one core ({rows} row bytes)",
            per * 1e6,
            with_rows * 1e6,
            with_rows * 4.0 * 100.0
        );
        assert!(threads >= 100);
    }

    #[test]
    fn own_maps_are_parsed() {
        let ranges = readable_ranges();
        let here = covered as *const () as usize as u64;
        assert!(covered(&ranges, here, 16));
    }
}
