//! The process-wide core instance and the entry points hosts export.
//!
//! A host (Linux agent, Windows wrapper) does three things:
//! 1. after verifying the executable, [`install`] with its bound [`Api`];
//! 2. once per simulation frame on the engine thread, [`frame`];
//! 3. export [`md_take`] / [`md_result`] as C functions for the Lua bridge, and drain
//!    the log with [`drain_log`] from a thread that may do I/O.
//!
//! Thread rule: the first [`frame`] call claims the engine thread. Later frames and all
//! bridge calls must come from that thread (the Lua UI `onUpdate` runs on it); anything
//! else is refused. The frame thread never blocks: it uses `try_lock` and counts a
//! contended frame instead of waiting.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::thread::ThreadId;

use crate::config::CoreConfig;
use crate::ffi::{Api, BindReport, Capabilities, Engine};
use crate::frame::{Clock, DriverConfig, FrameCtx, FrameDriver, FrameInput, MonotonicClock, Subsystem};
use crate::logbuf::LogBuf;
use crate::mdq::{ActionId, MdAction, MdQueue, MdQueueConfig, MdStatus};
use crate::pins::Platform;
use crate::pose::PlayerPose;

/// Bridge return codes (negative = refused, nothing consumed).
pub const BRIDGE_DISABLED: isize = -1;
pub const BRIDGE_FOREIGN_THREAD: isize = -2;
pub const BRIDGE_BAD_ARGUMENT: isize = -3;
pub const BRIDGE_BUSY: isize = -4;

/// Periodic `debug_text` through the MD queue, proving submit -> MD -> result.
pub struct MdProbe {
    interval_ns: u64,
    next_ns: Option<u64>,
    seq: u64,
    sent: u64,
    rejected: u64,
    ok: u64,
    failed: u64,
    timeouts: u64,
    last_latency_ns: Option<u64>,
}

impl MdProbe {
    pub fn new(interval_s: u64) -> Self {
        MdProbe {
            interval_ns: interval_s.max(1) * 1_000_000_000,
            next_ns: None,
            seq: 0,
            sent: 0,
            rejected: 0,
            ok: 0,
            failed: 0,
            timeouts: 0,
            last_latency_ns: None,
        }
    }
}

impl Subsystem for MdProbe {
    fn name(&self) -> &'static str {
        "md_probe"
    }
    fn on_frame(&mut self, ctx: &mut FrameCtx<'_>) -> Result<(), &'static str> {
        while let Some(result) = ctx.md_result() {
            match result.status {
                MdStatus::Ok => self.ok += 1,
                MdStatus::Timeout => self.timeouts += 1,
                MdStatus::Failed | MdStatus::UnknownKind => self.failed += 1,
            }
            self.last_latency_ns = Some(result.latency_ns);
        }
        let now = ctx.input.monotonic_ns;
        let due = *self.next_ns.get_or_insert(now);
        if now >= due {
            self.next_ns = Some(now + self.interval_ns);
            self.seq += 1;
            let text = format!("[X4MP_CORE] md probe seq={} frame={}", self.seq, ctx.input.frame);
            match ctx.md_submit(MdAction::DebugText(text)) {
                Ok(_) => self.sent += 1,
                Err(_) => self.rejected += 1,
            }
        }
        Ok(())
    }
    fn summary(&mut self, now: &FrameInput) -> Option<String> {
        Some(format!(
            "X4MP_CORE MDPROBE frame={} sent={} rejected={} ok={} failed={} timeouts={} last_latency_ms={}",
            now.frame,
            self.sent,
            self.rejected,
            self.ok,
            self.failed,
            self.timeouts,
            self.last_latency_ns.map_or("?".to_string(), |ns| format!("{:.3}", ns as f64 / 1e6)),
        ))
    }
}

pub struct Core {
    api: Api,
    driver: FrameDriver,
    mdq: bool,
    owner: Option<ThreadId>,
    wire: String,
    features: String,
}

