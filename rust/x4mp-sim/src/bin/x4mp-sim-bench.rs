//! x4mp-sim-bench: per-sector scheduler benchmark at scale (synthetic workload).
//!
//! ```text
//! cargo run -p x4mp-sim --release --bin x4mp-sim-bench -- [--entities 10000,50000,200000]
//!     [--sectors 8,50,200] [--threads 1,2,4,8,16,32] [--frames 60] [--warmup 10] [--work 64]
//!     [--msg-per-mille 10] [--migrate-per-mille 1] [--quick]
//! ```
//! Workload per entity and frame (one system, every world in parallel): kinematics on the
//! transform (f32, engine operation order), `--work` rounds of float work seeded from the
//! entity's engine RNG stream (stands in for per-entity behaviour), a cross-sector message
//! from `--msg-per-mille` entities (snapshot-based target choice), a migration from
//! `--migrate-per-mille`. Each configuration checks that the final state hash is identical for
//! every thread count (the determinism guarantee) and prints frame time, parallel-phase time,
//! barrier time and speedup. This is synthetic CPU load: it measures the core's scheduling
//! and barrier overhead and its scaling, not engine systems.

use std::time::Instant;

use x4mp_sim::comp::{Hull, Mat4f, Transform, Vel, Velocity};
use x4mp_sim::{Bundle, FrameInfo, Pool, SectorCtx, SectorIx, System, Target, Universe, UniverseId, WorldKind};

#[derive(Debug)]
struct Hit {
    amount: f64,
}

struct Load {
    sectors: u32,
    work: u32,
    msg: u32,
    migrate: u32,
}

impl System for Load {
    fn name(&self) -> &'static str {
        "load"
    }
    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        for env in ctx.inbox {
            if let (Some(h), x4mp_sim::Target::Sector(_)) = (env.get::<Hit>(), env.to) {
                if let Some(first) = ctx.world.col_mut::<Hull>().first_mut() {
                    first.current -= h.amount;
                }
            }
        }
        let dt = ctx.frame.dt as f32;
        {
            let (t, v) = ctx.world.cols_mut::<Transform, Velocity>();
            for (t, v) in t.iter_mut().zip(v.iter()) {
                for k in 0..3 {
                    t.0 .0[0][k] += v.0.linear[k] * dt;
                }
            }
        }
        let n = ctx.world.len();
        for row in 0..n {
            let roll = ctx.rng(row).u32_below(1000);
            // behaviour stand-in: a short dependent float chain from the entity's state
            let mut x = ctx.world.col::<Transform>()[row].0 .0[0][0] * 1e-4;
            for _ in 0..self.work {
                x = x * 0.999_9 + 0.000_1 * (x * x - 0.5);
            }
            ctx.world.col_mut::<Hull>()[row].current += f64::from(x) * 1e-9;
            if roll < self.msg {
                let to = SectorIx((ctx.sector.0 * 31 + roll) % self.sectors);
                ctx.out.send(Target::Sector(to), Hit { amount: f64::from(roll) * 1e-3 });
            } else if roll >= 1000 - self.migrate {
                let e = ctx.world.entities()[row];
                ctx.out.migrate(e, SectorIx((ctx.sector.0 + 1 + roll % 7) % self.sectors));
            }
        }
    }
}

fn build(entities: u32, sectors: u32) -> Universe {
    let mut u = Universe::new(0xbe9c4);
    for s in 0..sectors {
        let ix = u.add_sector(UniverseId(0x7000_0000 + u64::from(s)), WorldKind::Sector);
        let per = entities / sectors + u32::from(s < entities % sectors);
        for i in 0..per {
            let mut m = Mat4f::IDENTITY;
            m.0[0] = [i as f32, s as f32, 0.5, 0.0];
            let v = Vel { linear: [1.0 + (i % 13) as f32, -2.0, 0.25, 0.0], angular: [0.0; 4] };
            let uid = UniverseId((u64::from(s) << 32) | u64::from(i + 1));
            u.spawn(ix, uid, Bundle::new().with(Transform(m)).with(Velocity(v)).with(Hull { current: 1.0, max: 1.0 }))
                .expect("spawn");
        }
    }
    u.commit();
    u
}

struct Sample {
    frame_ms: Vec<f64>,
    parallel_ms: Vec<f64>,
    barrier_ms: Vec<f64>,
    split_ms: Vec<[f64; 4]>,
    ideal: Vec<f64>,
    messages: u64,
    migrations: u64,
    hash: u64,
}

