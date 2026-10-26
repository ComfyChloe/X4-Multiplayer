//! Fixed 60 Hz simulation stepping decoupled from rendering (platform-independent logic).
//!
//! The engine pairs exactly one `FrameTick` (main thread: advance time, release
//! semSimStart, then render the previous frame) with one MoveWorker simulation frame.
//! With the dt patch every `FrameTick` advances game time by exactly `speed / 60`, so
//! one simulation frame is one fixed step. The scheduler never adds or removes
//! semaphore traffic. Per step it chooses whether the **next** `FrameTick` renders
//! (normal branch) or takes the engine's own no-render branch, and it paces steps
//! against the wall clock:
//!
//! - **Pacing.** Step `k` after the baseline may not be followed by the next `FrameTick`
//!   before `epoch + k/60 s`; the caller sleeps until then (VBI slot 13).
//! - **Catch-up.** When steps run late, renders are skipped so `FrameTick`s run back to
//!   back at simulation speed until the step clock is back on wall time.
//! - **Bounded catch-up.** At most `max_catchup_steps` consecutive skipped renders while
//!   behind, then one render is forced (clients keep a picture).
//! - **Spiral-of-death guard.** Lateness above `max_lag_ms` is dropped (the epoch moves
//!   forward) and reported; game time then runs slower than wall time instead of the
//!   loop trying to repay an unbounded debt.
//! - **Loading** frames reset the baseline (a load is not catch-up debt) and render.
//! - **Temporary pause** (save countdown) renders whenever the interval allows, so the
//!   engine's 3-frame countdown is shown, even while behind.
//! - **Pause / SETA** need no rule: speed only scales the game time per step; the step
//!   clock stays 60 Hz wall time and game time stays monotonic.

pub const STEPS_PER_SECOND: u64 = 60;
pub const FIXED_DT: f64 = 1.0 / 60.0;
const NS_PER_S: u64 = 1_000_000_000;

