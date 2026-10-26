//! The turret joint step as an x4mp-sim system (rewrite-coord/interfaces.md, x4mp-sim v0.5).
//!
//! Each turret joint is an entity of its sector's world with three components:
//! [`JointState`] (the `U::IKValue` fields the step reads and writes), [`JointInput`] (this
//! frame's command, written by the aim logic, which is still engine-side) and [`JointOutput`]
//! (the original's return value and which writes it would make). [`TurretJointSystem`] steps
//! every active joint of a sector with [`ikjoint::step_with_dt`], which is bit-identical to
//! `IKValue_RotateToward` (oracle, native shadow and switch runs: reports/REWRITE-COMBAT.md
//! sections 4, 9, 10). A step touches only its own joint, so the system sends no messages and
//! the results do not depend on the thread count or the order of sectors.
//!
//! The engine-side effects around the step (StartedAiming/StoppedAiming events, the turret's
//! at-rest flag) are not part of it; they stay with the engine and its event buffer.
//! Worker threads must run with the simulation thread's floating-point control bits: the
//! MoveWorker's MXCSR is 0x1fbf natively (round to nearest, no FTZ/DAZ; only sticky flags
//! set), the same control bits a fresh x4mp-sim worker starts with.

use x4mp_sim::{Component, SectorCtx, System};

use crate::ikjoint::{step_with_dt, JointCommand, JointState, Outcome};

x4mp_sim::impl_fields!(JointState { value, time, velocity, target, wrap });
x4mp_sim::impl_fields!(JointCommand { target, min, max, max_speed, accel, feed_forward, snap });

impl Component for JointState {
    const NAME: &'static str = "combat.joint";
}

/// This frame's command. Rows with `active == false` (entities that are not turret joints,
/// or joints the engine does not step this frame) are left alone.
#[derive(Debug, Clone, Copy, Default)]
pub struct JointInput {
    pub active: bool,
    pub command: JointCommand,
}
x4mp_sim::impl_fields!(JointInput { active, command });

impl Component for JointInput {
    const NAME: &'static str = "combat.joint_input";
}

/// The last step of a joint: whether it ran, the original's return value (reached or snapped)
/// and the outcome (which fields and calls the engine-side apply must perform, see
/// [`Outcome`]: 0 snapped, 1 waiting, 2 stepped).
#[derive(Debug, Clone, Copy, Default)]
pub struct JointOutput {
    pub stepped: bool,
    pub reached: bool,
    pub outcome: u8,
}
x4mp_sim::impl_fields!(JointOutput { stepped, reached, outcome });

impl Component for JointOutput {
    const NAME: &'static str = "combat.joint_output";
}

fn outcome_code(outcome: Outcome) -> u8 {
    match outcome {
        Outcome::Snapped => 0,
        Outcome::Waiting => 1,
        Outcome::Stepped => 2,
    }
}

/// Steps every active joint of a sector once per frame.
pub struct TurretJointSystem;

impl TurretJointSystem {
    pub const NAME: &'static str = "combat.turret_joint";

    /// Register the three joint components with a universe.
    pub fn register(universe: &mut x4mp_sim::Universe) {
        universe.register::<JointState>().register::<JointInput>().register::<JointOutput>();
    }
}

