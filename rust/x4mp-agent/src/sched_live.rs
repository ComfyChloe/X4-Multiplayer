//! In-process side of the fixed-step scheduler (Linux only). Runs inside the agent's
//! VBI slot-13 callback on the MoveWorker thread; the worker thread only logs and
//! requests a restore.
//!
//! Engine writes, all verified before and restored on disarm:
//! 1. Code: two aligned qwords of FrameTick's dt path ([`pinned::DT_LOAD_VA`],
//!    [`pinned::DT_MIN_VA`]) so every `FrameTick` advances time by exactly 1/60 s × speed.
//!    Each is one 8-byte compare-exchange; every intermediate state is valid.
//! 2. Data: the FrameState render flag (`+0x200`), written only while the main thread is
//!    parked outside `FrameTick` (see [`scheduler::slot13_step`]).
//!
//! No semaphore is touched and no engine function is called.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::agent::{covered, epoch_ns, read_f64, read_i32, read_u64, readable_ranges, LiveImage};
use crate::config::Sched;
use crate::pinned;
use crate::scheduler::{check_step, slot13_step, Engine, SchedSnapshot, Scheduler, Slot13, StepCheck, TimeSample};

// ----------------------------------------------------------------------------------
// Phases, faults and published statistics

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u32)]
pub enum Phase {
    Off = 0,
    Pending = 1,
    Active = 2,
    Refused = 3,
    Faulted = 4,
    Restored = 5,
}

impl Phase {
    fn from_code(code: u32) -> Phase {
        [Phase::Off, Phase::Pending, Phase::Active, Phase::Refused, Phase::Faulted, Phase::Restored]
            .get(code as usize)
            .copied()
            .unwrap_or(Phase::Faulted)
    }
    pub fn name(self) -> &'static str {
        match self {
            Phase::Off => "off",
            Phase::Pending => "pending",
            Phase::Active => "armed",
            Phase::Refused => "refused",
            Phase::Faulted => "faulted",
            Phase::Restored => "restored",
        }
    }
}

/// Why admission was refused or the active scheduler faulted (first reason wins).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u32)]
pub enum Fault {
    None = 0,
    FrameStateUnavailable,
    RenderFlagNotInitial,
    MovieCaptureActive,
    DtBytesNotOriginal,
    ConstantMismatch,
    LiveSiteMismatch,
    CodePageNotRx,
    MprotectFailed,
    PatchRace,
    FrameStateChanged,
    DtPatchChanged,
    RealTimeStep,
    TimeInvalid,
    ForeignFlagWrite,
    StatePoisoned,
}

impl Fault {
    fn from_code(code: u32) -> Fault {
        use Fault::*;
        [
            None,
            FrameStateUnavailable,
            RenderFlagNotInitial,
            MovieCaptureActive,
            DtBytesNotOriginal,
            ConstantMismatch,
            LiveSiteMismatch,
            CodePageNotRx,
            MprotectFailed,
            PatchRace,
            FrameStateChanged,
            DtPatchChanged,
            RealTimeStep,
            TimeInvalid,
            ForeignFlagWrite,
            StatePoisoned,
        ]
        .get(code as usize)
        .copied()
        .unwrap_or(StatePoisoned)
    }
    pub fn reason(self) -> &'static str {
        match self {
            Fault::None => "none",
            Fault::FrameStateUnavailable => "frame_state_unavailable",
            Fault::RenderFlagNotInitial => "render_flag_not_initial",
            Fault::MovieCaptureActive => "movie_capture_active",
            Fault::DtBytesNotOriginal => "dt_bytes_not_original",
            Fault::ConstantMismatch => "one_sixtieth_constant_mismatch",
            Fault::LiveSiteMismatch => "live_site_mismatch",
            Fault::CodePageNotRx => "dt_code_page_not_r-x",
            Fault::MprotectFailed => "mprotect_failed",
            Fault::PatchRace => "dt_bytes_changed_during_patch",
            Fault::FrameStateChanged => "frame_state_pointer_changed",
            Fault::DtPatchChanged => "dt_patch_bytes_changed",
            Fault::RealTimeStep => "real_time_step_not_one_sixtieth",
            Fault::TimeInvalid => "timer_values_invalid",
            Fault::ForeignFlagWrite => "render_flag_foreign_write",
            Fault::StatePoisoned => "scheduler_state_poisoned",
        }
    }
}