impl Core {
    pub fn new(
        config: &CoreConfig,
        api: Api,
        report: &BindReport,
        platform: Platform,
        clock: Box<dyn Clock + Send>,
    ) -> Self {
        let driver_config = DriverConfig {
            budget_ns: config.budget_us * 1000,
            frame_budget_ns: config.frame_budget_us * 1000,
            ..Default::default()
        };
        let md = MdQueue::new(MdQueueConfig::default());
        let mut driver = FrameDriver::new(driver_config, clock, md, LogBuf::default());
        for line in report.lines(platform) {
            driver.log.push(line);
        }
        let caps = api.capabilities();
        if config.pose {
            driver.add(Box::new(PlayerPose::new()), None, caps);
        }
        if config.mdq && config.md_probe_s > 0 {
            driver.add(Box::new(MdProbe::new(config.md_probe_s)), None, Capabilities::NONE);
        }
        let features = features_fields(config, &driver.features());
        driver.log.push(format!(
            "X4MP_CORE READY pose={} mdq={} md_probe_s={} budget_us={} frame_budget_us={} writes=0",
            u8::from(config.pose),
            u8::from(config.mdq),
            config.md_probe_s,
            config.budget_us,
            config.frame_budget_us
        ));
        Core { api, driver, mdq: config.mdq, owner: None, wire: String::with_capacity(4096), features }
    }

    /// Feature fields for the host's one `FEATURES` line, as of installation.
    pub fn features(&self) -> &str {
        &self.features
    }

    fn on_owner(&mut self) -> Result<(), &'static str> {
        let me = std::thread::current().id();
        match self.owner {
            None => {
                self.owner = Some(me);
                Ok(())
            }
            Some(owner) if owner == me => Ok(()),
            Some(_) => Err("foreign_thread"),
        }
    }

    pub fn frame(&mut self, input: &FrameInput) -> Result<(), &'static str> {
        self.on_owner()?;
        if self.api.capabilities() == Capabilities::NONE {
            self.driver.run(input, None);
        } else {
            // SAFETY: the host calls `frame` only from its verified per-frame callback on
            // the engine thread, and the `Engine` does not outlive this call.
            let engine = unsafe { Engine::enter(&self.api, false) };
            self.driver.run(input, Some(&engine));
        }
        Ok(())
    }

    /// Copy the next action batch into `buf`. Returns bytes written (0 = nothing), or a
    /// negative `BRIDGE_*` code.
    pub fn md_take(&mut self, buf: &mut [u8]) -> isize {
        if !self.mdq || self.owner.is_none() {
            return BRIDGE_DISABLED;
        }
        if self.owner != Some(std::thread::current().id()) {
            return BRIDGE_FOREIGN_THREAD;
        }
        let now = self.driver.now_ns();
        let count = self.driver.md.take_batch(now, buf.len(), &mut self.wire);
        if count == 0 {
            return 0;
        }
        buf[..self.wire.len()].copy_from_slice(self.wire.as_bytes());
        self.wire.len() as isize
    }

    /// One MD result. Returns 0 accepted, 1 unknown id, or a negative `BRIDGE_*` code.
    pub fn md_result(&mut self, id: u64, code: i32) -> isize {
        if !self.mdq || self.owner.is_none() {
            return BRIDGE_DISABLED;
        }
        if self.owner != Some(std::thread::current().id()) {
            return BRIDGE_FOREIGN_THREAD;
        }
        let Some(status) = MdStatus::from_code(code) else { return BRIDGE_BAD_ARGUMENT };
        let now = self.driver.now_ns();
        if self.driver.md.complete(ActionId(id), status, now) {
            0
        } else {
            1
        }
    }

    pub fn drain_log(&mut self, mut sink: impl FnMut(String)) {
        for line in self.driver.log.drain() {
            sink(line);
        }
    }

    pub fn driver(&self) -> &FrameDriver {
        &self.driver
    }
}

