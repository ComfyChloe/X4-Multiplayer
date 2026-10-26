//! Turret joint step: the per-joint rotate-toward update of X4's IK joints.
//!
//! X4 build 23660954 aims a turret by moving each joint of its IK chains (yaw, pitch)
//! toward a target angle once per frame. The joint is a `U::IKValue` (0x680 bytes; its
//! `XLib::ValueSource<float>` virtual base sits at +0x678) and the step is the free
//! function at RVA 0xe1f840 (`IKValue_RotateToward` in `labels/rw-combat.tsv`). Callers:
//! the turret aim path (0x81c580 -> 0x7569a0 -> 0xe221a0 -> 0xe21110), the return-to-rest
//! path of an idle turret (0x81c7a0 -> 0x756a80) and the IK chain update 0xe209a0.
//!
//! [`rotate_toward`] reproduces the step's default branch (global byte 0x3d19b14 == 0,
//! "kinematic" mode; the alternative 50 Hz sub-stepped branch drives an embedded
//! animation controller and is not reimplemented). It keeps the original's operation
//! order and precision so that its results are bit-identical: f32 arithmetic in the
//! same order, the frame delta converted from f64 to f32 once, x86 `minss`/`maxss` and
//! `comiss` semantics for every comparison (including what happens with NaN), and the
//! constants with the exact bit patterns from the binary's .rdata. `fmodf` from the
//! game's CRT computes the exact IEEE remainder, which is what Rust's `%` computes.
//!
//! Results depend on MXCSR like the original's: run it on a thread whose MXCSR equals the
//! simulation thread's (see [`crate::mxcsr`]). The default MXCSR (0x1f80) and FTZ only
//! differ for subnormal intermediates.
//!
//! The behavioural description (no game code) is in `reports/REWRITE-COMBAT.md`.

/// 2*pi as stored at RVA 0x2cbebdc (0x40c90fdb).
pub const TWO_PI: f32 = f32::from_bits(0x40c9_0fdb);
/// pi as stored at RVA 0x2cbeab4 (0x40490fdb).
pub const PI: f32 = f32::from_bits(0x4049_0fdb);
/// The engine's 1e-4 "equal" tolerance at RVA 0x2cbe0ac (0x38d1b717).
pub const EPSILON: f32 = f32::from_bits(0x38d1_b717);
/// Braking margin 0.9 at RVA 0x2cbe550 (0x3f666666).
pub const BRAKE_MARGIN: f32 = f32::from_bits(0x3f66_6666);

/// The part of a `U::IKValue` that the step reads and writes.
#[derive(Debug, Clone, Copy, Default)]
pub struct JointState {
    /// +0x08: current angle (radians), the value source's value.
    pub value: f32,
    /// +0x10: game time of `value`.
    pub time: f64,
    /// +0x18: current angular velocity (radians per second).
    pub velocity: f32,
    /// +0x1c: target angle of the last step (after wrap-around normalisation).
    pub target: f32,
    /// +0x21: continuous joint (no end stops); targets are taken the short way round.
    pub wrap: bool,
}

/// The arguments of one step.
#[derive(Debug, Clone, Copy, Default)]
pub struct JointCommand {
    /// Desired angle.
    pub target: f32,
    /// Lower and upper limit. Both within 1e-4 of zero means "no limits", but the
    /// settle test against the limits still runs (see the report).
    pub min: f32,
    pub max: f32,
    /// Maximum angular speed.
    pub max_speed: f32,
    /// Angular acceleration.
    pub accel: f32,
    /// Feed-forward angular velocity (the target's own angular rate; 0 when returning to rest).
    pub feed_forward: f32,
    /// Jump straight to the target.
    pub snap: bool,
}

/// The two game clocks the step reads from the simulation thread's TLS block.
#[derive(Debug, Clone, Copy, Default)]
pub struct FrameClock {
    /// TLS+0x300: current game time.
    pub now: f64,
    /// TLS+0x328: game time of the previous frame; `now - previous` is the step's dt.
    pub previous: f64,
}

#[inline(always)]
fn max_ss(a: f32, b: f32) -> f32 {
    // x86 maxss: the first operand if it is greater, else the second (also for NaN).
    if a > b {
        a
    } else {
        b
    }
}

#[inline(always)]
fn min_ss(a: f32, b: f32) -> f32 {
    // x86 minss: the first operand if it is less, else the second (also for NaN).
    if a < b {
        a
    } else {
        b
    }
}

/// Wrap-around normalisation of a continuous joint's target: the angle congruent to
/// `target` (mod 2*pi) that lies within (about) pi of `current`.
#[inline]
pub fn wrap_target(target: f32, current: f32) -> f32 {
    let mut a = target % TWO_PI;
    if !(a >= 0.0) {
        a += TWO_PI;
    }
    let mut b = ((a - current) + PI) % TWO_PI;
    if !(b >= 0.0) {
        b += TWO_PI;
    }
    (b + current) - PI
}