static CONFIG: OnceLock<Sched> = OnceLock::new();
static PHASE: AtomicU32 = AtomicU32::new(Phase::Off as u32);
static FAULT: AtomicU32 = AtomicU32::new(0);
static RESTORE_REQUEST: AtomicBool = AtomicBool::new(false);
/// The dt qwords currently hold our patch.
static PATCHED: AtomicBool = AtomicBool::new(false);
/// The FrameState whose render flag we own, and the value we wrote last.
static FRAME_STATE: AtomicU64 = AtomicU64::new(0);
static FLAG_VALUE: AtomicBool = AtomicBool::new(true);
static FLAG_OWNED: AtomicBool = AtomicBool::new(false);
static RESTORE_NOTE: AtomicU32 = AtomicU32::new(0);

static ST_STEPS: AtomicU64 = AtomicU64::new(0);
static ST_RENDERS: AtomicU64 = AtomicU64::new(0);
static ST_SKIPPED: AtomicU64 = AtomicU64::new(0);
static ST_FORCED: AtomicU64 = AtomicU64::new(0);
static ST_LOADING: AtomicU64 = AtomicU64::new(0);
static ST_DROPPED_NS: AtomicU64 = AtomicU64::new(0);
static ST_REBASELINES: AtomicU64 = AtomicU64::new(0);
static ST_MAX_LAG: AtomicU64 = AtomicU64::new(0);
static ST_MAX_SKIP: AtomicU64 = AtomicU64::new(0);
static ST_EXACT: AtomicU64 = AtomicU64::new(0);
static ST_IRREGULAR: AtomicU64 = AtomicU64::new(0);
static ST_NO_ADVANCE: AtomicU64 = AtomicU64::new(0);
static ST_MAX_WAIT_NS: AtomicU64 = AtomicU64::new(0);
static ST_TIMEOUTS: AtomicU64 = AtomicU64::new(0);
static ST_GAME: AtomicU64 = AtomicU64::new(0);
static ST_SPEED: AtomicU64 = AtomicU64::new(0);

struct Live {
    sched: Scheduler,
    prev: Option<TimeSample>,
}

static STATE: Mutex<Option<Live>> = Mutex::new(None);

pub fn phase() -> Phase {
    Phase::from_code(PHASE.load(Ordering::SeqCst))
}
pub fn fault() -> Fault {
    Fault::from_code(FAULT.load(Ordering::SeqCst))
}
pub fn config() -> Option<Sched> {
    CONFIG.get().copied()
}
/// How the last restore ended: 0 none, 1 in slot, 2 from the worker, 3 flag written
/// without seeing the main thread parked (timeout).
pub fn restore_note() -> &'static str {
    match RESTORE_NOTE.load(Ordering::SeqCst) {
        1 => "slot",
        2 => "worker",
        3 => "slot_flag_unparked",
        4 => "worker_flag_unparked",
        _ => "none",
    }
}

fn set_fault(f: Fault, phase: Phase) {
    let _ = FAULT.compare_exchange(0, f as u32, Ordering::SeqCst, Ordering::SeqCst);
    PHASE.store(phase as u32, Ordering::SeqCst);
}

/// Called once by the worker before the slot is registered.
pub fn init(cfg: Sched) {
    if CONFIG.set(cfg).is_ok() {
        *STATE.lock().unwrap_or_else(|e| e.into_inner()) = Some(Live { sched: Scheduler::new(cfg.policy), prev: None });
        PHASE.store(Phase::Pending as u32, Ordering::SeqCst);
    }
}

