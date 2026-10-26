//! First subsystem: read the local player's ship pose in its sector every frame through
//! exports, derive linear and angular velocity, and log a summary once per report
//! interval. **Read only**: it never writes engine state.
//!
//! Velocity: build 23660954 exports no velocity getter (FFI-INVENTORY.md). Velocity is
//! derived from consecutive poses over the engine's TLS game time (`GetCurrentGameTime`,
//! the time base `GetObjectPositionInSector` uses). The scalar `speed` from
//! `GetComponentDetails` is sampled once per summary as an independent cross-check.
//! Derivation restarts when the ship or sector changes, on paused frames (no time
//! step) and after any invalid sample.

use std::f64::consts::PI;

use crate::ffi::{fmt_id, Capabilities, Capability, Engine, UiPosRot, Unavailable, UniverseId};
use crate::frame::{FrameCtx, FrameInput, Subsystem};

/// |position| above this (m) is treated as invalid (X4 sectors are ~ a few 100 km).
pub const MAX_COORD: f32 = 1.0e8;

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct Sample {
    pub ship: UniverseId,
    pub sector: UniverseId,
    pub game_time: f64,
    pub pose: UiPosRot,
    /// m/s in sector coordinates; `None` until two consecutive valid samples exist.
    pub velocity: Option<[f64; 3]>,
    /// rad/s of yaw, pitch, roll.
    pub angular: Option<[f64; 3]>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PoseCounters {
    pub samples: u64,
    pub derived: u64,
    pub no_ship: u64,
    pub no_sector: u64,
    pub invalid: u64,
    pub paused: u64,
    pub ship_changes: u64,
    pub sector_changes: u64,
}

pub struct PlayerPose {
    last: Option<Sample>,
    counters: PoseCounters,
    window: PoseCounters,
    max_speed: f64,
    /// Report extras looked up at summary time (cheap, 1 Hz).
    pending_identity: bool,
    id_code: Option<String>,
    export_speed: Option<i32>,
}

fn wrap(angle: f64) -> f64 {
    let a = (angle + PI).rem_euclid(2.0 * PI) - PI;
    if a <= -PI {
        a + 2.0 * PI
    } else {
        a
    }
}

impl PlayerPose {
    pub fn new() -> Self {
        PlayerPose {
            last: None,
            counters: PoseCounters::default(),
            window: PoseCounters::default(),
            max_speed: 0.0,
            pending_identity: true,
            id_code: None,
            export_speed: None,
        }
    }

    pub fn last(&self) -> Option<Sample> {
        self.last
    }
    pub fn counters(&self) -> PoseCounters {
        self.counters
    }

    fn count(&mut self, f: impl Fn(&mut PoseCounters)) {
        f(&mut self.counters);
        f(&mut self.window);
    }

    fn sample(&mut self, engine: &Engine<'_>, paused_hint: bool) -> Result<(), Unavailable> {
        let paused = engine.paused().unwrap_or(paused_hint);
        let Some(ship) = engine.player_occupied_ship()? else {
            self.count(|c| c.no_ship += 1);
            self.last = None;
            return Ok(());
        };
        let Some(sector) = engine.sector_of(ship)? else {
            self.count(|c| c.no_sector += 1);
            self.last = None;
            return Ok(());
        };
        let game_time = engine.game_time()?;
        let pose = engine.pose_in_sector(ship)?;
        if !pose.is_finite() || [pose.x, pose.y, pose.z].iter().any(|v| v.abs() > MAX_COORD) || !game_time.is_finite() {
            self.count(|c| c.invalid += 1);
            self.last = None;
            return Ok(());
        }
        self.count(|c| c.samples += 1);
        if paused {
            self.count(|c| c.paused += 1);
        }
        let mut sample = Sample { ship, sector, game_time, pose, velocity: None, angular: None };
        if let Some(prev) = self.last {
            if prev.ship != ship {
                self.count(|c| c.ship_changes += 1);
                self.pending_identity = true;
            } else if prev.sector != sector {
                self.count(|c| c.sector_changes += 1);
            } else {
                let dt = game_time - prev.game_time;
                if dt > 1e-6 && !paused {
                    let d = |a: f32, b: f32| (f64::from(a) - f64::from(b)) / dt;
                    let v = [d(pose.x, prev.pose.x), d(pose.y, prev.pose.y), d(pose.z, prev.pose.z)];
                    let w = |a: f32, b: f32| wrap(f64::from(a) - f64::from(b)) / dt;
                    sample.velocity = Some(v);
                    sample.angular = Some([
                        w(pose.yaw, prev.pose.yaw),
                        w(pose.pitch, prev.pose.pitch),
                        w(pose.roll, prev.pose.roll),
                    ]);
                    self.max_speed = self.max_speed.max((v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt());
                    self.count(|c| c.derived += 1);
                } else if dt.abs() <= 1e-6 || paused {
                    // Same time step (paused, or several callbacks per engine tick): keep
                    // the previous derivation instead of dividing by ~0.
                    sample.velocity = prev.velocity;
                    sample.angular = prev.angular;
                }
            }
        }
        self.last = Some(sample);
        Ok(())
    }
}

impl Default for PlayerPose {
    fn default() -> Self {
        Self::new()
    }
}

impl Subsystem for PlayerPose {
    fn name(&self) -> &'static str {
        "player_pose"
    }

    fn needs(&self) -> Capabilities {
        Capabilities::of(&[Capability::Clock, Capability::PoseRead, Capability::PlayerShip])
    }

    fn on_frame(&mut self, ctx: &mut FrameCtx<'_>) -> Result<(), &'static str> {
        let Some(engine) = ctx.engine() else { return Ok(()) };
        // A missing export after registration means the binding changed: stop.
        self.sample(engine, ctx.input.paused).map_err(|e| e.0)?;
        if self.pending_identity {
            if let Some(last) = self.last {
                self.id_code = engine.id_code(last.ship).ok().flatten();
                self.pending_identity = false;
            }
        }
        // Scalar engine speed for the cross-check, at most once per report window.
        if self.export_speed.is_none() {
            if let (Some(last), true) = (self.last, engine.capabilities().has(Capability::Details)) {
                self.export_speed = engine.details(last.ship).ok().map(|d| d.speed);
            }
        }
        Ok(())
    }

    fn summary(&mut self, now: &FrameInput) -> Option<String> {
        let w = self.window;
        let mut line = format!(
            "X4MP_CORE POSE frame={} samples={} dsamples={} derived={} no_ship={} no_sector={} invalid={} paused={} ship_changes={} sector_changes={}",
            now.frame, self.counters.samples, w.samples, w.derived, w.no_ship, w.no_sector, w.invalid, w.paused,
            w.ship_changes, w.sector_changes
        );
        match self.last {
            Some(s) => {
                let p = s.pose;
                line.push_str(&format!(
                    " ship={} idcode={} sector={} gt={:.3} pos={:.1},{:.1},{:.1} rot={:.4},{:.4},{:.4}",
                    fmt_id(Some(s.ship)),
                    self.id_code.as_deref().unwrap_or("?"),
                    fmt_id(Some(s.sector)),
                    s.game_time,
                    p.x,
                    p.y,
                    p.z,
                    p.yaw,
                    p.pitch,
                    p.roll
                ));
                match s.velocity {
                    Some(v) => line.push_str(&format!(
                        " vel={:.2},{:.2},{:.2} speed={:.2}",
                        v[0],
                        v[1],
                        v[2],
                        (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt()
                    )),
                    None => line.push_str(" vel=? speed=?"),
                }
                if let Some(a) = s.angular {
                    line.push_str(&format!(" angvel={:.4},{:.4},{:.4}", a[0], a[1], a[2]));
                }
            }
            None => line.push_str(" ship=0"),
        }
        match self.export_speed {
            Some(speed) => line.push_str(&format!(" export_speed={speed}")),
            None => line.push_str(" export_speed=?"),
        }
        line.push_str(&format!(" max_speed={:.2}", self.max_speed));
        self.window = PoseCounters::default();
        self.max_speed = 0.0;
        self.export_speed = None;
        Some(line)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ffi::fake::{self, World};
    use crate::frame::testing::{driver, FakeClock};
    use crate::frame::{DriverConfig, State};

    fn pose(x: f32, yaw: f32) -> UiPosRot {
        UiPosRot { x, y: 0.0, z: 0.0, yaw, pitch: 0.0, roll: 0.0 }
    }

    fn input(n: u64) -> FrameInput {
        FrameInput { frame: n, game_time: 0.0, dt: 0.0, paused: false, monotonic_ns: n * 100_000_000 }
    }

    #[test]
    fn angle_wrap() {
        assert!((wrap(2.0 * PI - 0.1) + 0.1).abs() < 1e-12);
        assert!((wrap(-2.0 * PI + 0.1) - 0.1).abs() < 1e-12);
        assert!((wrap(PI) - PI).abs() < 1e-12);
    }

    #[test]
    fn derives_velocity_from_fake_engine_and_reports() {
        let (api, report) = fake::api();
        assert!(report.capabilities.has(Capability::PoseRead));
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig { report_interval_ns: 1_000_000_000, ..Default::default() });
        let id = d.add(Box::new(PlayerPose::new()), None, api.capabilities());
        assert_eq!(d.state(id), Some(State::Active));

        let mut world = World { occupied: 42, ..Default::default() };
        world.sector_of.insert(42, 7);
        fake::reset(world);
        for n in 0..=10u64 {
            let t = n as f64 * 0.1;
            fake::with(|w| {
                w.game_time = t;
                // 100 m/s along x; yaw turning at 1 rad/s across the +/-pi seam.
                w.poses.insert(42, pose(100.0 * t as f32, wrap(PI - 0.5 + t) as f32));
                w.speed = 100;
            });
            let engine = unsafe { Engine::enter(&api, false) };
            d.run(&input(n), Some(&engine));
        }
        assert_eq!(d.state(id), Some(State::Active));
        let lines: Vec<String> = d.log.drain().collect();
        let line = lines.iter().find(|l| l.starts_with("X4MP_CORE POSE")).expect("pose line");
        assert!(line.contains("frame=10 samples=11 dsamples=11 derived=10 no_ship=0"), "{line}");
        assert!(line.contains(" ship=0x2a idcode=ABC-123 sector=0x7 gt=1.000 pos=100.0,0.0,0.0"), "{line}");
        assert!(line.contains(" vel=100.00,0.00,0.00 speed=100.00 angvel=1.0000,0.0000,0.0000"), "{line}");
        assert!(line.contains(" export_speed=100 "), "{line}");
        assert!(fake::with(|w| w.writes.is_empty()), "read-only subsystem wrote");
    }

    #[test]
    fn resets_on_ship_or_sector_change_and_no_ship() {
        let (api, _) = fake::api();
        let engine = unsafe { Engine::enter(&api, false) };
        let mut p = PlayerPose::new();
        let mut world = World { occupied: 1, game_time: 1.0, ..Default::default() };
        world.sector_of.insert(1, 10);
        world.sector_of.insert(2, 10);
        world.poses.insert(1, pose(0.0, 0.0));
        world.poses.insert(2, pose(5.0, 0.0));
        fake::reset(world);
        p.sample(&engine, false).unwrap();
        fake::with(|w| {
            w.occupied = 2;
            w.game_time = 1.1;
        });
        p.sample(&engine, false).unwrap();
        assert_eq!(p.last().unwrap().velocity, None, "no velocity across a ship change");
        fake::with(|w| {
            w.sector_of.insert(2, 11);
            w.game_time = 1.2;
        });
        p.sample(&engine, false).unwrap();
        assert_eq!(p.last().unwrap().velocity, None, "no velocity across a sector change");
        fake::with(|w| w.occupied = 0);
        p.sample(&engine, false).unwrap();
        assert!(p.last().is_none());
        let c = p.counters();
        assert_eq!((c.ship_changes, c.sector_changes, c.no_ship, c.derived), (1, 1, 1, 0));
    }

    #[test]
    fn paused_and_invalid_samples() {
        let (api, _) = fake::api();
        let engine = unsafe { Engine::enter(&api, false) };
        let mut p = PlayerPose::new();
        let mut world = World { occupied: 1, game_time: 1.0, ..Default::default() };
        world.sector_of.insert(1, 10);
        world.poses.insert(1, pose(0.0, 0.0));
        fake::reset(world);
        p.sample(&engine, false).unwrap();
        fake::with(|w| {
            w.game_time = 1.1;
            w.poses.insert(1, pose(1.0, 0.0));
        });
        p.sample(&engine, false).unwrap();
        let v = p.last().unwrap().velocity.unwrap();
        assert!((v[0] - 10.0).abs() < 1e-3);
        // Paused: same game time, velocity is carried, not recomputed.
        fake::with(|w| w.paused = true);
        p.sample(&engine, false).unwrap();
        assert_eq!(p.last().unwrap().velocity, Some(v));
        fake::with(|w| w.poses.insert(1, pose(f32::NAN, 0.0)));
        p.sample(&engine, false).unwrap();
        assert!(p.last().is_none());
        assert_eq!((p.counters().paused, p.counters().invalid), (1, 1));
    }

    #[test]
    fn missing_pose_export_keeps_it_suspended() {
        let clock = FakeClock::default();
        let mut d = driver(&clock, DriverConfig::default());
        let id = d.add(Box::new(PlayerPose::new()), None, Capabilities::of(&[Capability::Clock]));
        assert_eq!(d.state(id), Some(State::Suspended("missing_capabilities")));
    }
}
