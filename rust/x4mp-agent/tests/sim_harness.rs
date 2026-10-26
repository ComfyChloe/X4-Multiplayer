//! Synthetic harness: a fake main thread (X4 main loop + FrameTick) and a fake MoveWorker,
//! joined by semSimStart/semSimDone exactly like the engine, drive the real
//! `scheduler::slot13_step`. It proves, with real threads and wall time:
//! - every simulation frame is one fixed step (game time == FrameTicks / hz, exactly);
//! - the render decision is written only while the main thread is parked outside
//!   FrameTick, so it never changes inside one FrameTick;
//! - no render ever overlaps more than one simulation frame (double-buffer rule);
//! - the engine's render-list double buffer (filled by "slot 10", flipped by FrameTick's
//!   first flag read, drained by the render) never overflows, because the flip runs on
//!   every frame. The legacy variant, which cleared the flip flag too, reproduces the
//!   native freeze of 25 Oct 2026 (overflow, then ever slower renders);
//! - with render-prep skip (slot 10 only on frames before a render, one-frame lookahead)
//!   no FrameTick ever renders an unprepared half, and with the RNG save/restore around
//!   slot 10 the simulation's RNG stream is identical whether render prep runs or not;
//!   variants without the lookahead or without the isolation are shown to fail;
//! - game time tracks wall time with infrequent, expensive renders; renders happen at
//!   least every `max_render_gap_ms`; the spiral-of-death guard drops time instead of
//!   diverging when rendering cannot keep up.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use x4mp_agent::scheduler::{
    check_step, fixed_dt, slot13_step, Engine, RenderPolicy, SchedConfig, Scheduler, Slot13, StepCheck, TimeSample,
};

struct Sem {
    count: Mutex<u32>,
    cv: Condvar,
}
impl Sem {
    fn new(initial: u32) -> Self {
        Sem { count: Mutex::new(initial), cv: Condvar::new() }
    }
    fn post(&self) {
        let mut c = self.count.lock().unwrap();
        *c = (*c + 1).min(1); // max 1, like the engine's semaphores
        self.cv.notify_one();
    }
    fn wait(&self) {
        let mut c = self.count.lock().unwrap();
        while *c == 0 {
            c = self.cv.wait(c).unwrap();
        }
        *c -= 1;
    }
    fn wait_timeout(&self, d: Duration) -> bool {
        let mut c = self.count.lock().unwrap();
        let deadline = Instant::now() + d;
        while *c == 0 {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            c = self.cv.wait_timeout(c, deadline - now).unwrap().0;
        }
        *c -= 1;
        true
    }
}

/// The engine's render-list double buffer (0x454d9a0, index 0x72a0fa0): the simulation
/// appends to the producer half (bounded, "PushBoneTransforms() out of space"), FrameTick
/// flips (0x218e2c0) when its first render-flag read is set, the render drains the
/// consumed half.
#[derive(Default)]
struct RenderLists {
    halves: [u64; 2],
    /// Slot 10 prepared this half since it was last consumed (finalize clears it).
    prepared: [bool; 2],
    producer: usize,
    overflows: u64,
    max_fill: u64,
}
const LIST_CAPACITY: u64 = 400;
const ITEMS_PER_FRAME: u64 = 20;

#[derive(Default)]
struct Shared {
    stop: AtomicBool,
    in_flight: AtomicBool,
    /// FrameState+0x200: engine render flag, gates the flip (read #1).
    flip_flag: AtomicBool,
    /// FrameState+0x201: the scheduler's decision, read #2 after the decision patch.
    decision: AtomicBool,
    /// Legacy (pre-fix) design: the scheduler wrote +0x200, so read #1 saw it too.
    legacy: AtomicBool,
    rendering: AtomicBool,
    game_bits: AtomicU64,
    real_bits: AtomicU64,
    frame_ticks: AtomicU64,
    renders: AtomicU64,
    max_render_gap_ns: AtomicU64,
    sim_frames_during_render: AtomicU64,
    max_sim_frames_in_one_render: AtomicU64,
    torn_decisions: AtomicU64,
    unparked_writes: AtomicU64,
    lists: Mutex<RenderLists>,
    /// Render-prep skip: decision for the next slot-10 call, and its results.
    prep_next: AtomicBool,
    prep_kept: AtomicU64,
    prep_skipped: AtomicU64,
    unprepared_renders: AtomicU64,
    rng_mismatches: AtomicU64,
}

