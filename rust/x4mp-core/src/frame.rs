//! Frame driver: runs the registered subsystems once per simulation frame, with a time
//! budget per subsystem and per frame, cost counters, and 1 Hz `X4MP_CORE COST` lines in
//! the style of the hooks crate's `HASHCOST`.
//!
//! Every subsystem is a feature in the sense of ENGINE-INSTRUMENTATION.txt rule 4: a
//! suspension logs `FEATURE <name> DISABLED reason=...`, and [`FrameDriver::features`]
//! feeds the process's one `FEATURES` line.
//!
//! Failure policy (fail closed per subsystem, never per process):
//! - `Err` from `on_frame`, or a panic: that subsystem is suspended for the session.
//! - Over its budget on `suspend_after` consecutive frames: suspended (`over_budget`).
//! - Missing capabilities at registration: suspended before it ever runs.
//! - When the frame budget is spent, the remaining subsystems are skipped this frame.
//!
//! Budgets are cooperative: an engine call cannot be pre-empted. Subsystems can check
//! [`FrameCtx::remaining_ns`] between calls. Timings are wall time (monotonic clock).

use std::fmt::Write as _;
use std::panic::{catch_unwind, AssertUnwindSafe};

use crate::ffi::{Capabilities, Engine};
use crate::logbuf::LogBuf;
use crate::mdq::{ActionId, Backpressure, MdAction, MdQueue, MdResult};

/// What the host observed for this frame.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct FrameInput {
    /// Host callback counter (not save-persistent).
    pub frame: u64,
    /// Engine game time (s) as the host read it.
    pub game_time: f64,
    /// Game-time step of this frame (s).
    pub dt: f64,
    pub paused: bool,
    /// Host monotonic clock at callback entry (ns).
    pub monotonic_ns: u64,
}

/// Monotonic time source (ns). Injected so tests control time.
pub trait Clock {
    fn now_ns(&self) -> u64;
}

/// `std::time::Instant` relative to the first use.
pub struct MonotonicClock(std::time::Instant);

impl MonotonicClock {
    pub fn new() -> Self {
        MonotonicClock(std::time::Instant::now())
    }
}
impl Default for MonotonicClock {
    fn default() -> Self {
        Self::new()
    }
}
impl Clock for MonotonicClock {
    fn now_ns(&self) -> u64 {
        self.0.elapsed().as_nanos() as u64
    }
}

pub trait Subsystem: Send {
    fn name(&self) -> &'static str;
    /// Capabilities that must be bound; otherwise the subsystem never runs.
    fn needs(&self) -> Capabilities {
        Capabilities::NONE
    }
    fn on_frame(&mut self, ctx: &mut FrameCtx<'_>) -> Result<(), &'static str>;
    /// Optional summary line, called once per report interval after the COST lines.
    fn summary(&mut self, _now: &FrameInput) -> Option<String> {
        None
    }
}

/// Per-call context handed to a subsystem.
pub struct FrameCtx<'a> {
    pub input: &'a FrameInput,
    engine: Option<&'a Engine<'a>>,
    md: &'a mut MdQueue,
    log: &'a mut LogBuf,
    owner: u16,
    clock: &'a dyn Clock,
    deadline_ns: u64,
}

impl<'a> FrameCtx<'a> {
    /// Engine access, `None` when the host has no bound API this frame.
    pub fn engine(&self) -> Option<&Engine<'a>> {
        self.engine
    }
    pub fn md_submit(&mut self, action: MdAction) -> Result<ActionId, Backpressure> {
        self.md.submit(self.owner, action, self.clock.now_ns())
    }
    pub fn md_result(&mut self) -> Option<MdResult> {
        self.md.take_result(self.owner)
    }
    pub fn log(&mut self, line: String) {
        self.log.push(line);
    }
    pub fn now_ns(&self) -> u64 {
        self.clock.now_ns()
    }
    /// Budget left for this subsystem in this frame.
    pub fn remaining_ns(&self) -> u64 {
        self.deadline_ns.saturating_sub(self.clock.now_ns())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Active,
    Suspended(&'static str),
}

/// Cost counters of one subsystem. `d*`/`window_*` fields reset at every report.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Cost {
    pub calls: u64,
    pub cumulative_ns: u64,
    pub last_ns: u64,
    pub max_ns: u64,
    pub over_budget: u64,
    pub consecutive_over: u32,
    pub errors: u64,
    pub skipped: u64,
    pub window_calls: u64,
    pub window_ns: u64,
    pub window_max_ns: u64,
    pub window_over: u64,
}

