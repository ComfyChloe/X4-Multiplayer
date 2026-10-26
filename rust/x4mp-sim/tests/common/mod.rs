//! Shared helpers for the integration tests: a deterministic PRNG for property tests and a
//! synthetic multi-sector scenario exercising every scheduler path.
#![allow(dead_code)]

use std::sync::Mutex;

use x4mp_sim::comp::{Hull, Mat4f, Transform, Vel, Velocity};
use x4mp_sim::{
    BarrierCtx, Bundle, Component, EntityId, FrameInfo, SectorCtx, SectorIx, System, Target, Universe, UniverseId,
    WorldKind,
};

/// splitmix64: test-input generator (independent of the engine generator under test).
pub struct TestRng(pub u64);

impl TestRng {
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        self.next() % n.max(1)
    }
    pub fn f32(&mut self, scale: f32) -> f32 {
        ((self.next() >> 40) as f32 / (1u64 << 24) as f32 - 0.5) * scale
    }
}

/// A custom per-entity component (systems add their own like this).
#[derive(Clone, Copy, Default, Debug, PartialEq)]
pub struct Age(pub u32);
x4mp_sim::impl_fields!(newtype Age);
impl Component for Age {
    const NAME: &'static str = "age";
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Damage {
    pub target: EntityId,
    pub amount: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Report {
    pub uid: UniverseId,
    pub hull_bits: u64,
    pub frame: u64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Census {
    pub sector: u32,
    pub count: u32,
}

/// Builds `sectors` worlds with `per_sector` ships each (positions, velocities, hull).
pub fn build_universe(seed: u64, sectors: u32, per_sector: u32) -> Universe {
    let mut u = Universe::new(seed);
    u.register::<Age>();
    u.publish::<Hull>();
    u.publish::<Transform>();
    let mut rng = TestRng(seed ^ 0xabcdef);
    for s in 0..sectors {
        let ix = u.add_sector(UniverseId(0x1000_0000 + u64::from(s)), WorldKind::Sector);
        for i in 0..per_sector {
            let mut m = Mat4f::IDENTITY;
            m.0[0] = [rng.f32(20_000.0), rng.f32(20_000.0), rng.f32(20_000.0), 0.0];
            let v = Vel { linear: [rng.f32(400.0), rng.f32(400.0), rng.f32(400.0), 0.0], angular: [0.0; 4] };
            let b = Bundle::new()
                .with(Transform(m))
                .with(Velocity(v))
                .with(Hull { current: 100.0 + f64::from(i % 7) * 250.0, max: 2500.0 });
            u.spawn(ix, UniverseId((u64::from(s) << 20) | u64::from(i + 1)), b).expect("spawn");
        }
    }
    u.commit();
    u
}

/// Synthetic traffic: integrates motion, shoots across sectors (Entity messages), reports to
/// the barrier, migrates through a "gate", despawns wrecks, spawns replacements.
pub struct Traffic {
    pub sectors: u32,
    pub gate: f32,
    pub log: Mutex<Vec<String>>,
}

impl Traffic {
    pub fn new(sectors: u32) -> Traffic {
        Traffic { sectors, gate: 9_000.0, log: Mutex::new(Vec::new()) }
    }
}

impl System for Traffic {
    fn name(&self) -> &'static str {
        "traffic"
    }

    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        // Deliveries from the previous phase, in total order.
        for env in ctx.inbox {
            if let Some(d) = env.get::<Damage>() {
                if let Some(row) = ctx.row_of(d.target) {
                    ctx.world.col_mut::<Hull>()[row].current -= d.amount;
                }
            }
        }
        let dt = ctx.frame.dt as f32;
        {
            let (tr, vel) = ctx.world.cols_mut::<Transform, Velocity>();
            for (t, v) in tr.iter_mut().zip(vel.iter()) {
                for k in 0..3 {
                    t.0 .0[0][k] += v.0.linear[k] * dt;
                }
            }
        }
        let n = ctx.world.len();
        let mut spawned = 0u64;
        for row in 0..n {
            let e = ctx.world.entities()[row];
            let uid = ctx.world.uids()[row];
            let roll = ctx.rng(row).u32_below(1000);
            let age = {
                let a = &mut ctx.world.col_mut::<Age>()[row];
                a.0 += 1;
                a.0
            };
            if roll < 15 {
                // Fire at the strongest ship of another sector, as seen in the snapshot.
                let t = SectorIx((ctx.sector.0 * 7 + 3 + roll) % self.sectors);
                if let (Some(hulls), Some(uids)) = (ctx.snapshot.column::<Hull>(t), ctx.snapshot.uids(t)) {
                    // Even rolls hit the strongest ship, odd rolls the weakest (ties: lowest row).
                    let pick = |weakest: bool| {
                        hulls
                            .iter()
                            .enumerate()
                            .max_by(|a, b| {
                                let o = a.1.current.total_cmp(&b.1.current);
                                if weakest { o.reverse() } else { o }.then(b.0.cmp(&a.0))
                            })
                            .map(|(i, _)| i)
                    };
                    let best = pick(roll % 2 == 1);
                    if let Some(i) = best {
                        if let Some(target) = ctx.ids.by_uid(uids[i]) {
                            let amount = f64::from(ctx.rng(row).float(300.0));
                            ctx.out.send(Target::Entity(target), Damage { target, amount });
                        }
                    }
                }
            }
            if roll == 500 {
                let hull = ctx.world.col::<Hull>()[row].current;
                ctx.out.send(Target::Barrier, Report { uid, hull_bits: hull.to_bits(), frame: ctx.frame.frame });
            }
            let x = ctx.world.col::<Transform>()[row].0 .0[0][0];
            if ctx.world.col::<Hull>()[row].current <= 0.0 {
                ctx.out.despawn(e);
                if roll % 2 == 0 && spawned < 2 {
                    let new_uid =
                        UniverseId((1 << 62) | (ctx.frame.frame << 24) | (u64::from(ctx.sector.0) << 8) | spawned);
                    let mut m = Mat4f::IDENTITY;
                    m.0[0][0] = f32::from(u8::try_from(age % 200).unwrap_or(0)) * 10.0;
                    ctx.out.spawn(
                        ctx.sector,
                        new_uid,
                        Bundle::new().with(Transform(m)).with(Hull { current: 800.0, max: 800.0 }),
                    );
                    spawned += 1;
                }
            } else if x.abs() > self.gate {
                let t = &mut ctx.world.col_mut::<Transform>()[row];
                t.0 .0[0][0] = -t.0 .0[0][0] * 0.5;
                let to = SectorIx((ctx.sector.0 + 1 + roll % 3) % self.sectors);
                ctx.out.migrate(e, to);
            }
        }
        ctx.out.send(Target::Barrier, Census { sector: ctx.sector.0, count: n as u32 });
    }

    fn barrier(&self, ctx: &mut BarrierCtx<'_>) {
        let mut log = self.log.lock().unwrap();
        for env in ctx.messages {
            log.push(format!("f{} {}", ctx.frame.frame, env.describe()));
        }
    }
}

pub fn frame(n: u64) -> FrameInfo {
    FrameInfo { frame: n, game_time: n as f64 / 60.0, dt: 1.0 / 60.0 }
}