struct FakeEngine {
    s: Arc<Shared>,
    t0: Instant,
}
impl Engine for FakeEngine {
    fn main_parked(&self) -> bool {
        self.s.in_flight.load(Ordering::SeqCst)
    }
    fn now_ns(&self) -> u64 {
        self.t0.elapsed().as_nanos() as u64
    }
    fn pause(&self) {
        thread::sleep(Duration::from_micros(200));
    }
    fn sleep_until(&self, ns: u64) {
        let now = self.now_ns();
        if ns > now {
            thread::sleep(Duration::from_nanos(ns - now));
        }
    }
    fn render_decision(&self) -> bool {
        self.s.decision.load(Ordering::SeqCst)
    }
    fn set_render_decision(&self, render: bool) {
        if !self.s.in_flight.load(Ordering::SeqCst) {
            self.s.unparked_writes.fetch_add(1, Ordering::SeqCst);
        }
        self.s.decision.store(render, Ordering::SeqCst);
        if self.s.legacy.load(Ordering::SeqCst) {
            self.s.flip_flag.store(render, Ordering::SeqCst);
        }
    }
}

struct Scenario {
    policy: SchedConfig,
    render_ms: u64,
    /// Extra render cost per drained render-list item (µs).
    render_us_per_item: u64,
    sim_ms: u64,
    run_ms: u64,
    legacy: bool,
    /// X4MP_SCHED_SKIP_RENDERPREP: slot 10 runs only when `prep_next` says so.
    skip_prep: bool,
    /// Pipeline decisions one frame ahead (the real design). `false` = naive variant.
    lookahead: bool,
    /// Save/restore the RNG around slot 10 (the real design).
    isolate_rng: bool,
}

impl Scenario {
    fn new(policy: SchedConfig, render_ms: u64, sim_ms: u64, run_ms: u64) -> Self {
        Scenario {
            policy,
            render_ms,
            render_us_per_item: 0,
            sim_ms,
            run_ms,
            legacy: false,
            skip_prep: false,
            lookahead: false,
            isolate_rng: false,
        }
    }
    fn with_prep_skip(mut self) -> Self {
        (self.skip_prep, self.lookahead, self.isolate_rng) = (true, true, true);
        self
    }
}

/// The engine's per-thread RNG step (Random::U64State, 0x1a490a0).
fn rng_step(state: u64) -> u64 {
    state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407).rotate_right(30)
}

struct Outcome {
    steps: u64,
    renders: u64,
    exact: u64,
    checked: u64,
    game: f64,
    wall_s: f64,
    dropped_ms: f64,
    max_skip_run: u64,
    hz: u32,
    shared: Arc<Shared>,
}

fn f(bits: &AtomicU64) -> f64 {
    f64::from_bits(bits.load(Ordering::SeqCst))
}