pub fn snapshot() -> SchedSnapshot {
    let mut s = SchedSnapshot::default();
    s.stats.steps = ST_STEPS.load(Ordering::Relaxed);
    s.stats.renders = ST_RENDERS.load(Ordering::Relaxed);
    s.stats.skipped = ST_SKIPPED.load(Ordering::Relaxed);
    s.stats.forced_renders = ST_FORCED.load(Ordering::Relaxed);
    s.stats.loading_steps = ST_LOADING.load(Ordering::Relaxed);
    s.stats.dropped_ns = ST_DROPPED_NS.load(Ordering::Relaxed);
    s.stats.rebaselines = ST_REBASELINES.load(Ordering::Relaxed);
    s.stats.max_lag_steps = ST_MAX_LAG.swap(0, Ordering::Relaxed);
    s.stats.max_skip_run = ST_MAX_SKIP.swap(0, Ordering::Relaxed);
    s.exact_steps = ST_EXACT.load(Ordering::Relaxed);
    s.irregular_steps = ST_IRREGULAR.load(Ordering::Relaxed) + ST_NO_ADVANCE.load(Ordering::Relaxed);
    s.max_wait_ns = ST_MAX_WAIT_NS.swap(0, Ordering::Relaxed);
    s.wait_timeouts = ST_TIMEOUTS.load(Ordering::Relaxed);
    s.game_time = f64::from_bits(ST_GAME.load(Ordering::Relaxed));
    s.speed = f64::from_bits(ST_SPEED.load(Ordering::Relaxed));
    s
}

// ----------------------------------------------------------------------------------
// Raw memory, code patching

unsafe fn read_u8(va: u64) -> u8 {
    std::ptr::read_volatile(va as *const u8)
}

fn qword(va: u64) -> &'static AtomicU64 {
    // SAFETY: callers pass the two 8-byte-aligned dt addresses of the pinned image,
    // verified mapped before the first use.
    unsafe { &*(va as *const AtomicU64) }
}

extern "C" {
    fn mprotect(addr: *mut std::ffi::c_void, len: usize, prot: i32) -> i32;
    fn syscall(number: i64, ...) -> i64;
}
const PROT_READ: i32 = 1;
const PROT_WRITE: i32 = 2;
const PROT_EXEC: i32 = 4;
const SYS_MEMBARRIER: i64 = 324;
const MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE: i64 = 32;
const MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE: i64 = 64;

/// Best effort: make every thread of the process serialize its instruction stream.
fn sync_cores() {
    // SAFETY: plain syscalls with integer arguments; failure (old kernel) is ignored.
    unsafe {
        static REGISTERED: OnceLock<bool> = OnceLock::new();
        let ok = *REGISTERED.get_or_init(|| {
            syscall(SYS_MEMBARRIER, MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE, 0i64, 0i64) == 0
        });
        if ok {
            syscall(SYS_MEMBARRIER, MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE, 0i64, 0i64);
        }
    }
}

/// Run `f` with the dt code page writable (and still executable: other threads may run
/// code on that page meanwhile), then make it `r-x` again.
fn with_code_writable<T>(f: impl FnOnce() -> T) -> Result<T, Fault> {
    let page = pinned::DT_PAGE as *mut std::ffi::c_void;
    // SAFETY: the page belongs to the pinned executable's text mapping (checked r-x).
    unsafe {
        if mprotect(page, 4096, PROT_READ | PROT_WRITE | PROT_EXEC) != 0 {
            return Err(Fault::MprotectFailed);
        }
        let out = f();
        if mprotect(page, 4096, PROT_READ | PROT_EXEC) != 0 {
            return Err(Fault::MprotectFailed);
        }
        Ok(out)
    }
}

fn code_page_is_rx() -> bool {
    let Ok(maps) = std::fs::read_to_string("/proc/self/maps") else { return false };
    maps.lines().any(|line| {
        let mut parts = line.split_whitespace();
        let (Some(range), Some(perms)) = (parts.next(), parts.next()) else { return false };
        let Some((a, b)) = range.split_once('-') else { return false };
        let (Ok(a), Ok(b)) = (u64::from_str_radix(a, 16), u64::from_str_radix(b, 16)) else { return false };
        a <= pinned::DT_PAGE && pinned::DT_PAGE + 4096 <= b && perms.starts_with("r-x")
    })
}

fn load_original() -> u64 {
    u64::from_le_bytes(pinned::DT_LOAD_ORIGINAL)
}
fn load_patched() -> u64 {
    u64::from_le_bytes(pinned::dt_load_patched())
}
fn min_original() -> u64 {
    u64::from_le_bytes(pinned::DT_MIN_ORIGINAL)
}
fn min_patched() -> u64 {
    u64::from_le_bytes(pinned::DT_MIN_PATCHED)
}

