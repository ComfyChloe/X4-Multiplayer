//! Multi-pass systems (`System::max_passes`): repeat a system within one frame until it is
//! quiescent, with the barrier after every pass (the engine's AIDirector pass loop).

use std::sync::Mutex;

use x4mp_sim::{
    BarrierCtx, FrameInfo, FrameReport, Pool, SectorCtx, SectorIx, System, Target, Universe, UniverseId, WorldKind,
};

#[derive(Debug, Clone, Copy)]
struct Token {
    hops: u32,
    id: u32,
}

#[derive(Debug, Clone)]
struct Seen(String);

/// Per-world log of what this world saw, in order.
#[derive(Default, Debug)]
struct Log(Vec<String>);

/// Forwards every token to the next sector until its hop count is used up. In pass 0 of frame 0
/// every sector `s` with `s % 3 == 0` starts a token with `s + base` hops; every token also
/// reports to the barrier and to the "watch" system.
struct Relay {
    sectors: u32,
    base: u32,
    max: u32,
    hook_log: Mutex<Vec<String>>,
}

impl System for Relay {
    fn name(&self) -> &'static str {
        "relay"
    }
    fn max_passes(&self) -> u32 {
        self.max
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let next = SectorIx((ctx.sector.0 + 1) % self.sectors);
        let mut tokens: Vec<Token> = ctx.messages::<Token>().map(|(_, t)| *t).collect();
        if ctx.frame.frame == 0 && ctx.pass == 0 && ctx.sector.0 % 3 == 0 {
            tokens.push(Token { hops: ctx.sector.0 + self.base, id: ctx.sector.0 });
        }
        for t in tokens {
            let line = format!("f{} p{} s{} token{} hops{}", ctx.frame.frame, ctx.pass, ctx.sector.0, t.id, t.hops);
            ctx.world.resource_mut::<Log>().0.push(line.clone());
            ctx.out.send(Target::Barrier, Seen(line.clone()));
            ctx.out.send_to("watch", Target::Sector(ctx.sector), Seen(line));
            if t.hops > 0 {
                ctx.out.send(Target::Sector(next), Token { hops: t.hops - 1, id: t.id });
            }
        }
    }
    fn barrier(&self, ctx: &mut BarrierCtx<'_>) {
        let mut log = self.hook_log.lock().unwrap();
        log.push(format!("f{} p{} hook {} seen", ctx.frame.frame, ctx.pass, ctx.messages.len()));
    }
}

/// Single-pass system after the relay: sees everything the relay's passes sent it this frame.
struct Watch;

impl System for Watch {
    fn name(&self) -> &'static str {
        "watch"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let seen: Vec<String> = ctx.messages::<Seen>().map(|(_, s)| s.0.clone()).collect();
        if !seen.is_empty() {
            let line = format!("f{} watch s{} got {}", ctx.frame.frame, ctx.sector.0, seen.join(" | "));
            ctx.world.resource_mut::<Log>().0.push(line);
        }
        ctx.request_pass(); // ignored: single-pass system
    }
}

/// Local work without messages: every world counts down its own budget, one unit per pass,
/// and asks for another pass while budget remains; the hook asks for one pass more in frame 0.
#[derive(Default, Debug)]
struct Budget(u32);

struct Countdown {
    max: u32,
}

impl System for Countdown {
    fn name(&self) -> &'static str {
        "countdown"
    }
    fn max_passes(&self) -> u32 {
        self.max
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        if ctx.frame.frame == 0 && ctx.pass == 0 {
            ctx.world.resource_mut::<Budget>().0 = ctx.sector.0 % 4;
        }
        let pass = ctx.pass;
        let b = ctx.world.resource_mut::<Budget>();
        if b.0 > 0 {
            b.0 -= 1;
            let left = b.0;
            ctx.world.resource_mut::<Log>().0.push(format!("p{pass} left {left}"));
            ctx.request_pass();
        }
    }
    fn barrier(&self, ctx: &mut BarrierCtx<'_>) {
        if ctx.frame.frame == 0 && ctx.pass == 3 {
            ctx.request_pass();
        }
    }
}

fn universe(sectors: u32) -> Universe {
    let mut u = Universe::new(7);
    for s in 0..sectors {
        u.add_sector(UniverseId(0x5000 + u64::from(s)), WorldKind::Sector);
    }
    u.set_message_log(true);
    u
}

fn frame(n: u64) -> FrameInfo {
    FrameInfo { frame: n, game_time: n as f64 / 60.0, dt: 1.0 / 60.0 }
}

/// Per-world logs, per-pass reports, message log, hook log.
type Outcome = (Vec<String>, Vec<String>, Vec<String>, Vec<String>);