fn run(sc: Scenario) -> Outcome {
    let s = Arc::new(Shared::default());
    s.flip_flag.store(true, Ordering::SeqCst); // FrameState constructor value
    s.decision.store(true, Ordering::SeqCst); // set to "render" before the patch
    s.legacy.store(sc.legacy, Ordering::SeqCst);
    s.in_flight.store(true, Ordering::SeqCst); // .data initial value of 0x3d8d97b: the first
                                               // wait consumes semSimDone's initial count
    s.prep_next.store(true, Ordering::SeqCst);
    s.lists.lock().unwrap().prepared = [true, true]; // vanilla frames before arming
    let start = Arc::new(Sem::new(0));
    let done = Arc::new(Sem::new(1)); // initial count 1, like g_semSimDone
    let t0 = Instant::now();
    let hz = sc.policy.hz;
    let dt = fixed_dt(hz);

    // MoveWorker: wait semSimStart, simulate (slot 10 fills the render lists), slot 13
    // (scheduler), release semSimDone.
    let worker = {
        let (s, start, done) = (s.clone(), start.clone(), done.clone());
        let policy = sc.policy;
        let sim_ms = sc.sim_ms;
        let (skip_prep, lookahead, isolate_rng) = (sc.skip_prep, sc.lookahead, sc.isolate_rng);
        thread::spawn(move || {
            let engine = FakeEngine { s: s.clone(), t0 };
            let mut sched = Scheduler::new(policy);
            let mut last = true;
            let mut pending = true;
            // TLS RNG of the MoveWorker, and the stream the simulation alone would see.
            let (mut rng, mut sim_only) = (0x1234_5678_9abc_def0u64, 0x1234_5678_9abc_def0u64);
            let (mut prev, mut exact, mut checked) = (None::<TimeSample>, 0u64, 0u64);
            loop {
                start.wait();
                if s.stop.load(Ordering::SeqCst) {
                    break;
                }
                if s.rendering.load(Ordering::SeqCst) {
                    let n = s.sim_frames_during_render.fetch_add(1, Ordering::SeqCst) + 1;
                    s.max_sim_frames_in_one_render.fetch_max(n, Ordering::SeqCst);
                }
                thread::sleep(Duration::from_millis(sim_ms)); // slots 0..9
                rng = rng_step(rng); // a simulation draw (e.g. slot 3)
                sim_only = rng_step(sim_only);
                // slot 10: GameRender::Move (wrapped when skip_prep)
                let saved = rng;
                let prep = !skip_prep || s.prep_next.load(Ordering::SeqCst);
                if prep {
                    // camera render draws the TLS RNG (zones, effects)
                    for _ in 0..3 {
                        rng = rng_step(rng);
                    }
                    let mut l = s.lists.lock().unwrap();
                    let p = l.producer;
                    if l.halves[p] + ITEMS_PER_FRAME > LIST_CAPACITY {
                        l.overflows += 1; // "out of space"
                    } else {
                        l.halves[p] += ITEMS_PER_FRAME;
                    }
                    l.prepared[p] = true;
                    l.max_fill = l.max_fill.max(l.halves[p]);
                    s.prep_kept.fetch_add(1, Ordering::SeqCst);
                } else {
                    s.prep_skipped.fetch_add(1, Ordering::SeqCst);
                }
                if isolate_rng {
                    rng = saved;
                }
                // slot 11: JobEngine draws; must see the simulation-only stream
                rng = rng_step(rng);
                sim_only = rng_step(sim_only);
                if rng != sim_only {
                    s.rng_mismatches.fetch_add(1, Ordering::SeqCst);
                    rng = sim_only; // count each divergence once
                }
                let cur = TimeSample { game: f(&s.game_bits), real: f(&s.real_bits), speed: 1.0 };
                if let Some(p) = prev {
                    checked += 1;
                    match check_step(&p, &cur, dt) {
                        StepCheck::Exact => exact += 1,
                        other => panic!("step check {other:?}"),
                    }
                }
                prev = Some(cur);
                let ahead = if lookahead { Some(&mut pending) } else { None };
                match slot13_step(&mut sched, &engine, &mut last, false, false, 5_000_000_000, ahead) {
                    Slot13::Stepped { prep_next, decision, .. } => {
                        // naive variant: prepare according to the decision just made,
                        // which applies to the FrameTick after the next prep
                        s.prep_next.store(if lookahead { prep_next } else { decision.render }, Ordering::SeqCst);
                    }
                    other => panic!("slot13 {other:?}"),
                }
                done.post();
            }
            (sched.stats, exact, checked)
        })
    };

    // Main thread: X4::MainMessageLoop + FrameTick (fixed dt, decision patch).
    let deadline = t0 + Duration::from_millis(sc.run_ms);
    let (mut game, mut real) = (0.0f64, 0.0f64);
    let mut last_render = Instant::now();
    while Instant::now() < deadline {
        if s.in_flight.load(Ordering::SeqCst) {
            if !done.wait_timeout(Duration::from_millis(16)) {
                continue; // pump events
            }
            s.in_flight.store(false, Ordering::SeqCst);
        }
        // FrameTick: advance time by exactly 1/hz x speed (speed 1).
        game += dt;
        real += dt;
        s.game_bits.store(game.to_bits(), Ordering::SeqCst);
        s.real_bits.store(real.to_bits(), Ordering::SeqCst);
        s.frame_ticks.fetch_add(1, Ordering::SeqCst);
        let decision_before = s.decision.load(Ordering::SeqCst);
        if s.flip_flag.load(Ordering::SeqCst) {
            // read #1 (0x2025162) -> 0x218e2c0: free the consumed half, flip
            let mut l = s.lists.lock().unwrap();
            let consumed = 1 - l.producer;
            l.halves[consumed] = 0;
            l.prepared[consumed] = false; // finalize empties the half
            l.producer = consumed;
        }
        start.post(); // semSimStart
        let render = s.decision.load(Ordering::SeqCst); // read #2 (0x2025208, patched)
        if render != decision_before {
            s.torn_decisions.fetch_add(1, Ordering::SeqCst);
        }
        if render {
            let items = {
                let l = s.lists.lock().unwrap();
                if !l.prepared[1 - l.producer] {
                    s.unprepared_renders.fetch_add(1, Ordering::SeqCst);
                }
                l.halves[1 - l.producer]
            };
            s.sim_frames_during_render.store(0, Ordering::SeqCst);
            s.rendering.store(true, Ordering::SeqCst);
            thread::sleep(Duration::from_millis(sc.render_ms) + Duration::from_micros(items * sc.render_us_per_item));
            s.rendering.store(false, Ordering::SeqCst);
            s.renders.fetch_add(1, Ordering::SeqCst);
            let gap = last_render.elapsed().as_nanos() as u64;
            s.max_render_gap_ns.fetch_max(gap, Ordering::SeqCst);
            last_render = Instant::now();
        }
        s.in_flight.store(true, Ordering::SeqCst);
    }
    let wall_s = t0.elapsed().as_secs_f64();
    s.stop.store(true, Ordering::SeqCst);
    done.wait_timeout(Duration::from_secs(5));
    start.post();
    let (stats, exact, checked) = worker.join().unwrap();
    Outcome {
        steps: stats.steps,
        renders: s.renders.load(Ordering::SeqCst),
        exact,
        checked,
        game: f(&s.game_bits),
        wall_s,
        dropped_ms: stats.dropped_ns as f64 / 1e6,
        max_skip_run: stats.max_skip_run,
        hz,
        shared: s,
    }
}