struct Slot {
    subsystem: Box<dyn Subsystem>,
    budget_ns: u64,
    state: State,
    cost: Cost,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DriverConfig {
    /// Default per-subsystem budget.
    pub budget_ns: u64,
    /// Total core time per frame before remaining subsystems are skipped.
    pub frame_budget_ns: u64,
    /// Consecutive over-budget frames before a subsystem is suspended.
    pub suspend_after: u32,
    /// Report interval on the host's monotonic clock.
    pub report_interval_ns: u64,
}

impl Default for DriverConfig {
    fn default() -> Self {
        DriverConfig {
            budget_ns: 500_000,
            frame_budget_ns: 2_000_000,
            suspend_after: 30,
            report_interval_ns: 1_000_000_000,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct FrameTotals {
    pub frames: u64,
    pub paused_frames: u64,
    pub cumulative_ns: u64,
    pub window_frames: u64,
    pub window_ns: u64,
    pub window_max_ns: u64,
}

pub struct FrameDriver {
    config: DriverConfig,
    clock: Box<dyn Clock + Send>,
    slots: Vec<Slot>,
    pub md: MdQueue,
    pub log: LogBuf,
    totals: FrameTotals,
    next_report_ns: Option<u64>,
}

impl FrameDriver {
    pub fn new(config: DriverConfig, clock: Box<dyn Clock + Send>, md: MdQueue, log: LogBuf) -> Self {
        FrameDriver { config, clock, slots: Vec::new(), md, log, totals: FrameTotals::default(), next_report_ns: None }
    }

    /// Register a subsystem with `budget_ns` (or the default). `available` are the bound
    /// capabilities; a subsystem whose needs are not met starts suspended.
    pub fn add(&mut self, subsystem: Box<dyn Subsystem>, budget_ns: Option<u64>, available: Capabilities) -> u16 {
        let state = if available.contains(subsystem.needs()) {
            State::Active
        } else {
            self.log.push(format!(
                "FEATURE {} DISABLED reason=missing_capabilities needs={} have={}",
                subsystem.name(),
                subsystem.needs().names(),
                available.names()
            ));
            State::Suspended("missing_capabilities")
        };
        self.slots.push(Slot {
            subsystem,
            budget_ns: budget_ns.unwrap_or(self.config.budget_ns),
            state,
            cost: Cost::default(),
        });
        (self.slots.len() - 1) as u16
    }

    pub fn state(&self, index: u16) -> Option<State> {
        self.slots.get(index as usize).map(|s| s.state)
    }
    pub fn cost(&self, index: u16) -> Option<Cost> {
        self.slots.get(index as usize).map(|s| s.cost)
    }
    /// `(name, "on" | "auto-disabled:<reason>")` per registered subsystem.
    pub fn features(&self) -> Vec<(&'static str, String)> {
        self.slots
            .iter()
            .map(|slot| {
                let state = match slot.state {
                    State::Active => "on".to_string(),
                    State::Suspended(reason) => format!("auto-disabled:{reason}"),
                };
                (slot.subsystem.name(), state)
            })
            .collect()
    }

    pub fn totals(&self) -> FrameTotals {
        self.totals
    }
    pub fn now_ns(&self) -> u64 {
        self.clock.now_ns()
    }

    fn suspend(log: &mut LogBuf, slot: &mut Slot, reason: &'static str) {
        slot.state = State::Suspended(reason);
        log.push(format!("FEATURE {} DISABLED reason={reason}", slot.subsystem.name()));
    }

    /// Run one frame. `engine` is `None` when nothing is bound (subsystems still run and
    /// see no engine).
    pub fn run(&mut self, input: &FrameInput, engine: Option<&Engine<'_>>) {
        let start = self.clock.now_ns();
        self.md.expire(start);
        let frame_deadline = start.saturating_add(self.config.frame_budget_ns);
        for (index, slot) in self.slots.iter_mut().enumerate() {
            if slot.state != State::Active {
                continue;
            }
            let before = self.clock.now_ns();
            if before >= frame_deadline {
                slot.cost.skipped += 1;
                continue;
            }
            let mut ctx = FrameCtx {
                input,
                engine,
                md: &mut self.md,
                log: &mut self.log,
                owner: index as u16,
                clock: &*self.clock,
                deadline_ns: before.saturating_add(slot.budget_ns).min(frame_deadline),
            };
            let result = catch_unwind(AssertUnwindSafe(|| slot.subsystem.on_frame(&mut ctx)));
            let elapsed = self.clock.now_ns().saturating_sub(before);
            let cost = &mut slot.cost;
            cost.calls += 1;
            cost.window_calls += 1;
            cost.cumulative_ns += elapsed;
            cost.window_ns += elapsed;
            cost.last_ns = elapsed;
            cost.max_ns = cost.max_ns.max(elapsed);
            cost.window_max_ns = cost.window_max_ns.max(elapsed);
            if elapsed > slot.budget_ns {
                cost.over_budget += 1;
                cost.window_over += 1;
                cost.consecutive_over += 1;
            } else {
                cost.consecutive_over = 0;
            }
            match result {
                Ok(Ok(())) => {
                    if cost.consecutive_over >= self.config.suspend_after {
                        Self::suspend(&mut self.log, slot, "over_budget");
                    }
                }
                Ok(Err(reason)) => {
                    slot.cost.errors += 1;
                    Self::suspend(&mut self.log, slot, reason);
                }
                Err(_) => {
                    slot.cost.errors += 1;
                    Self::suspend(&mut self.log, slot, "panic");
                }
            }
        }
        let spent = self.clock.now_ns().saturating_sub(start);
        let t = &mut self.totals;
        t.frames += 1;
        t.paused_frames += u64::from(input.paused);
        t.cumulative_ns += spent;
        t.window_frames += 1;
        t.window_ns += spent;
        t.window_max_ns = t.window_max_ns.max(spent);
        self.maybe_report(input);
    }

    fn maybe_report(&mut self, input: &FrameInput) {
        let due = *self.next_report_ns.get_or_insert(input.monotonic_ns.saturating_add(self.config.report_interval_ns));
        if input.monotonic_ns < due {
            return;
        }
        self.next_report_ns = Some(input.monotonic_ns.saturating_add(self.config.report_interval_ns));
        for line in self.report_lines(input) {
            self.log.push(line);
        }
    }

    /// COST line per subsystem, then subsystem summaries, then the FRAME line. Resets the
    /// window counters.
    pub fn report_lines(&mut self, input: &FrameInput) -> Vec<String> {
        let ms = |ns: u64| ns as f64 / 1e6;
        let mut out = Vec::with_capacity(self.slots.len() * 2 + 1);
        for slot in &mut self.slots {
            let c = &mut slot.cost;
            let state = match slot.state {
                State::Active => "active".to_string(),
                State::Suspended(reason) => format!("suspended:{reason}"),
            };
            out.push(format!(
                "X4MP_CORE COST name={} state={} frame={} calls={} dcalls={} last_ms={:.3} max_ms={:.3} total_ms={:.3} cumulative_ms={:.3} over_budget={} dover={} errors={} skipped={} budget_ms={:.3}",
                slot.subsystem.name(),
                state,
                input.frame,
                c.calls,
                c.window_calls,
                ms(c.last_ns),
                ms(c.window_max_ns),
                ms(c.window_ns),
                ms(c.cumulative_ns),
                c.over_budget,
                c.window_over,
                c.errors,
                c.skipped,
                ms(slot.budget_ns),
            ));
            c.window_calls = 0;
            c.window_ns = 0;
            c.window_max_ns = 0;
            c.window_over = 0;
        }
        for slot in &mut self.slots {
            if slot.state == State::Active {
                if let Some(line) = slot.subsystem.summary(input) {
                    out.push(line);
                }
            }
        }
        let t = &mut self.totals;
        let q = self.md.counters();
        let mut line = String::with_capacity(320);
        let _ = write!(
            line,
            "X4MP_CORE FRAME frame={} frames={} dframes={} paused={} game={:.3} core_ms={:.3} core_max_ms={:.3} cumulative_ms={:.3} md_pending={} md_inflight={} md_submitted={} md_rejected={} md_sent={} md_ok={} md_failed={} md_unknown_kind={} md_timeouts={} md_unknown_results={} log_dropped={}",
            input.frame,
            t.frames,
            t.window_frames,
            u8::from(input.paused),
            input.game_time,
            ms(t.window_ns),
            ms(t.window_max_ns),
            ms(t.cumulative_ns),
            self.md.pending(),
            self.md.in_flight(),
            q.submitted,
            q.rejected,
            q.sent,
            q.completed_ok,
            q.completed_failed,
            q.unknown_kind,
            q.timeouts,
            q.unknown_results,
            self.log.dropped(),
        );
        out.push(line);
        t.window_frames = 0;
        t.window_ns = 0;
        t.window_max_ns = 0;
        out
    }
}

#[cfg(test)]
pub(crate) mod testing {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Arc;

    /// Manually advanced clock shared between the test and the driver.
    #[derive(Clone, Default)]
    pub struct FakeClock(pub Arc<AtomicU64>);
    impl FakeClock {
        pub fn advance(&self, ns: u64) {
            self.0.fetch_add(ns, Ordering::SeqCst);
        }
    }
    impl Clock for FakeClock {
        fn now_ns(&self) -> u64 {
            self.0.load(Ordering::SeqCst)
        }
    }

    pub fn driver(clock: &FakeClock, config: DriverConfig) -> FrameDriver {
        FrameDriver::new(config, Box::new(clock.clone()), MdQueue::new(Default::default()), LogBuf::new(64))
    }
}

#[cfg(test)]
mod tests {
    use super::testing::*;
    use super::*;
    use crate::ffi::Capability;

    /// Spends `cost_ns` of fake time per frame, optionally failing or panicking at a frame.
    struct Probe {
        clock: FakeClock,
        cost_ns: u64,
        fail_at: Option<u64>,
        panic_at: Option<u64>,
        seen: u64,
        needs: Capabilities,
    }

    impl Subsystem for Probe {
        fn name(&self) -> &'static str {
            "probe"
        }
        fn needs(&self) -> Capabilities {
            self.needs
        }
        fn on_frame(&mut self, ctx: &mut FrameCtx<'_>) -> Result<(), &'static str> {
            self.seen += 1;
            self.clock.advance(self.cost_ns);
            if Some(ctx.input.frame) == self.panic_at {
                panic!("probe panic");
            }
            if Some(ctx.input.frame) == self.fail_at {
                return Err("probe_failed");
            }
            Ok(())
        }
        fn summary(&mut self, _now: &FrameInput) -> Option<String> {
            Some(format!("X4MP_CORE PROBE seen={}", self.seen))
        }
    }

    fn probe(clock: &FakeClock, cost_ns: u64) -> Probe {
        Probe { clock: clock.clone(), cost_ns, fail_at: None, panic_at: None, seen: 0, needs: Capabilities::NONE }
    }

    fn frame(n: u64) -> FrameInput {
        FrameInput { frame: n, game_time: n as f64 / 60.0, dt: 1.0 / 60.0, paused: false, monotonic_ns: n * 16_666_667 }
    }

    #[test]
    fn measures_cost_and_reports_like_hashcost() {
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig::default());
        let id = d.add(Box::new(probe(&clock, 100_000)), Some(500_000), Capabilities::NONE);
        for n in 1..=60 {
            d.run(&frame(n), None);
        }
        let cost = d.cost(id).unwrap();
        assert_eq!((cost.calls, cost.cumulative_ns, cost.max_ns, cost.over_budget), (60, 6_000_000, 100_000, 0));
        assert_eq!(d.state(id), Some(State::Active));
        // First report fires one interval after the first frame (frame 61 at ~1.0167 s).
        d.run(&frame(61), None);
        let lines: Vec<String> = d.log.drain().collect();
        let cost_line = lines.iter().find(|l| l.starts_with("X4MP_CORE COST ")).expect("cost line");
        assert!(
            cost_line.contains(
                "name=probe state=active frame=61 calls=61 dcalls=61 last_ms=0.100 max_ms=0.100 total_ms=6.100"
            ),
            "{cost_line}"
        );
        assert!(cost_line.ends_with("over_budget=0 dover=0 errors=0 skipped=0 budget_ms=0.500"), "{cost_line}");
        assert!(lines.iter().any(|l| l == "X4MP_CORE PROBE seen=61"));
        assert!(
            lines.last().unwrap().starts_with("X4MP_CORE FRAME frame=61 frames=61 dframes=61 paused=0 "),
            "{lines:?}"
        );
        assert_eq!(d.cost(id).unwrap().window_calls, 0);
    }

    #[test]
    fn sustained_overrun_suspends_only_that_subsystem() {
        let clock = FakeClock::default();
        let config = DriverConfig { suspend_after: 3, ..Default::default() };
        let mut d = driver(&clock, config);
        let slow = d.add(Box::new(probe(&clock, 600_000)), Some(500_000), Capabilities::NONE);
        let fast = d.add(Box::new(probe(&clock, 1_000)), None, Capabilities::NONE);
        for n in 1..=2 {
            d.run(&frame(n), None);
        }
        assert_eq!(d.state(slow), Some(State::Active));
        d.run(&frame(3), None);
        assert_eq!(d.state(slow), Some(State::Suspended("over_budget")));
        d.run(&frame(4), None);
        assert_eq!(d.cost(slow).unwrap().calls, 3);
        assert_eq!(d.cost(fast).unwrap().calls, 4);
        assert!(d.log.drain().any(|l| l == "FEATURE probe DISABLED reason=over_budget"));
    }

    #[test]
    fn a_single_overrun_does_not_suspend() {
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig { suspend_after: 2, ..Default::default() });
        let mut p = probe(&clock, 600_000);
        p.cost_ns = 600_000;
        let id = d.add(Box::new(p), Some(500_000), Capabilities::NONE);
        d.run(&frame(1), None);
        assert_eq!(d.cost(id).unwrap().consecutive_over, 1);
        assert_eq!(d.state(id), Some(State::Active));
    }

    #[test]
    fn errors_and_panics_suspend_without_stopping_the_frame() {
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig::default());
        let mut failing = probe(&clock, 10);
        failing.fail_at = Some(2);
        let mut panicking = probe(&clock, 10);
        panicking.panic_at = Some(1);
        let f = d.add(Box::new(failing), None, Capabilities::NONE);
        let p = d.add(Box::new(panicking), None, Capabilities::NONE);
        let ok = d.add(Box::new(probe(&clock, 10)), None, Capabilities::NONE);
        let hook = std::panic::take_hook();
        std::panic::set_hook(Box::new(|_| {}));
        for n in 1..=3 {
            d.run(&frame(n), None);
        }
        std::panic::set_hook(hook);
        assert_eq!(d.state(f), Some(State::Suspended("probe_failed")));
        assert_eq!(d.state(p), Some(State::Suspended("panic")));
        assert_eq!(d.cost(ok).unwrap().calls, 3);
        assert_eq!((d.cost(f).unwrap().errors, d.cost(p).unwrap().errors), (1, 1));
    }