/// Wall-clock offset of step `k` from the epoch, exact (no accumulated rounding).
pub fn step_offset_ns(k: u64) -> u64 {
    ((k as u128 * NS_PER_S as u128) / STEPS_PER_SECOND as u128) as u64
}
pub const STEP_NS: u64 = NS_PER_S / STEPS_PER_SECOND;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RenderPolicy {
    /// Render when at least this many ms passed since the last render and the step clock
    /// is not behind. `0` renders every step that is on time (client behaviour).
    IntervalMs(u64),
    /// Never render (headless server). Every `FrameTick` takes the no-render branch.
    Never,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SchedConfig {
    pub render: RenderPolicy,
    /// Consecutive skipped renders while behind before one render is forced.
    pub max_catchup_steps: u32,
    /// Lateness kept as catch-up debt; anything beyond is dropped.
    pub max_lag_ms: u64,
}

impl Default for SchedConfig {
    fn default() -> Self {
        SchedConfig { render: RenderPolicy::IntervalMs(0), max_catchup_steps: 120, max_lag_ms: 1000 }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Inputs {
    /// Monotonic clock (ns), sampled after the main thread was seen parked.
    pub now_ns: u64,
    /// VBI loading flag set (or the time guard saw no advance after a load).
    pub loading: bool,
    /// TimerBase temporary pause count non-zero (save countdown).
    pub temp_paused: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Decision {
    /// Value for the FrameState render flag before the next `FrameTick`.
    pub render: bool,
    /// Sleep on the simulation thread until this monotonic time (ns) before returning.
    pub sleep_until_ns: Option<u64>,
    /// Steps of lateness at this decision (0 when on time).
    pub lag_steps: u64,
}

/// Cumulative counters (monotonic) plus window maxima the logger resets.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Stats {
    pub steps: u64,
    pub renders: u64,
    pub skipped: u64,
    pub forced_renders: u64,
    pub loading_steps: u64,
    pub dropped_ns: u64,
    pub rebaselines: u64,
    pub max_lag_steps: u64,
    pub max_skip_run: u64,
}

#[derive(Debug, Clone)]
pub struct Scheduler {
    cfg: SchedConfig,
    epoch_ns: Option<u64>,
    steps_since_epoch: u64,
    last_render_ns: Option<u64>,
    skip_run: u64,
    pub stats: Stats,
}

impl Scheduler {
    pub fn new(cfg: SchedConfig) -> Self {
        Scheduler {
            cfg,
            epoch_ns: None,
            steps_since_epoch: 0,
            last_render_ns: None,
            skip_run: 0,
            stats: Stats::default(),
        }
    }

    /// Consecutive skipped renders so far.
    pub fn current_skip_run(&self) -> u64 {
        self.skip_run
    }

    pub fn config(&self) -> SchedConfig {
        self.cfg
    }

    /// Forget the wall-clock baseline (next step starts a new one).
    pub fn rebaseline(&mut self) {
        if self.epoch_ns.is_some() {
            self.stats.rebaselines += 1;
        }
        self.epoch_ns = None;
        self.steps_since_epoch = 0;
    }

    /// Called once per simulation frame (VBI slot 13), i.e. once per `FrameTick`.
    pub fn step(&mut self, inp: Inputs) -> Decision {
        if inp.loading {
            self.stats.loading_steps += 1;
            self.rebaseline();
            self.skip_run = 0;
            return Decision { render: true, sleep_until_ns: None, lag_steps: 0 };
        }
        let now = inp.now_ns;
        let epoch = *self.epoch_ns.get_or_insert(now);
        self.steps_since_epoch += 1;
        self.stats.steps += 1;
        let target = epoch.saturating_add(step_offset_ns(self.steps_since_epoch));
        let mut lag_ns = now.saturating_sub(target);
        let max_lag_ns = self.cfg.max_lag_ms.saturating_mul(1_000_000);
        if lag_ns > max_lag_ns {
            let drop = lag_ns - max_lag_ns;
            self.epoch_ns = Some(epoch + drop);
            self.stats.dropped_ns += drop;
            lag_ns = max_lag_ns;
        }
        let target = self.epoch_ns.unwrap_or(epoch).saturating_add(step_offset_ns(self.steps_since_epoch));
        let lag_steps = lag_ns / STEP_NS;
        let behind = lag_steps >= 1;
        self.stats.max_lag_steps = self.stats.max_lag_steps.max(lag_steps);

        let render = match self.cfg.render {
            RenderPolicy::Never => false,
            RenderPolicy::IntervalMs(ms) => {
                let due = match self.last_render_ns {
                    None => true,
                    Some(t) => now.saturating_sub(t) >= ms.saturating_mul(1_000_000),
                };
                let forced = behind && self.skip_run >= self.cfg.max_catchup_steps as u64;
                if due && forced {
                    self.stats.forced_renders += 1;
                }
                due && (!behind || inp.temp_paused || forced)
            }
        };
        if render {
            self.stats.renders += 1;
            self.last_render_ns = Some(now);
            self.skip_run = 0;
        } else {
            self.stats.skipped += 1;
            self.skip_run += 1;
            self.stats.max_skip_run = self.stats.max_skip_run.max(self.skip_run);
        }
        let sleep_until_ns = (now < target).then_some(target);
        Decision { render, sleep_until_ns, lag_steps }
    }
}

// ----------------------------------------------------------------------------------
// The slot-13 protocol, generic over the engine so the synthetic harness runs it too.

/// What the scheduler needs from the running engine (or a fake of it).
pub trait Engine {
    /// The main thread has returned from `FrameTick` and waits for semSimDone of the
    /// simulation frame that is calling us (Linux: main-loop frame-in-flight byte == 1).
    fn main_parked(&self) -> bool;
    fn now_ns(&self) -> u64;
    /// Short back-off while waiting for the main thread (render in progress).
    fn pause(&self);
    fn sleep_until(&self, ns: u64);
    /// Current value of the render flag (the guard compares it with the last write).
    fn render_flag(&self) -> bool;
    fn set_render_flag(&self, render: bool);
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Slot13 {
    /// Decision written (when it changed) and pacing done.
    Stepped { decision: Decision, waited_ns: u64 },
    /// The main thread stayed inside `FrameTick` for the whole timeout. Nothing was
    /// written: both of `FrameTick`'s render-flag reads keep seeing the old value.
    WaitTimeout { waited_ns: u64 },
    /// The render flag no longer holds the value this scheduler wrote last: another
    /// writer exists. Fatal.
    ForeignFlagWrite,
}

/// One simulation frame of the scheduler, run in VBI slot 13 on the MoveWorker.
///
/// The render flag is written only while the main thread is parked outside `FrameTick`
/// (it cannot enter `FrameTick` before this frame releases semSimDone), so the two reads
/// in one `FrameTick` always agree and the write happens-before them through the
/// semaphore. `last_written` tracks the flag value this scheduler owns.
pub fn slot13_step(
    sched: &mut Scheduler,
    engine: &impl Engine,
    last_written: &mut bool,
    loading: bool,
    temp_paused: bool,
    wait_timeout_ns: u64,
) -> Slot13 {
    let start = engine.now_ns();
    while !engine.main_parked() {
        let waited = engine.now_ns().saturating_sub(start);
        if waited >= wait_timeout_ns {
            return Slot13::WaitTimeout { waited_ns: waited };
        }
        engine.pause();
    }
    let now = engine.now_ns();
    let waited_ns = now.saturating_sub(start);
    if engine.render_flag() != *last_written {
        return Slot13::ForeignFlagWrite;
    }
    let decision = sched.step(Inputs { now_ns: now, loading, temp_paused });
    if decision.render != *last_written {
        engine.set_render_flag(decision.render);
        *last_written = decision.render;
    }
    if let Some(t) = decision.sleep_until_ns {
        engine.sleep_until(t);
    }
    Slot13::Stepped { decision, waited_ns }
}

// ----------------------------------------------------------------------------------
// Time guard: proves that every simulation frame was one fixed step.

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TimeSample {
    pub game: f64,
    pub real: f64,
    pub speed: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepCheck {
    /// realTime +1/60 and game time + previous speed / 60.
    Exact,
    /// realTime +1/60 but game time moved differently (SetGameTime, speed written after
    /// slot 13). Counted, not fatal: the dt patch is still in effect.
    GameIrregular,
    /// realTime unchanged: a `FrameTick` without time advance (end of loading).
    NoAdvance,
    /// realTime moved by anything else: the fixed dt is not in effect. Fatal.
    RealTimeStep,
    /// A non-finite or out-of-range value. Fatal.
    Invalid,
}

const REAL_TOL: f64 = 1e-6;

pub fn check_step(prev: &TimeSample, cur: &TimeSample) -> StepCheck {
    for v in [prev.game, prev.real, prev.speed, cur.game, cur.real, cur.speed] {
        if !v.is_finite() {
            return StepCheck::Invalid;
        }
    }
    if !(0.0..=64.0).contains(&cur.speed) {
        return StepCheck::Invalid;
    }
    let dreal = cur.real - prev.real;
    if dreal == 0.0 {
        return StepCheck::NoAdvance;
    }
    if (dreal - FIXED_DT).abs() > REAL_TOL {
        return StepCheck::RealTimeStep;
    }
    let dgame = cur.game - prev.game;
    if (dgame - prev.speed * FIXED_DT).abs() <= REAL_TOL {
        StepCheck::Exact
    } else {
        StepCheck::GameIrregular
    }
}

// ----------------------------------------------------------------------------------
// Log line

/// Values the frame callback publishes for the per-second `SCHED` line.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct SchedSnapshot {
    pub stats: Stats,
    pub exact_steps: u64,
    pub irregular_steps: u64,
    pub game_time: f64,
    pub speed: f64,
    /// Longest wait for the main thread to leave FrameTick (≈ render time), window.
    pub max_wait_ns: u64,
    pub wait_timeouts: u64,
}

pub fn sched_line(state: &str, prev: &SchedSnapshot, cur: &SchedSnapshot, wall_ms: u64) -> String {
    let wall_s = wall_ms.max(1) as f64 / 1000.0;
    let dsteps = cur.stats.steps.saturating_sub(prev.stats.steps);
    let drenders = cur.stats.renders.saturating_sub(prev.stats.renders);
    let dgame = cur.game_time - prev.game_time;
    let ratio = dgame / wall_s;
    let per_speed = if cur.speed > 0.0 { ratio / cur.speed } else { 0.0 };
    format!(
        "X4MP_AGENT SCHED state={state} sps={:.2} rps={:.2} game_wall={:.4} game_wall_per_speed={:.4} speed={:.3} max_lag_steps={} max_skip_run={} dropped_ms={:.1} ddropped_ms={:.1} forced={} rebaselines={} loading_steps={} exact={} irregular={} max_wait_ms={:.1} wait_timeouts={} steps={} renders={} wall_ms={}",
        dsteps as f64 / wall_s,
        drenders as f64 / wall_s,
        ratio,
        per_speed,
        cur.speed,
        cur.stats.max_lag_steps,
        cur.stats.max_skip_run,
        cur.stats.dropped_ns as f64 / 1e6,
        cur.stats.dropped_ns.saturating_sub(prev.stats.dropped_ns) as f64 / 1e6,
        cur.stats.forced_renders,
        cur.stats.rebaselines,
        cur.stats.loading_steps,
        cur.exact_steps,
        cur.irregular_steps,
        cur.max_wait_ns as f64 / 1e6,
        cur.wait_timeouts,
        cur.stats.steps,
        cur.stats.renders,
        wall_ms,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const MS: u64 = 1_000_000;

    fn cfg(render: RenderPolicy) -> SchedConfig {
        SchedConfig { render, max_catchup_steps: 120, max_lag_ms: 1000 }
    }
    fn at(now_ns: u64) -> Inputs {
        Inputs { now_ns, ..Default::default() }
    }

    #[test]
    fn step_offsets_are_exact_and_drift_free() {
        assert_eq!(step_offset_ns(0), 0);
        assert_eq!(step_offset_ns(60), NS_PER_S);
        assert_eq!(step_offset_ns(3), 50 * MS);
        assert_eq!(step_offset_ns(216_000), 3600 * NS_PER_S); // one hour, exact
    }

    #[test]
    fn on_time_steps_sleep_to_the_step_boundary_and_render() {
        let mut s = Scheduler::new(cfg(RenderPolicy::IntervalMs(0)));
        let d = s.step(at(1_000 * MS));
        assert!(d.render);
        assert_eq!(d.sleep_until_ns, Some(1_000 * MS + step_offset_ns(1)));
        // The caller slept; the next step arrives exactly on time.
        let d = s.step(at(1_000 * MS + step_offset_ns(1)));
        assert_eq!((d.render, d.lag_steps), (true, 0));
        assert_eq!(d.sleep_until_ns, Some(1_000 * MS + step_offset_ns(2)));
    }

    #[test]
    fn late_steps_skip_renders_until_caught_up() {
        let mut s = Scheduler::new(cfg(RenderPolicy::IntervalMs(0)));
        s.step(at(0));
        // A 500 ms render: the next step is 29 steps late.
        let mut now = 500 * MS;
        let d = s.step(at(now));
        assert!(!d.render && d.sleep_until_ns.is_none() && d.lag_steps >= 28);
        // Catch-up steps cost 2 ms each: no render, no sleep until on time again.
        let mut skipped = 1;
        loop {
            now += 2 * MS;
            let d = s.step(at(now));
            if d.render {
                break;
            }
            assert!(d.sleep_until_ns.is_none());
            skipped += 1;
            assert!(skipped < 100);
        }
        assert!((29..=33).contains(&skipped), "{skipped}");
        assert_eq!(s.stats.dropped_ns, 0);
    }

    #[test]
    fn catch_up_is_bounded_by_forced_renders() {
        let mut s = Scheduler::new(SchedConfig {
            render: RenderPolicy::IntervalMs(0),
            max_catchup_steps: 5,
            max_lag_ms: 10_000,
        });
        s.step(at(0));
        let mut now = 0;
        let mut renders = Vec::new();
        for i in 0..30 {
            now += 40 * MS; // each step slower than 60 Hz: permanently behind
            if s.step(at(now)).render {
                renders.push(i);
            }
        }
        // Step 0 is still within one step of its slot and renders; afterwards every run
        // of 5 skipped renders ends in a forced render.
        assert_eq!(renders, vec![0, 6, 12, 18, 24]);
        assert_eq!(s.stats.max_skip_run, 5);
        assert_eq!(s.stats.forced_renders, 4);
    }

    #[test]
    fn spiral_of_death_guard_drops_excess_lag() {
        let mut s = Scheduler::new(SchedConfig {
            render: RenderPolicy::IntervalMs(0),
            max_catchup_steps: 120,
            max_lag_ms: 250,
        });
        s.step(at(0));
        // A 5 s hitch (save): only 250 ms stay as debt.
        let d = s.step(at(5_000 * MS));
        assert_eq!(d.lag_steps, 250 * MS / STEP_NS);
        let dropped = 5_000 * MS - step_offset_ns(2) - 250 * MS;
        assert!(s.stats.dropped_ns.abs_diff(dropped) <= 1, "{} vs {dropped}", s.stats.dropped_ns);
        // About 15 fast catch-up steps later the clock is on time and renders resume.
        let mut now = 5_000 * MS;
        let mut n = 0;
        while !s.step(at(now)).render {
            now += MS;
            n += 1;
            assert!(n < 40);
        }
        assert!((14..=17).contains(&n), "{n}");
    }

    #[test]
    fn render_interval_limits_render_rate() {
        let mut s = Scheduler::new(cfg(RenderPolicy::IntervalMs(500)));
        let mut renders = 0;
        for k in 0..600u64 {
            // 10 s of on-time steps
            if s.step(at(step_offset_ns(k))).render {
                renders += 1;
            }
        }
        assert_eq!(renders, 20);
    }

    #[test]
    fn never_policy_never_renders_and_still_paces() {
        let mut s = Scheduler::new(cfg(RenderPolicy::Never));
        for k in 0..120u64 {
            let d = s.step(at(step_offset_ns(k)));
            assert!(!d.render);
            assert_eq!(d.sleep_until_ns, Some(step_offset_ns(k + 1)));
        }
        assert_eq!(s.stats.renders, 0);
    }

    #[test]
    fn loading_renders_and_rebaselines_without_debt() {
        let mut s = Scheduler::new(cfg(RenderPolicy::IntervalMs(0)));
        s.step(at(0));
        s.step(at(step_offset_ns(1)));
        let d = s.step(Inputs { now_ns: 10 * MS, loading: true, temp_paused: false });
        assert_eq!(d, Decision { render: true, sleep_until_ns: None, lag_steps: 0 });
        // 30 s later the load ends: no catch-up debt, no dropped time.
        let d = s.step(at(30_000 * MS));
        assert!(d.render && d.lag_steps == 0);
        assert_eq!((s.stats.dropped_ns, s.stats.rebaselines, s.stats.loading_steps), (0, 1, 1));
    }

    #[test]
    fn temp_pause_renders_even_while_behind() {
        let mut s = Scheduler::new(cfg(RenderPolicy::IntervalMs(0)));
        s.step(at(0));
        let d = s.step(Inputs { now_ns: 300 * MS, loading: false, temp_paused: true });
        assert!(d.render && d.lag_steps > 0);
        let d = s.step(at(301 * MS));
        assert!(!d.render);
    }

    #[test]
    fn step_check_classifies_time_deltas() {
        let p = TimeSample { game: 100.0, real: 200.0, speed: 1.0 };
        let next = |dg: f64, dr: f64, speed: f64| TimeSample { game: 100.0 + dg, real: 200.0 + dr, speed };
        assert_eq!(check_step(&p, &next(FIXED_DT, FIXED_DT, 1.0)), StepCheck::Exact);
        assert_eq!(check_step(&p, &next(FIXED_DT, FIXED_DT, 6.0)), StepCheck::Exact); // speed changes after FrameTick
        let paused = TimeSample { speed: 0.0, ..p };
        assert_eq!(check_step(&paused, &next(0.0, FIXED_DT, 0.0)), StepCheck::Exact);
        let seta = TimeSample { speed: 6.0, ..p };
        assert_eq!(check_step(&seta, &next(6.0 * FIXED_DT, FIXED_DT, 6.0)), StepCheck::Exact);
        assert_eq!(check_step(&p, &next(-50.0, FIXED_DT, 1.0)), StepCheck::GameIrregular);
        assert_eq!(check_step(&p, &next(0.0, 0.0, 1.0)), StepCheck::NoAdvance);
        assert_eq!(check_step(&p, &next(0.02, 0.02, 1.0)), StepCheck::RealTimeStep);
        assert_eq!(check_step(&p, &next(FIXED_DT, 2.0 * FIXED_DT, 1.0)), StepCheck::RealTimeStep);
        assert_eq!(check_step(&p, &next(f64::NAN, FIXED_DT, 1.0)), StepCheck::Invalid);
        assert_eq!(check_step(&p, &next(FIXED_DT, FIXED_DT, 99.0)), StepCheck::Invalid);
    }

    #[test]
    fn exact_steps_accumulate_to_n_sixtieths() {
        // FrameTick's accumulation (gameTime += dt*speed) at speed 1 over one hour.
        let (mut game, mut real) = (1234.5_f64, 98765.0_f64);
        let mut prev = TimeSample { game, real, speed: 1.0 };
        for _ in 0..216_000 {
            game += FIXED_DT * 1.0;
            real += FIXED_DT;
            let cur = TimeSample { game, real, speed: 1.0 };
            assert_eq!(check_step(&prev, &cur), StepCheck::Exact);
            prev = cur;
        }
        assert!((game - 1234.5 - 3600.0).abs() < 1e-6);
    }

    #[test]
    fn sched_line_reports_rates_and_ratio() {
        let a = SchedSnapshot { game_time: 10.0, speed: 1.0, ..Default::default() };
        let mut b = a;
        b.stats.steps = 60;
        b.stats.renders = 2;
        b.stats.dropped_ns = 5 * MS;
        b.game_time = 11.0;
        let line = sched_line("armed", &a, &b, 1000);
        assert!(
            line.starts_with(
                "X4MP_AGENT SCHED state=armed sps=60.00 rps=2.00 game_wall=1.0000 game_wall_per_speed=1.0000"
            ),
            "{line}"
        );
        assert!(line.contains(" ddropped_ms=5.0 "), "{line}");
    }
}