fn common_invariants(o: &Outcome) {
    assert_eq!(o.shared.torn_decisions.load(Ordering::SeqCst), 0, "decision changed inside one FrameTick");
    assert_eq!(o.shared.unparked_writes.load(Ordering::SeqCst), 0, "decision written while main was in FrameTick");
    assert!(o.shared.max_sim_frames_in_one_render.load(Ordering::SeqCst) <= 1, "a render overlapped two sim frames");
    assert_eq!(o.exact, o.checked, "a simulation frame was not one fixed step");
    let ticks = o.shared.frame_ticks.load(Ordering::SeqCst) as f64;
    assert!((o.game - ticks * fixed_dt(o.hz)).abs() < 1e-9, "game time {} != {} x 1/{}", o.game, ticks, o.hz);
}

fn lists_bounded(o: &Outcome) {
    let l = o.shared.lists.lock().unwrap();
    assert_eq!(l.overflows, 0, "render-list producer half overflowed");
    // One flip per FrameTick: the producer half never holds more than one frame.
    assert!(l.max_fill <= ITEMS_PER_FRAME, "producer half held {} items", l.max_fill);
}

fn policy(
    hz: u32,
    render: RenderPolicy,
    max_catchup_steps: u32,
    max_render_gap_ms: u64,
    max_lag_ms: u64,
) -> SchedConfig {
    SchedConfig { hz, render, max_catchup_steps, max_render_gap_ms, max_lag_ms }
}

