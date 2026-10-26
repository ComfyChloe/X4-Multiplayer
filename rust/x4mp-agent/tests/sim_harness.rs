//! Synthetic harness: a fake main thread (X4 main loop + FrameTick) and a fake MoveWorker,
//! joined by semSimStart/semSimDone exactly like the engine, drive the real
//! `scheduler::slot13_step`. It proves, with real threads and wall time:
//! - every simulation frame is one fixed step (game time == steps / 60, exactly);
//! - the render flag is written only while the main thread is parked outside FrameTick,
//!   so the two reads inside one FrameTick always agree;
//! - no render ever overlaps more than one simulation frame (double-buffer rule);
//! - game time tracks wall time with infrequent, expensive renders, and the
//!   spiral-of-death guard drops time instead of diverging when rendering cannot keep up.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use x4mp_agent::scheduler::{
    check_step, slot13_step, Engine, RenderPolicy, SchedConfig, Scheduler, Slot13, StepCheck, TimeSample, FIXED_DT,
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

#[derive(Default)]
struct Shared {
    stop: AtomicBool,
    in_flight: AtomicBool,
    render_flag: AtomicBool,
    rendering: AtomicBool,
    game_bits: AtomicU64,
    real_bits: AtomicU64,
    frame_ticks: AtomicU64,
    renders: AtomicU64,
    sim_frames_during_render: AtomicU64,
    max_sim_frames_in_one_render: AtomicU64,
    flip_violations: AtomicU64,
    unparked_writes: AtomicU64,
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
    fn render_flag(&self) -> bool {
        self.s.render_flag.load(Ordering::SeqCst)
    }
    fn set_render_flag(&self, render: bool) {
        if !self.s.in_flight.load(Ordering::SeqCst) {
            self.s.unparked_writes.fetch_add(1, Ordering::SeqCst);
        }
        self.s.render_flag.store(render, Ordering::SeqCst);
    }
}

struct Scenario {
    policy: SchedConfig,
    render_ms: u64,
    sim_ms: u64,
    run_ms: u64,
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
    shared: Arc<Shared>,
}

fn f(bits: &AtomicU64) -> f64 {
    f64::from_bits(bits.load(Ordering::SeqCst))
}

fn run(sc: Scenario) -> Outcome {
    let s = Arc::new(Shared::default());
    s.render_flag.store(true, Ordering::SeqCst); // FrameState constructor value
    s.in_flight.store(true, Ordering::SeqCst); // .data initial value of 0x3d8d97b: the first
                                               // wait consumes semSimDone's initial count
    let start = Arc::new(Sem::new(0));
    let done = Arc::new(Sem::new(1)); // initial count 1, like g_semSimDone
    let t0 = Instant::now();

    // MoveWorker: wait semSimStart, simulate, slot 13 (scheduler), release semSimDone.
    let worker = {
        let (s, start, done) = (s.clone(), start.clone(), done.clone());
        let policy = sc.policy;
        let sim_ms = sc.sim_ms;
        thread::spawn(move || {
            let engine = FakeEngine { s: s.clone(), t0 };
            let mut sched = Scheduler::new(policy);
            let mut last = true;
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
                thread::sleep(Duration::from_millis(sim_ms)); // slots 0..12
                let cur = TimeSample { game: f(&s.game_bits), real: f(&s.real_bits), speed: 1.0 };
                if let Some(p) = prev {
                    checked += 1;
                    match check_step(&p, &cur) {
                        StepCheck::Exact => exact += 1,
                        other => panic!("step check {other:?}"),
                    }
                }
                prev = Some(cur);
                match slot13_step(&mut sched, &engine, &mut last, false, false, 5_000_000_000) {
                    Slot13::Stepped { .. } => {}
                    other => panic!("slot13 {other:?}"),
                }
                done.post();
            }
            (sched.stats, exact, checked)
        })
    };

    // Main thread: X4::MainMessageLoop + FrameTick (fixed dt).
    let deadline = t0 + Duration::from_millis(sc.run_ms);
    let mut speed_game = 0.0f64;
    let mut real = 0.0f64;
    while Instant::now() < deadline {
        if s.in_flight.load(Ordering::SeqCst) {
            if !done.wait_timeout(Duration::from_millis(16)) {
                continue; // pump events
            }
            s.in_flight.store(false, Ordering::SeqCst);
        }
        // FrameTick: advance time by exactly 1/60 x speed (speed 1).
        speed_game += FIXED_DT * 1.0;
        real += FIXED_DT;
        s.game_bits.store(speed_game.to_bits(), Ordering::SeqCst);
        s.real_bits.store(real.to_bits(), Ordering::SeqCst);
        s.frame_ticks.fetch_add(1, Ordering::SeqCst);
        let first_read = s.render_flag.load(Ordering::SeqCst); // 0x2025162
        start.post(); // semSimStart
        let second_read = s.render_flag.load(Ordering::SeqCst); // 0x2025208
        if first_read != second_read {
            s.flip_violations.fetch_add(1, Ordering::SeqCst);
        }
        if second_read {
            s.sim_frames_during_render.store(0, Ordering::SeqCst);
            s.rendering.store(true, Ordering::SeqCst);
            thread::sleep(Duration::from_millis(sc.render_ms)); // render + present
            s.rendering.store(false, Ordering::SeqCst);
            s.renders.fetch_add(1, Ordering::SeqCst);
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
        shared: s,
    }
}