impl System for TurretJointSystem {
    fn name(&self) -> &'static str {
        Self::NAME
    }

    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let now = ctx.frame.game_time;
        // FrameInfo::dt is TLS+0x300 - TLS+0x328 in f64; the step converts it once, as the
        // original does.
        let dt = ctx.frame.dt as f32;
        let (joints, inputs, outputs) = ctx.world.cols_mut3::<JointState, JointInput, JointOutput>();
        for ((joint, input), output) in joints.iter_mut().zip(inputs.iter()).zip(outputs.iter_mut()) {
            if !input.active {
                output.stepped = false;
                continue;
            }
            let (reached, outcome) = step_with_dt(joint, &input.command, now, dt);
            *output = JointOutput { stepped: true, reached, outcome: outcome_code(outcome) };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ikjoint::{step, FrameClock};
    use crate::testrng::Rng;
    use x4mp_sim::{Bundle, EntityId, FrameInfo, Pool, Universe, UniverseId, WorldKind};

    fn random_state(rng: &mut Rng, now: f64) -> JointState {
        JointState {
            value: rng.range(-3.0, 3.0),
            time: now,
            velocity: rng.range(-2.0, 2.0),
            target: 0.0,
            wrap: rng.chance(0.5),
        }
    }

    fn random_command(rng: &mut Rng) -> JointCommand {
        JointCommand {
            target: rng.range(-6.0, 6.0),
            min: if rng.chance(0.5) { -1.2 } else { 0.0 },
            max: if rng.chance(0.5) { 1.2 } else { 0.0 },
            max_speed: rng.range(0.1, 3.0),
            accel: rng.range(0.1, 8.0),
            feed_forward: rng.range(-0.3, 0.3),
            snap: rng.chance(0.02),
        }
    }

    struct Setup {
        universe: Universe,
        joints: Vec<EntityId>,
    }

    /// `sectors` worlds with `per_sector` entities each; every fifth entity is not a joint.
    fn setup(sectors: u32, per_sector: u32, seed: u64) -> Setup {
        let mut universe = Universe::bare(0x5eed);
        TurretJointSystem::register(&mut universe);
        let mut rng = Rng::new(seed);
        let mut joints = Vec::new();
        for s in 0..sectors {
            let sector = universe.add_sector(UniverseId(1_000 + s as u64), WorldKind::Sector);
            for k in 0..per_sector {
                let active = k % 5 != 4;
                let bundle = Bundle::new()
                    .with(random_state(&mut rng, 100.0))
                    .with(JointInput { active, command: random_command(&mut rng) });
                let e = universe.spawn(sector, UniverseId(10_000 + (s * per_sector + k) as u64), bundle).unwrap();
                joints.push(e);
            }
        }
        universe.commit();
        Setup { universe, joints }
    }

    /// Frame clocks as the engine has them: game time steps of about 1/30 s.
    fn clocks(frames: usize) -> Vec<FrameClock> {
        let mut rng = Rng::new(77);
        let mut now = 100.0;
        (0..frames)
            .map(|_| {
                let previous = now;
                now += 1.0 / 30.0 + rng.unit() * 0.01;
                FrameClock { now, previous }
            })
            .collect()
    }

    fn run(threads: usize, frames: usize) -> (u64, Vec<(JointState, JointOutput)>) {
        let mut s = setup(12, 40, 3);
        let pool = Pool::new(threads);
        let system = TurretJointSystem;
        let mut commands = Rng::new(9);
        for (frame, clock) in clocks(frames).into_iter().enumerate() {
            // the aim logic writes new commands to a quarter of the joints every frame
            for &e in &s.joints {
                if commands.chance(0.25) {
                    let command = random_command(&mut commands);
                    let input = s.universe.get_mut::<JointInput>(e).unwrap();
                    input.command = command;
                }
            }
            let info = FrameInfo { frame: frame as u64, game_time: clock.now, dt: clock.now - clock.previous };
            let report = s.universe.step(&pool, &[&system], &info);
            assert!(report.errors.is_empty(), "{:?}", report.errors);
        }
        let states = s
            .joints
            .iter()
            .map(|&e| (*s.universe.get::<JointState>(e).unwrap(), *s.universe.get::<JointOutput>(e).unwrap()))
            .collect();
        (s.universe.state_hash(), states)
    }

    #[test]
    fn equals_the_serial_step_and_does_not_depend_on_threads() {
        const FRAMES: usize = 60;
        let (hash1, states1) = run(1, FRAMES);
        for threads in [2, 4, 16] {
            let (hash, states) = run(threads, FRAMES);
            assert_eq!(hash, hash1, "{threads} threads");
            assert_eq!(states.len(), states1.len());
        }
        // the same joints and commands through the plain serial step
        let s = setup(12, 40, 3);
        let mut reference: Vec<(JointState, JointInput)> = s
            .joints
            .iter()
            .map(|&e| (*s.universe.get::<JointState>(e).unwrap(), *s.universe.get::<JointInput>(e).unwrap()))
            .collect();
        let mut outputs = vec![JointOutput::default(); reference.len()];
        let mut commands = Rng::new(9);
        for clock in clocks(FRAMES) {
            for (_, input) in reference.iter_mut() {
                if commands.chance(0.25) {
                    input.command = random_command(&mut commands);
                }
            }
            for ((joint, input), output) in reference.iter_mut().map(|(j, i)| (j, *i)).zip(outputs.iter_mut()) {
                if input.active {
                    let (reached, outcome) = step(joint, &input.command, clock);
                    *output = JointOutput { stepped: true, reached, outcome: outcome_code(outcome) };
                } else {
                    output.stepped = false;
                }
            }
        }
        let mut stepped = 0;
        for (((joint, input), output), (ours, our_output)) in reference.iter().zip(&outputs).zip(&states1) {
            assert_eq!(joint.value.to_bits(), ours.value.to_bits());
            assert_eq!(joint.time.to_bits(), ours.time.to_bits());
            assert_eq!(joint.velocity.to_bits(), ours.velocity.to_bits());
            assert_eq!(joint.target.to_bits(), ours.target.to_bits());
            assert_eq!(
                (output.stepped, output.reached, output.outcome),
                (our_output.stepped, our_output.reached, our_output.outcome)
            );
            stepped += input.active as usize;
        }
        assert_eq!(stepped, 12 * 32, "inactive rows are not stepped");
    }

    #[test]
    fn inactive_rows_are_untouched() {
        let mut s = setup(2, 5, 11);
        let before: Vec<JointState> = s.joints.iter().map(|&e| *s.universe.get::<JointState>(e).unwrap()).collect();
        let info = FrameInfo { frame: 0, game_time: 100.05, dt: 0.05 };
        s.universe.step(&Pool::new(2), &[&TurretJointSystem], &info);
        for (k, &e) in s.joints.iter().enumerate() {
            let after = s.universe.get::<JointState>(e).unwrap();
            if k % 5 == 4 {
                assert_eq!(after.value.to_bits(), before[k].value.to_bits());
                assert_eq!(after.time.to_bits(), before[k].time.to_bits());
                assert!(!s.universe.get::<JointOutput>(e).unwrap().stepped);
            } else {
                assert!(s.universe.get::<JointOutput>(e).unwrap().stepped);
            }
        }
    }
}