#[test]
fn server_renders_rarely_and_game_time_tracks_wall_time() {
    // CPU renderer: each render costs 150 ms; render at most every 500 ms; sim 2 ms/step.
    let o = run(Scenario::new(policy(60, RenderPolicy::IntervalMs(500), 600, 5000, 1000), 150, 2, 3000));
    common_invariants(&o);
    lists_bounded(&o);
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.93 && ratio < 1.03, "game/wall {ratio}");
    assert!((4..=8).contains(&o.renders), "renders {}", o.renders);
    assert_eq!(o.dropped_ms, 0.0);
}

#[test]
fn server_without_rendering_steps_at_sixty_hertz_and_keeps_lists_bounded() {
    let o = run(Scenario::new(policy(60, RenderPolicy::Never, 3600, 0, 1000), 5, 1, 2000));
    common_invariants(&o);
    lists_bounded(&o);
    // Only the first FrameTick, decided before the scheduler's first slot-13 call, renders.
    assert!(o.renders <= 1, "renders {}", o.renders);
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.95 && ratio < 1.03, "game/wall {ratio}");
    let sps = o.steps as f64 / o.wall_s;
    assert!((55.0..=61.5).contains(&sps), "steps/s {sps}");
}

#[test]
fn legacy_flag_design_starves_the_flip_and_overflows() {
    // Reproduces the native freeze: clearing the engine flag also skipped the render-list
    // flip, so the producer half filled up (overflow) and the next render drained a huge
    // list. Same scenario as above with the pre-fix flag handling.
    let mut sc = Scenario::new(policy(60, RenderPolicy::Never, 3600, 0, 1000), 5, 1, 1500);
    sc.legacy = true;
    let o = run(sc);
    let l = o.shared.lists.lock().unwrap();
    assert!(l.overflows > 0, "legacy design did not overflow");
    assert_eq!(l.max_fill, LIST_CAPACITY);
}

#[test]
fn slow_simulation_at_sixty_hertz_still_renders_within_the_gap_bound() {
    // The native setup: 33 ms per step cannot reach 60 Hz, so the step clock is always
    // behind. Interval 1000 ms with 600 catch-up steps used to render only every ~20 s;
    // the gap bound (1000 ms) forces renders anyway, and no list overflows.
    let mut sc = Scenario::new(policy(60, RenderPolicy::IntervalMs(1000), 600, 1000, 1000), 40, 33, 4000);
    sc.render_us_per_item = 50;
    let o = run(sc);
    common_invariants(&o);
    lists_bounded(&o);
    let gap_ms = o.shared.max_render_gap_ns.load(Ordering::SeqCst) as f64 / 1e6;
    assert!(gap_ms < 1000.0 + 33.0 * 2.0 + 40.0 + 20.0, "render gap {gap_ms} ms");
    assert!(o.renders >= 3, "renders {}", o.renders);
}

#[test]
fn thirty_hertz_keeps_up_where_sixty_cannot() {
    // Same 25 ms steps: 30 Hz tracks wall time (plus a render every 1000 ms).
    let o = run(Scenario::new(policy(30, RenderPolicy::IntervalMs(1000), 600, 2000, 1000), 40, 25, 3000));
    common_invariants(&o);
    lists_bounded(&o);
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.9 && ratio < 1.05, "game/wall {ratio}");
    let sps = o.steps as f64 / o.wall_s;
    assert!((26.0..=31.0).contains(&sps), "steps/s {sps}");
}

#[test]
fn client_at_thirty_fps_render_keeps_sixty_hertz_simulation() {
    let o = run(Scenario::new(policy(60, RenderPolicy::IntervalMs(0), 120, 5000, 1000), 25, 1, 2000));
    common_invariants(&o);
    lists_bounded(&o);
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.93 && ratio < 1.03, "game/wall {ratio}");
    let rps = o.renders as f64 / o.wall_s;
    assert!((15.0..=40.0).contains(&rps), "renders/s {rps}");
}