fn apply_patch() -> Result<(), Fault> {
    let cas =
        |va: u64, from: u64, to: u64| qword(va).compare_exchange(from, to, Ordering::SeqCst, Ordering::SeqCst).is_ok();
    let result = with_code_writable(|| {
        // Load first (dt = min(1/60, dt)), then drop the min (dt = 1/60).
        if !cas(pinned::DT_LOAD_VA, load_original(), load_patched()) {
            return false;
        }
        if !cas(pinned::DT_MIN_VA, min_original(), min_patched()) {
            let _ = cas(pinned::DT_LOAD_VA, load_patched(), load_original());
            return false;
        }
        true
    })?;
    sync_cores();
    if result {
        PATCHED.store(true, Ordering::SeqCst);
        Ok(())
    } else {
        Err(Fault::PatchRace)
    }
}

/// Restore the dt bytes (reverse order). Safe from any thread at any time: each store is
/// atomic and every intermediate state is a valid FrameTick.
fn restore_code() {
    if !PATCHED.swap(false, Ordering::SeqCst) {
        return;
    }
    let cas = |va: u64, from: u64, to: u64| {
        let _ = qword(va).compare_exchange(from, to, Ordering::SeqCst, Ordering::SeqCst);
    };
    let _ = with_code_writable(|| {
        cas(pinned::DT_MIN_VA, min_patched(), min_original());
        cas(pinned::DT_LOAD_VA, load_patched(), load_original());
    });
    sync_cores();
}

fn main_parked() -> bool {
    // SAFETY: fixed .bss byte of the pinned image (inside main_loop_* sites).
    unsafe { read_u8(pinned::MAIN_FRAME_IN_FLIGHT) == 1 }
}

fn wait_parked(timeout: Duration) -> bool {
    let start = Instant::now();
    while !main_parked() {
        if start.elapsed() >= timeout {
            return false;
        }
        std::thread::sleep(Duration::from_micros(500));
    }
    true
}

/// Put the render flag back to the engine's value (1) if we own it.
fn restore_flag(wait: Duration, from_worker: bool) {
    if !FLAG_OWNED.swap(false, Ordering::SeqCst) {
        return;
    }
    let fs = FRAME_STATE.load(Ordering::SeqCst);
    // SAFETY: fs was verified mapped at admission; FrameState lives for the process.
    unsafe {
        if read_u64(pinned::FRAME_STATE_GLOBAL) != fs || FLAG_VALUE.load(Ordering::SeqCst) {
            return; // object replaced, or the flag already holds 1
        }
        let parked = wait_parked(wait);
        let flag = (fs + pinned::FRAME_STATE_RENDER_FLAG) as *mut u8;
        if std::ptr::read_volatile(flag) == 0 {
            std::ptr::write_volatile(flag, 1);
        }
        FLAG_VALUE.store(true, Ordering::SeqCst);
        RESTORE_NOTE.store(
            match (from_worker, parked) {
                (false, true) => 1,
                (true, true) => 2,
                (false, false) => 3,
                (true, false) => 4,
            },
            Ordering::SeqCst,
        );
    }
}

fn wait_budget() -> Duration {
    Duration::from_millis(config().map(|c| c.wait_ms).unwrap_or(10_000))
}

/// Full restore from the slot-13 thread (safe window available).
pub fn restore_in_slot() {
    restore_code();
    restore_flag(wait_budget(), false);
}