/// One rotate-toward step. Returns the original's result: `true` when the joint reached
/// the target (or a limit) or snapped, `false` while it is still moving or when no game
/// time passed since the joint's last update.
pub fn rotate_toward(joint: &mut JointState, cmd: &JointCommand, clock: FrameClock) -> bool {
    let target = if joint.wrap { wrap_target(cmd.target, joint.value) } else { cmd.target };
    joint.target = target;
    if cmd.snap || !((target - joint.value).abs() >= EPSILON) {
        // Snap: value := target at the joint's own time; keep the feed-forward rate unless
        // the target sits on a limit.
        joint.value = joint.target;
        let mut velocity = 0.0;
        if (joint.target - cmd.min).abs() >= EPSILON && (joint.target - cmd.max).abs() >= EPSILON {
            velocity = cmd.feed_forward;
        }
        joint.velocity = velocity;
        return true;
    }
    if !(clock.now > joint.time) {
        return false;
    }
    let goal = joint.target;
    let current = joint.value;
    let diff = goal - current;
    let accel2 = cmd.accel + cmd.accel;
    let mut braking = (diff.abs() * accel2).sqrt() * BRAKE_MARGIN;
    if !(diff >= 0.0) {
        braking = -braking;
    }
    let velocity = joint.velocity;
    let dt = (clock.now - clock.previous) as f32;
    let accel_dt = cmd.accel * dt;
    let wanted = dt * cmd.feed_forward + braking;
    let capped = if wanted > cmd.max_speed { cmd.max_speed } else { wanted };
    let limit = max_ss(capped, -cmd.max_speed);
    let new_velocity = if !((velocity - limit).abs() >= EPSILON) {
        limit
    } else if limit > velocity {
        min_ss(accel_dt + velocity, limit)
    } else {
        max_ss(velocity - accel_dt, limit)
    };
    joint.velocity = new_velocity;
    let step = new_velocity * dt;
    let mut reached = false;
    let mut position = step + current;
    if new_velocity < 0.0 || new_velocity > 0.0 {
        // Arrival: only when the (signed) step overshoots and the speed is settled.
        if !((position - goal).abs() >= step) && !((new_velocity - limit).abs() >= accel_dt) {
            position = goal;
            joint.velocity = limit;
            reached = true;
        }
    }
    if cmd.min.abs() >= EPSILON || cmd.max.abs() >= EPSILON {
        let below = if position > cmd.max { cmd.max } else { position };
        position = max_ss(below, cmd.min);
    }
    if !((position - cmd.min).abs() >= EPSILON) || !((position - cmd.max).abs() >= EPSILON) {
        joint.velocity = 0.0;
        reached = true;
    }
    joint.value = position;
    joint.time = clock.now;
    reached
}

#[cfg(test)]
mod tests {
    use super::*;

    fn joint(value: f32, velocity: f32) -> JointState {
        JointState { value, time: 10.0, velocity, target: 0.0, wrap: false }
    }

    fn cmd(target: f32) -> JointCommand {
        JointCommand { target, min: -1.0, max: 1.0, max_speed: 2.0, accel: 4.0, feed_forward: 0.0, snap: false }
    }

    const CLOCK: FrameClock = FrameClock { now: 10.02, previous: 10.0 };

    #[test]
    fn constants_have_the_binary_bit_patterns() {
        assert_eq!(TWO_PI, std::f32::consts::TAU);
        assert_eq!(PI, std::f32::consts::PI);
        assert_eq!(EPSILON, 1e-4f32);
        assert_eq!(BRAKE_MARGIN, 0.9f32);
    }

    #[test]
    fn accelerates_toward_target() {
        let mut j = joint(0.0, 0.0);
        assert!(!rotate_toward(&mut j, &cmd(0.5), CLOCK));
        let dt = (CLOCK.now - CLOCK.previous) as f32;
        assert_eq!(j.velocity, 4.0 * dt);
        assert_eq!(j.value, j.velocity * dt + 0.0);
        assert_eq!(j.time, CLOCK.now);
        assert_eq!(j.target, 0.5);
    }

    #[test]
    fn no_time_passed_changes_only_the_target() {
        let mut j = joint(0.2, 0.3);
        j.time = CLOCK.now;
        assert!(!rotate_toward(&mut j, &cmd(0.5), CLOCK));
        assert_eq!((j.value, j.velocity, j.target, j.time), (0.2, 0.3, 0.5, CLOCK.now));
    }

    #[test]
    fn snap_keeps_time_and_takes_feed_forward() {
        let mut j = joint(0.2, 0.3);
        let mut c = cmd(0.7);
        c.snap = true;
        c.feed_forward = 0.25;
        assert!(rotate_toward(&mut j, &c, CLOCK));
        assert_eq!((j.value, j.velocity, j.time), (0.7, 0.25, 10.0));
        c.target = 1.0; // on the upper limit: no feed-forward
        assert!(rotate_toward(&mut j, &c, CLOCK));
        assert_eq!(j.velocity, 0.0);
    }

    #[test]
    fn tiny_difference_snaps() {
        let mut j = joint(0.5, 1.0);
        assert!(rotate_toward(&mut j, &cmd(0.50005), CLOCK));
        assert_eq!(j.value, 0.50005);
        assert_eq!(j.velocity, 0.0);
    }

    #[test]
    fn clamps_to_limits_and_stops() {
        let mut j = joint(0.99, 2.0);
        let mut c = cmd(3.0);
        c.max_speed = 10.0;
        assert!(rotate_toward(&mut j, &c, CLOCK));
        assert_eq!(j.value, 1.0);
        assert_eq!(j.velocity, 0.0);
    }

    #[test]
    fn wrap_takes_the_short_way() {
        let t = wrap_target(-3.0, 3.0);
        assert!((t - (TWO_PI - 3.0)).abs() < 1e-5, "{t}");
        let t = wrap_target(0.25, 0.0);
        assert!((t - 0.25).abs() < 1e-6, "{t}");
    }
}