    #[test]
    fn frame_budget_skips_the_rest() {
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig { frame_budget_ns: 1_000_000, ..Default::default() });
        let hog = d.add(Box::new(probe(&clock, 1_000_000)), Some(2_000_000), Capabilities::NONE);
        let next = d.add(Box::new(probe(&clock, 10)), None, Capabilities::NONE);
        d.run(&frame(1), None);
        assert_eq!(d.cost(hog).unwrap().calls, 1);
        assert_eq!((d.cost(next).unwrap().calls, d.cost(next).unwrap().skipped), (0, 1));
    }

    #[test]
    fn missing_capabilities_never_run() {
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig::default());
        let mut p = probe(&clock, 10);
        p.needs = Capabilities::of(&[Capability::PoseRead]);
        let id = d.add(Box::new(p), None, Capabilities::of(&[Capability::Clock]));
        d.run(&frame(1), None);
        assert_eq!(d.state(id), Some(State::Suspended("missing_capabilities")));
        assert_eq!(d.cost(id).unwrap().calls, 0);
        let line = d.log.drain().next().unwrap();
        assert!(
            line.starts_with("FEATURE probe DISABLED reason=missing_capabilities needs=pose_read have=clock"),
            "{line}"
        );
        assert_eq!(d.features(), vec![("probe", "auto-disabled:missing_capabilities".to_string())]);
    }

    #[test]
    fn md_results_route_to_their_submitter() {
        struct Sender {
            sent: Option<ActionId>,
            got: Option<MdResult>,
        }
        impl Subsystem for Sender {
            fn name(&self) -> &'static str {
                "sender"
            }
            fn on_frame(&mut self, ctx: &mut FrameCtx<'_>) -> Result<(), &'static str> {
                if self.sent.is_none() {
                    self.sent = Some(ctx.md_submit(MdAction::DebugText("hi".into())).map_err(|_| "md")?);
                }
                if let Some(r) = ctx.md_result() {
                    self.got = Some(r);
                    return Err("done");
                }
                Ok(())
            }
        }
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig::default());
        d.add(Box::new(probe(&clock, 1)), None, Capabilities::NONE);
        let s = d.add(Box::new(Sender { sent: None, got: None }), None, Capabilities::NONE);
        d.run(&frame(1), None);
        let mut wire = String::new();
        assert_eq!(d.md.take_batch(0, 4096, &mut wire), 1);
        assert!(d.md.complete(ActionId(1), crate::mdq::MdStatus::Ok, 5));
        d.run(&frame(2), None);
        assert_eq!(d.state(s), Some(State::Suspended("done")));
    }
}