/// Core part of the process's one `FEATURES` line (ENGINE-INSTRUMENTATION.txt rule 4):
/// every core feature with `on`, `off` (flag) or `auto-disabled:<reason>`. The host
/// prefixes `FEATURES process=<name>` and its own features.
pub fn features_fields(config: &CoreConfig, registered: &[(&'static str, String)]) -> String {
    let state = |name: &str, enabled: bool| -> String {
        if !enabled {
            return "off".into();
        }
        registered.iter().find(|(n, _)| *n == name).map(|(_, s)| s.clone()).unwrap_or_else(|| "on".into())
    };
    format!(
        "x4mp_core=on md_queue={} player_pose={} md_probe={} pose_write=off",
        if config.mdq { "on" } else { "off" },
        state("player_pose", config.pose),
        state("md_probe", config.mdq && config.md_probe_s > 0),
    )
}

/// Core feature fields when the core is switched off (`off`) or refused before install
/// (`auto-disabled:<reason>`).
pub fn features_fields_disabled(state: &str) -> String {
    format!("x4mp_core={state} md_queue=off player_pose=off md_probe=off pose_write=off")
}

/// Feature fields of the installed core, if any.
pub fn features() -> Option<String> {
    with_core(true, |core| core.features().to_string())
}

static CORE: Mutex<Option<Core>> = Mutex::new(None);
static CONTENDED_FRAMES: AtomicU64 = AtomicU64::new(0);

enum Access<R> {
    Done(R),
    Absent,
    Busy,
}

fn access<R>(blocking: bool, f: impl FnOnce(&mut Core) -> R) -> Access<R> {
    let mut guard = if blocking {
        CORE.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    } else {
        match CORE.try_lock() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => return Access::Busy,
        }
    };
    match guard.as_mut() {
        Some(core) => Access::Done(f(core)),
        None => Access::Absent,
    }
}

fn with_core<R>(blocking: bool, f: impl FnOnce(&mut Core) -> R) -> Option<R> {
    match access(blocking, f) {
        Access::Done(r) => Some(r),
        _ => None,
    }
}

/// Install the process-wide core. Refused if one is already installed.
pub fn install(config: &CoreConfig, api: Api, report: &BindReport, platform: Platform) -> Result<(), &'static str> {
    let mut guard = CORE.lock().unwrap_or_else(|p| p.into_inner());
    if guard.is_some() {
        return Err("already_installed");
    }
    *guard = Some(Core::new(config, api, report, platform, Box::new(MonotonicClock::new())));
    Ok(())
}

/// Per-frame entry. `Ok` when no core is installed or the lock is briefly held by the
/// log drain (that frame is counted and skipped); `Err` only on a thread violation.
pub fn frame(input: &FrameInput) -> Result<(), &'static str> {
    match access(false, |core| core.frame(input)) {
        Access::Done(result) => result,
        Access::Absent => Ok(()),
        Access::Busy => {
            CONTENDED_FRAMES.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    }
}

fn bridge(r: Access<isize>) -> isize {
    match r {
        Access::Done(code) => code,
        Access::Absent => BRIDGE_DISABLED,
        Access::Busy => BRIDGE_BUSY,
    }
}

pub fn md_take(buf: &mut [u8]) -> isize {
    bridge(access(false, |core| core.md_take(buf)))
}

pub fn md_result(id: u64, code: i32) -> isize {
    bridge(access(false, |core| core.md_result(id, code)))
}

/// Drain log lines (blocking lock; call from a thread that may do I/O, never the frame thread).
pub fn drain_log(sink: impl FnMut(String)) {
    with_core(true, |core| core.drain_log(sink));
}

/// Frames skipped because the log drain held the lock.
pub fn contended_frames() -> u64 {
    CONTENDED_FRAMES.load(Ordering::Relaxed)
}

pub fn installed() -> bool {
    CORE.lock().map(|g| g.is_some()).unwrap_or(false)
}