/// Everything that must not depend on the thread count.
fn outcome(u: &mut Universe, reports: &[FrameReport], hook: &[String]) -> Outcome {
    let logs = u.worlds().iter().flat_map(|w| w.resource::<Log>().map(|l| l.0.clone()).unwrap_or_default()).collect();
    let phases = reports
        .iter()
        .flat_map(|r| {
            r.phases.iter().map(move |p| {
                format!(
                    "f{} {} pass{} more{} msgs{} dropped{}",
                    r.frame, p.system, p.pass, p.more, p.messages, p.dropped_messages
                )
            })
        })
        .collect();
    (logs, phases, u.take_message_log(), hook.to_vec())
}

fn run_relay(threads: usize, base: u32, max: u32, frames: u64) -> (Vec<FrameReport>, Outcome) {
    let pool = Pool::new(threads);
    let mut u = universe(7);
    let relay = Relay { sectors: 7, base, max, hook_log: Mutex::new(Vec::new()) };
    let reports: Vec<FrameReport> = (0..frames).map(|f| u.step(&pool, &[&relay, &Watch], &frame(f))).collect();
    let hook = relay.hook_log.lock().unwrap().clone();
    let out = outcome(&mut u, &reports, &hook);
    (reports, out)
}

#[test]
fn relay_runs_until_quiescent_and_is_thread_invariant() {
    // tokens start in sectors 0, 3, 6 with 2, 5, 8 hops; token 6 is delivered in passes 1..=8
    // (sectors 0, 1, .., 6, 0) and arrives in pass 8 with 0 hops, so pass 8 ends quiescent.
    let (reports, base) = run_relay(1, 2, 0x21, 2);
    assert_eq!(reports[0].passes("relay"), 9);
    assert_eq!(reports[0].passes("watch"), 1, "request_pass is ignored for single-pass systems");
    let relay: Vec<_> = reports[0].phases.iter().filter(|p| p.system == "relay").collect();
    assert!(relay[..8].iter().all(|p| p.more) && !relay[8].more);
    assert_eq!(relay.iter().map(|p| p.pass).collect::<Vec<_>>(), (0..9).collect::<Vec<_>>());
    assert_eq!(reports[1].passes("relay"), 1, "frame 1 has nothing to do");
    assert!(!reports[1].phases[0].more);
    let (logs, _, _, hook) = &base;
    assert!(logs.contains(&"f0 p8 s0 token6 hops0".to_string()), "{logs:?}");
    assert!(logs.contains(&"f0 p0 s0 token0 hops2".to_string()));
    assert_eq!(hook.len(), 10, "one hook per pass: 9 in frame 0, 1 in frame 1");
    // the watch system runs after all relay passes and gets their reports in routing order
    let watch0 = logs.iter().find(|l| l.starts_with("f0 watch s0 got")).unwrap();
    assert!(watch0.starts_with("f0 watch s0 got f0 p0 s0 token0 hops2 | "), "{watch0}");
    for threads in [2, 16] {
        assert_eq!(run_relay(threads, 2, 0x21, 2).1, base, "{threads} threads differ");
    }
}

#[test]
fn the_pass_bound_leaves_work_for_the_next_frame() {
    let (reports, out) = run_relay(4, 2, 4, 3);
    assert_eq!(reports[0].passes("relay"), 4);
    let last = reports[0].phases.iter().rfind(|p| p.system == "relay").unwrap();
    assert!(last.more, "bound reached with tokens in flight");
    // the tokens in flight are delivered in the next frame's passes, in the same order
    assert_eq!(reports[1].passes("relay"), 4);
    assert_eq!(reports[2].passes("relay"), 1, "9 passes of work in total: 4 + 4 + 1");
    let logs = &out.0;
    assert!(logs.contains(&"f2 p0 s0 token6 hops0".to_string()), "{logs:?}");
    for threads in [1, 16] {
        assert_eq!(run_relay(threads, 2, 4, 3).1, out, "{threads} threads differ");
    }
}

#[test]
fn pass_requests_from_jobs_and_the_hook() {
    let run = |threads: usize| {
        let pool = Pool::new(threads);
        let mut u = universe(9);
        let sys = Countdown { max: 10 };
        let reports: Vec<FrameReport> = (0..2).map(|f| u.step(&pool, &[&sys], &frame(f))).collect();
        (reports.iter().map(|r| r.passes("countdown")).collect::<Vec<_>>(), outcome(&mut u, &reports, &[]))
    };
    let (passes, out) = run(1);
    // budgets 0..3: requests in passes 0, 1, 2; the hook asks once more in pass 3, then quiet
    assert_eq!(passes, vec![5, 1]);
    assert!(out.0.contains(&"p2 left 0".to_string()));
    assert_eq!(run(8), (passes, out));
}