/// Restore requested from the worker thread: let slot 13 do it in its safe window; fall
/// back to restoring here when the simulation does not run (e.g. loading) in time.
pub fn restore_from_worker() {
    let p = phase();
    if !matches!(p, Phase::Active | Phase::Pending)
        && !PATCHED.load(Ordering::SeqCst)
        && !FLAG_OWNED.load(Ordering::SeqCst)
    {
        return;
    }
    RESTORE_REQUEST.store(true, Ordering::SeqCst);
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(3) {
        if !PATCHED.load(Ordering::SeqCst) && !FLAG_OWNED.load(Ordering::SeqCst) && phase() != Phase::Active {
            return;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    restore_code();
    restore_flag(Duration::from_millis(500), true);
    if phase() == Phase::Active || phase() == Phase::Pending {
        PHASE.store(Phase::Restored as u32, Ordering::SeqCst);
    }
}

// ----------------------------------------------------------------------------------
// The live engine for the slot-13 protocol

struct LiveEngine {
    flag: *mut u8,
}

impl Engine for LiveEngine {
    fn main_parked(&self) -> bool {
        main_parked()
    }
    fn now_ns(&self) -> u64 {
        epoch_ns()
    }
    fn pause(&self) {
        std::thread::sleep(Duration::from_micros(500));
    }
    fn sleep_until(&self, ns: u64) {
        let now = epoch_ns();
        if ns > now {
            std::thread::sleep(Duration::from_nanos((ns - now).min(1_000_000_000)));
        }
    }
    fn render_flag(&self) -> bool {
        // SAFETY: verified FrameState field.
        unsafe { std::ptr::read_volatile(self.flag) != 0 }
    }
    fn set_render_flag(&self, render: bool) {
        FLAG_OWNED.store(true, Ordering::SeqCst);
        // SAFETY: verified FrameState field; main thread parked (slot13_step).
        unsafe { std::ptr::write_volatile(self.flag, u8::from(render)) };
        FLAG_VALUE.store(render, Ordering::SeqCst);
    }
}

fn admit(live: &mut Live) -> Result<bool, Fault> {
    let ranges = readable_ranges();
    // SAFETY: every address is checked against /proc/self/maps first.
    unsafe {
        let fs = read_u64(pinned::FRAME_STATE_GLOBAL);
        if fs == 0 || !covered(&ranges, fs, 0x2f0) {
            return Err(Fault::FrameStateUnavailable);
        }
        if read_u8(fs + pinned::FRAME_STATE_RENDER_FLAG) != 1 {
            return Err(Fault::RenderFlagNotInitial);
        }
        if read_u8(pinned::MOVIE_CAPTURE_FLAG) != 0 {
            return Err(Fault::MovieCaptureActive);
        }
        if read_u64(pinned::ONE_SIXTIETH_CONST) != (1.0f64 / 60.0).to_bits() {
            return Err(Fault::ConstantMismatch);
        }
        let image = LiveImage { ranges };
        for name in [
            "frame_tick_time_advance",
            "frame_tick_dt_render_decision",
            "main_loop_frame_tick_call",
            "main_loop_sim_done_wait",
        ] {
            let site = pinned::SITES.iter().find(|s| s.name == name).ok_or(Fault::LiveSiteMismatch)?;
            if pinned::check_site(&image, site) != pinned::Check::Ok {
                return Err(Fault::LiveSiteMismatch);
            }
        }
        if qword(pinned::DT_LOAD_VA).load(Ordering::SeqCst) != load_original()
            || qword(pinned::DT_MIN_VA).load(Ordering::SeqCst) != min_original()
        {
            return Err(Fault::DtBytesNotOriginal);
        }
        if !code_page_is_rx() {
            return Err(Fault::CodePageNotRx);
        }
        // Patch only while the main thread is parked outside FrameTick.
        if !wait_parked(wait_budget()) {
            return Ok(false); // try again next frame
        }
        FRAME_STATE.store(fs, Ordering::SeqCst);
        FLAG_VALUE.store(true, Ordering::SeqCst);
        apply_patch()?;
        live.prev = None;
        Ok(true)
    }
}

fn publish(live: &Live) {
    let s = &live.sched.stats;
    ST_STEPS.store(s.steps, Ordering::Relaxed);
    ST_RENDERS.store(s.renders, Ordering::Relaxed);
    ST_SKIPPED.store(s.skipped, Ordering::Relaxed);
    ST_FORCED.store(s.forced_renders, Ordering::Relaxed);
    ST_LOADING.store(s.loading_steps, Ordering::Relaxed);
    ST_DROPPED_NS.store(s.dropped_ns, Ordering::Relaxed);
    ST_REBASELINES.store(s.rebaselines, Ordering::Relaxed);
}

/// The scheduler's part of one slot-13 callback. `Err` means the scheduler faulted and
/// restored; the caller disarms the whole agent.
pub fn on_frame(loading: bool) -> Result<(), Fault> {
    let p = phase();
    if !matches!(p, Phase::Pending | Phase::Active) {
        return Ok(());
    }
    let mut guard = match STATE.lock() {
        Ok(g) => g,
        Err(_) => {
            restore_in_slot();
            set_fault(Fault::StatePoisoned, Phase::Faulted);
            return Err(Fault::StatePoisoned);
        }
    };
    let Some(live) = guard.as_mut() else { return Ok(()) };
    if RESTORE_REQUEST.load(Ordering::SeqCst) {
        restore_in_slot();
        PHASE.store(Phase::Restored as u32, Ordering::SeqCst);
        return Ok(());
    }
    match frame_inner(live, p, loading) {
        Ok(()) => Ok(()),
        Err(f) => {
            restore_in_slot();
            if phase() == Phase::Pending {
                // Admission refused: nothing of ours is in the engine; the observer stays.
                set_fault(f, Phase::Refused);
                Ok(())
            } else {
                set_fault(f, Phase::Faulted);
                Err(f)
            }
        }
    }
}

fn frame_inner(live: &mut Live, p: Phase, loading: bool) -> Result<(), Fault> {
    if p == Phase::Pending {
        if loading || !admit(live)? {
            return Ok(());
        }
        PHASE.store(Phase::Active as u32, Ordering::SeqCst);
    }
    let fs = FRAME_STATE.load(Ordering::SeqCst);
    // SAFETY: fixed addresses of the pinned image and the FrameState verified at admit.
    let (sample, temp_paused) = unsafe {
        if read_u64(pinned::FRAME_STATE_GLOBAL) != fs {
            return Err(Fault::FrameStateChanged);
        }
        if read_u8(pinned::MOVIE_CAPTURE_FLAG) != 0 {
            return Err(Fault::MovieCaptureActive);
        }
        if qword(pinned::DT_LOAD_VA).load(Ordering::SeqCst) != load_patched()
            || qword(pinned::DT_MIN_VA).load(Ordering::SeqCst) != min_patched()
        {
            return Err(Fault::DtPatchChanged);
        }
        (
            TimeSample {
                game: read_f64(pinned::TIMER_GAME_TIME),
                real: read_f64(pinned::TIMER_REAL_TIME),
                speed: read_f64(pinned::TIMER_SPEED),
            },
            read_i32(pinned::TIMER_TEMP_PAUSE_COUNT) != 0,
        )
    };
    let mut no_advance = false;
    if loading {
        live.prev = None;
    } else {
        if let Some(prev) = live.prev {
            match check_step(&prev, &sample) {
                StepCheck::Exact => {
                    ST_EXACT.fetch_add(1, Ordering::Relaxed);
                }
                StepCheck::GameIrregular => {
                    ST_IRREGULAR.fetch_add(1, Ordering::Relaxed);
                }
                StepCheck::NoAdvance => {
                    ST_NO_ADVANCE.fetch_add(1, Ordering::Relaxed);
                    no_advance = true;
                }
                StepCheck::RealTimeStep => return Err(Fault::RealTimeStep),
                StepCheck::Invalid => return Err(Fault::TimeInvalid),
            }
        }
        live.prev = Some(sample);
    }
    ST_GAME.store(sample.game.to_bits(), Ordering::Relaxed);
    ST_SPEED.store(sample.speed.to_bits(), Ordering::Relaxed);

    let engine = LiveEngine { flag: (fs + pinned::FRAME_STATE_RENDER_FLAG) as *mut u8 };
    let mut last = FLAG_VALUE.load(Ordering::SeqCst);
    let wait_ns = wait_budget().as_nanos() as u64;
    let outcome = slot13_step(&mut live.sched, &engine, &mut last, loading || no_advance, temp_paused, wait_ns);
    match outcome {
        Slot13::Stepped { waited_ns, .. } => {
            ST_MAX_WAIT_NS.fetch_max(waited_ns, Ordering::Relaxed);
        }
        Slot13::WaitTimeout { waited_ns } => {
            ST_MAX_WAIT_NS.fetch_max(waited_ns, Ordering::Relaxed);
            ST_TIMEOUTS.fetch_add(1, Ordering::Relaxed);
        }
        Slot13::ForeignFlagWrite => return Err(Fault::ForeignFlagWrite),
    }
    // Per-window maxima (the worker swaps them to 0 per log line).
    if let Slot13::Stepped { decision, .. } = outcome {
        ST_MAX_LAG.fetch_max(decision.lag_steps, Ordering::Relaxed);
    }
    ST_MAX_SKIP.fetch_max(live.sched.current_skip_run(), Ordering::Relaxed);
    publish(live);
    Ok(())
}