/// Remove the core (disarm/shutdown). Later frames and bridge calls are no-ops/disabled.
pub fn uninstall() {
    let mut guard = CORE.lock().unwrap_or_else(|p| p.into_inner());
    *guard = None;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ffi::fake;
    use crate::frame::testing::FakeClock;
    use crate::frame::State;
    use crate::mdq::{decode_batch, parse_results};

    fn core(config: CoreConfig, clock: &FakeClock) -> Core {
        let (api, report) = fake::api();
        Core::new(&config, api, &report, Platform::Linux, Box::new(clock.clone()))
    }

    fn input(n: u64) -> FrameInput {
        FrameInput { frame: n, game_time: n as f64 / 10.0, dt: 0.1, paused: false, monotonic_ns: n * 100_000_000 }
    }

    /// Plays the MD executor stub: decode the wire batch, "execute" debug_text, answer.
    fn fake_md(core: &mut Core, executed: &mut Vec<String>) {
        let mut buf = [0u8; 4096];
        let n = core.md_take(&mut buf);
        assert!(n >= 0, "bridge refused: {n}");
        if n == 0 {
            return;
        }
        let batch = decode_batch(std::str::from_utf8(&buf[..n as usize]).unwrap()).unwrap();
        let mut answer = String::new();
        for (id, kind, arg) in batch {
            let status = if kind == "debug_text" {
                executed.push(arg);
                "ok"
            } else {
                "unknown_kind"
            };
            answer.push_str(&format!("{id} {status};"));
        }
        for (id, code) in parse_results(&answer) {
            assert_eq!(core.md_result(id, code), 0);
        }
    }

    #[test]
    fn md_probe_round_trip_through_a_fake_md() {
        let clock = FakeClock::default();
        let config = CoreConfig { mdq: true, md_probe_s: 1, ..Default::default() };
        let mut core = core(config, &clock);
        let mut executed = Vec::new();
        assert_eq!(core.md_take(&mut [0u8; 64]), BRIDGE_DISABLED, "no frame has claimed the thread yet");
        for n in 0..=25 {
            core.frame(&input(n)).unwrap();
            clock.advance(1_000_000);
            fake_md(&mut core, &mut executed);
        }
        assert_eq!(executed.len(), 3, "{executed:?}");
        assert!(executed[0].starts_with("[X4MP_CORE] md probe seq=1 frame=0"));
        let mut lines = Vec::new();
        core.drain_log(|l| lines.push(l));
        let probe = lines.iter().rev().find(|l| l.starts_with("X4MP_CORE MDPROBE")).unwrap();
        assert!(probe.contains("sent=3 rejected=0 ok=2 failed=0 timeouts=0"), "{probe}");
        assert!(lines.iter().any(|l| l.starts_with("X4MP_CORE READY pose=1 mdq=1 md_probe_s=1")));
        assert_eq!(core.features(), "x4mp_core=on md_queue=on player_pose=on md_probe=on pose_write=off");
        assert!(lines.iter().any(|l| l.starts_with("X4MP_CORE BIND platform=linux")));
    }

    #[test]
    fn bridge_disabled_without_mdq_and_rejects_bad_codes() {
        let clock = FakeClock::default();
        let mut off = core(CoreConfig { mdq: false, md_probe_s: 0, ..Default::default() }, &clock);
        off.frame(&input(0)).unwrap();
        assert_eq!(off.md_take(&mut [0u8; 64]), BRIDGE_DISABLED);
        assert_eq!(off.md_result(1, 0), BRIDGE_DISABLED);
        let mut on = core(CoreConfig { md_probe_s: 0, ..Default::default() }, &clock);
        on.frame(&input(0)).unwrap();
        assert_eq!(on.md_take(&mut [0u8; 64]), 0);
        assert_eq!(on.md_result(1, 9), BRIDGE_BAD_ARGUMENT);
        assert_eq!(on.md_result(1, 0), 1, "unknown id");
    }

    #[test]
    fn foreign_threads_are_refused() {
        let clock = FakeClock::default();
        let mut c = core(CoreConfig { mdq: true, pose: true, ..Default::default() }, &clock);
        c.frame(&input(0)).unwrap();
        let mut c = std::thread::spawn(move || {
            assert_eq!(c.frame(&input(1)), Err("foreign_thread"));
            assert_eq!(c.md_take(&mut [0u8; 64]), BRIDGE_FOREIGN_THREAD);
            assert_eq!(c.md_result(1, 0), BRIDGE_FOREIGN_THREAD);
            c
        })
        .join()
        .unwrap();
        assert!(c.frame(&input(2)).is_ok());
    }

    #[test]
    fn features_line_shows_auto_disabled() {
        let clock = FakeClock::default();
        // No bound exports at all: the pose probe cannot run and says why.
        let (api, report) = Api::bind_with(&fake::Symbols::new(Platform::Linux), &fake::build(Platform::Linux), &[]);
        let mut c = Core::new(&CoreConfig::default(), api, &report, Platform::Linux, Box::new(clock.clone()));
        let mut lines = Vec::new();
        c.drain_log(|l| lines.push(l));
        assert!(
            lines.iter().any(|l| l.starts_with("FEATURE player_pose DISABLED reason=missing_capabilities")),
            "{lines:?}"
        );
        assert_eq!(
            c.features(),
            "x4mp_core=on md_queue=on player_pose=auto-disabled:missing_capabilities md_probe=on pose_write=off"
        );
        let off = features_fields(&CoreConfig { pose: false, mdq: false, md_probe_s: 0, ..Default::default() }, &[]);
        assert_eq!(off, "x4mp_core=on md_queue=off player_pose=off md_probe=off pose_write=off");
        assert!(features_fields_disabled("off").starts_with("x4mp_core=off md_queue=off"));
    }

    #[test]
    fn pose_runs_with_fake_engine() {
        let clock = FakeClock::default();
        let mut c = core(CoreConfig { pose: true, ..Default::default() }, &clock);
        let mut world = fake::World { occupied: 5, ..Default::default() };
        world.sector_of.insert(5, 6);
        fake::reset(world);
        for n in 0..=10 {
            fake::with(|w| {
                w.game_time = n as f64 / 10.0;
                w.poses.insert(5, crate::ffi::UiPosRot { x: n as f32, ..Default::default() });
            });
            c.frame(&input(n)).unwrap();
        }
        assert_eq!(c.driver().state(0), Some(State::Active));
        let mut lines = Vec::new();
        c.drain_log(|l| lines.push(l));
        assert!(
            lines.iter().any(|l| l.starts_with("X4MP_CORE POSE") && l.contains(" vel=10.00,0.00,0.00")),
            "{lines:?}"
        );
        assert!(lines.iter().any(|l| l.starts_with("X4MP_CORE COST name=player_pose state=active")));
    }

    #[test]
    fn global_instance_lifecycle() {
        // The only test touching the process-wide instance.
        assert_eq!(frame(&input(0)), Ok(()), "no core installed: no-op");
        assert_eq!(md_take(&mut [0u8; 16]), BRIDGE_DISABLED);
        let (api, report) = fake::api();
        install(&CoreConfig { md_probe_s: 0, ..Default::default() }, api, &report, Platform::Linux).unwrap();
        assert!(installed());
        assert!(features().unwrap().starts_with("x4mp_core=on md_queue=on"));
        let (api, report) = fake::api();
        assert_eq!(install(&CoreConfig::default(), api, &report, Platform::Linux), Err("already_installed"));
        frame(&input(1)).unwrap();
        assert_eq!(md_take(&mut [0u8; 64]), 0);
        let mut lines = 0;
        drain_log(|_| lines += 1);
        assert!(lines >= 2);
        uninstall();
        assert!(!installed());
    }
}