fn run(entities: u32, sectors: u32, threads: usize, frames: u64, warmup: u64, load: &Load) -> Sample {
    let pool = Pool::new(threads);
    let mut u = build(entities, sectors);
    let mut s = Sample {
        frame_ms: vec![],
        parallel_ms: vec![],
        barrier_ms: vec![],
        split_ms: vec![],
        ideal: vec![],
        messages: 0,
        migrations: 0,
        hash: 0,
    };
    for f in 0..warmup + frames {
        let fi = FrameInfo { frame: f, game_time: f as f64 / 60.0, dt: 1.0 / 60.0 };
        let t = Instant::now();
        let rep = u.step(&pool, &[load], &fi);
        let ms = t.elapsed().as_secs_f64() * 1e3;
        if f >= warmup {
            let p = &rep.phases[0];
            s.frame_ms.push(ms);
            s.parallel_ms.push(p.parallel_ns as f64 / 1e6);
            s.barrier_ms.push(p.barrier_ns as f64 / 1e6);
            s.split_ms.push([p.commands_ns, p.commit_ns, p.route_ns, p.hook_ns].map(|v| v as f64 / 1e6));
            s.ideal.push(p.ideal_speedup());
            s.messages += p.messages;
            s.migrations += p.migrated;
        }
    }
    s.hash = u.state_hash();
    s
}

fn median(v: &[f64]) -> f64 {
    let mut v = v.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    if v.is_empty() {
        0.0
    } else {
        v[v.len() / 2]
    }
}

fn p95(v: &[f64]) -> f64 {
    let mut v = v.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    if v.is_empty() {
        0.0
    } else {
        v[((v.len() as f64 * 0.95).ceil() as usize).saturating_sub(1).min(v.len() - 1)]
    }
}

fn list<T: std::str::FromStr + Clone>(args: &[String], name: &str, default: &[T]) -> Vec<T> {
    match args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)) {
        Some(v) => v.split(',').filter_map(|x| x.trim().parse().ok()).collect(),
        None => default.to_vec(),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let quick = args.iter().any(|a| a == "--quick");
    let entities =
        list(&args, "--entities", if quick { &[2_000u32][..] } else { &[10_000, 50_000, 100_000, 200_000][..] });
    let sectors = list(&args, "--sectors", if quick { &[8u32][..] } else { &[8, 50, 200][..] });
    let threads = list(&args, "--threads", if quick { &[1usize, 2][..] } else { &[1, 2, 4, 8, 16, 32][..] });
    let frames = list(&args, "--frames", &[if quick { 5u64 } else { 60 }])[0];
    let warmup = list(&args, "--warmup", &[if quick { 1u64 } else { 10 }])[0];
    let work = list(&args, "--work", &[64u32])[0];
    let msg = list(&args, "--msg-per-mille", &[10u32])[0];
    let migrate = list(&args, "--migrate-per-mille", &[1u32])[0];
    println!(
        "x4mp-sim-bench: frames={frames} warmup={warmup} work={work} msg/1000={msg} migrate/1000={migrate} cores={}",
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0)
    );
    println!("| entities | sectors | threads | frame ms (median) | p95 | parallel ms | barrier ms (cmd/commit/route/hook) | speedup | ideal (sum/max) | msgs/frame | migr/frame | hash |");
    println!("|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---|");
    let mut all_equal = true;
    for &e in &entities {
        for &s in &sectors {
            let load = Load { sectors: s, work, msg, migrate };
            let mut base_ms = None;
            let mut base_hash = None;
            for &t in &threads {
                let r = run(e, s, t, frames, warmup, &load);
                let m = median(&r.frame_ms);
                let b = *base_ms.get_or_insert(m);
                let h = *base_hash.get_or_insert(r.hash);
                let same = h == r.hash;
                all_equal &= same;
                println!(
                    "| {e} | {s} | {t} | {m:.3} | {:.3} | {:.3} | {:.3} ({}) | {:.2}x | {:.1}x | {:.0} | {:.1} | {:016x}{} |",
                    p95(&r.frame_ms),
                    median(&r.parallel_ms),
                    median(&r.barrier_ms),
                    (0..4)
                        .map(|k| format!("{:.2}", median(&r.split_ms.iter().map(|x| x[k]).collect::<Vec<_>>())))
                        .collect::<Vec<_>>()
                        .join("/"),
                    b / m.max(1e-9),
                    median(&r.ideal),
                    r.messages as f64 / frames as f64,
                    r.migrations as f64 / frames as f64,
                    r.hash,
                    if same { "" } else { " MISMATCH" }
                );
            }
        }
    }
    println!("determinism: {}", if all_equal { "all thread counts identical" } else { "MISMATCH" });
    if !all_equal {
        std::process::exit(1);
    }
}