fn common_invariants(o: &Outcome) {
    assert_eq!(o.shared.flip_violations.load(Ordering::SeqCst), 0, "render flag changed inside one FrameTick");
    assert_eq!(o.shared.unparked_writes.load(Ordering::SeqCst), 0, "render flag written while main was in FrameTick");
    assert!(o.shared.max_sim_frames_in_one_render.load(Ordering::SeqCst) <= 1, "a render overlapped two sim frames");
    assert_eq!(o.exact, o.checked, "a simulation frame was not one fixed step");
    let ticks = o.shared.frame_ticks.load(Ordering::SeqCst) as f64;
    assert!((o.game - ticks * FIXED_DT).abs() < 1e-9, "game time {} != {} x 1/60", o.game, ticks);
}

#[test]
fn server_renders_rarely_and_game_time_tracks_wall_time() {
    // CPU renderer: each render costs 150 ms; render at most every 500 ms; sim 2 ms/step.
    let o = run(Scenario {
        policy: SchedConfig { render: RenderPolicy::IntervalMs(500), max_catchup_steps: 600, max_lag_ms: 1000 },
        render_ms: 150,
        sim_ms: 2,
        run_ms: 3000,
    });
    common_invariants(&o);
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.93 && ratio < 1.03, "game/wall {ratio}");
    assert!((4..=8).contains(&o.renders), "renders {}", o.renders);
    assert_eq!(o.dropped_ms, 0.0);
}

#[test]
fn server_without_rendering_steps_at_sixty_hertz() {
    let o = run(Scenario {
        policy: SchedConfig { render: RenderPolicy::Never, max_catchup_steps: 120, max_lag_ms: 1000 },
        render_ms: 5, // only the pre-baseline first frame renders; keep it out of the ratio
        sim_ms: 1,
        run_ms: 2000,
    });
    common_invariants(&o);
    // Only the first FrameTick, decided before the scheduler's first slot-13 call, renders.
    assert!(o.renders <= 1, "renders {}", o.renders);
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.95 && ratio < 1.03, "game/wall {ratio}");
    let sps = o.steps as f64 / o.wall_s;
    assert!((55.0..=61.5).contains(&sps), "steps/s {sps}");
}

#[test]
fn client_at_thirty_fps_render_keeps_sixty_hertz_simulation() {
    // A 25 ms render (40 fps max) with render-whenever-on-time: renders interleave with
    // catch-up steps; simulation stays at 60 Hz.
    let o = run(Scenario {
        policy: SchedConfig { render: RenderPolicy::IntervalMs(0), max_catchup_steps: 120, max_lag_ms: 1000 },
        render_ms: 25,
        sim_ms: 1,
        run_ms: 2000,
    });
    common_invariants(&o);
    let ratio = o.game / o.wall_s;
    assert!(ratio > 0.93 && ratio < 1.03, "game/wall {ratio}");
    let rps = o.renders as f64 / o.wall_s;
    assert!((15.0..=40.0).contains(&rps), "renders/s {rps}");
}

#[test]
fn overload_is_bounded_and_drops_time_instead_of_spiralling() {
    // Renders cost 300 ms and are forced every 10 skipped steps; steps cost 12 ms.
    // Rendering cannot keep up: the guard drops lateness above 200 ms.
    let o = run(Scenario {
        policy: SchedConfig { render: RenderPolicy::IntervalMs(0), max_catchup_steps: 10, max_lag_ms: 200 },
        render_ms: 300,
        sim_ms: 12,
        run_ms: 3000,
    });
    common_invariants(&o);
    assert!(o.dropped_ms > 500.0, "dropped {}", o.dropped_ms);
    assert!(o.max_skip_run <= 10, "skip run {}", o.max_skip_run);
    let ratio = o.game / o.wall_s;
    assert!(ratio < 0.9, "game/wall {ratio}");
}