#[test]
fn overload_is_bounded_and_drops_time_instead_of_spiralling() {
    let o = run(Scenario::new(policy(60, RenderPolicy::IntervalMs(0), 10, 5000, 200), 300, 12, 3000));
    common_invariants(&o);
    lists_bounded(&o);
    assert!(o.dropped_ms > 500.0, "dropped {}", o.dropped_ms);
    assert!(o.max_skip_run <= 10, "skip run {}", o.max_skip_run);
    let ratio = o.game / o.wall_s;
    assert!(ratio < 0.9, "game/wall {ratio}");
}

// ----------------------------------------------------------------------------------
// X4MP_SCHED_SKIP_RENDERPREP

fn prep_count(o: &Outcome) -> (u64, u64) {
    (o.shared.prep_kept.load(Ordering::SeqCst), o.shared.prep_skipped.load(Ordering::SeqCst))
}

#[test]
fn renderprep_skip_prepares_exactly_before_renders() {
    // fixed20 server: render every 1000 ms, steps cost 10 ms.
    let o =
        run(Scenario::new(policy(20, RenderPolicy::IntervalMs(1000), 600, 2000, 1000), 40, 10, 4000).with_prep_skip());
    common_invariants(&o);
    lists_bounded(&o);
    assert_eq!(o.shared.unprepared_renders.load(Ordering::SeqCst), 0, "rendered an unprepared half");
    let (kept, skipped) = prep_count(&o);
    assert!(skipped > 5 * kept, "kept {kept} skipped {skipped}");
    // Every prep is followed by a render: kept == renders (+1 for the frame in flight).
    assert!(kept.abs_diff(o.renders) <= 2, "kept {kept} renders {}", o.renders);
    assert_eq!(o.shared.rng_mismatches.load(Ordering::SeqCst), 0, "slot 10 leaked into the RNG stream");
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.93 && ratio < 1.05, "game/wall {ratio}");
}

#[test]
fn renderprep_skip_with_never_and_client_policies_stays_consistent() {
    for (render, gap) in [(RenderPolicy::Never, 0), (RenderPolicy::Never, 500), (RenderPolicy::IntervalMs(0), 5000)] {
        let o = run(Scenario::new(policy(30, render, 120, gap, 1000), 20, 5, 2000).with_prep_skip());
        common_invariants(&o);
        lists_bounded(&o);
        assert_eq!(o.shared.unprepared_renders.load(Ordering::SeqCst), 0, "{render:?} gap {gap}");
        assert_eq!(o.shared.rng_mismatches.load(Ordering::SeqCst), 0, "{render:?} gap {gap}");
        let (kept, _) = prep_count(&o);
        assert!(kept.abs_diff(o.renders) <= 2, "{render:?} gap {gap}: kept {kept} renders {}", o.renders);
    }
}

#[test]
fn renderprep_skip_without_lookahead_renders_unprepared_halves() {
    // Negative control: deciding prep from the decision just made (one frame late) makes
    // FrameTicks render halves whose prep was skipped.
    let mut sc =
        Scenario::new(policy(20, RenderPolicy::IntervalMs(300), 600, 2000, 1000), 20, 10, 2500).with_prep_skip();
    sc.lookahead = false;
    let o = run(sc);
    assert!(o.shared.unprepared_renders.load(Ordering::SeqCst) > 0, "naive variant did not fail");
}

#[test]
fn renderprep_without_rng_isolation_changes_the_simulation_stream() {
    // Negative control: without the save/restore, every prep shifts the RNG stream the
    // simulation sees, so prep-on and prep-off runs diverge.
    let mut sc =
        Scenario::new(policy(20, RenderPolicy::IntervalMs(300), 600, 2000, 1000), 20, 10, 2000).with_prep_skip();
    sc.isolate_rng = false;
    let o = run(sc);
    assert!(o.shared.rng_mismatches.load(Ordering::SeqCst) > 0, "no divergence without isolation");
    // And with prep on every frame (vanilla) but isolation on, the stream is clean.
    let o = run(Scenario {
        isolate_rng: true,
        ..Scenario::new(policy(20, RenderPolicy::IntervalMs(0), 120, 5000, 1000), 5, 5, 1000)
    });
    assert_eq!(o.shared.rng_mismatches.load(Ordering::SeqCst), 0);
}
