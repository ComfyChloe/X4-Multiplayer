//! Fixed-point quantisation of motion fields.
//!
//! All motion values travel as integers with a fixed scale, so equal states encode to
//! equal bytes (delta/XOR friendly) and resolution does not depend on the distance from
//! the sector origin (unlike `f32`).
//!
//! | Field | Wire | Unit | Resolution | Range |
//! |---|---|---|---|---|
//! | position (sector coordinates) | 3 x i32 | 1/32 m | 3.1 cm | +-50,000 km (v1 bound) |
//! | orientation (yaw, pitch, roll) | 3 x i16 | 2*pi/65536 rad | 0.0055 deg | full circle, wraps |
//! | linear velocity | 3 x i32 | 1/256 m/s | 3.9 mm/s | +-1,000 km/s |
//! | angular velocity | 3 x i16 | 1/1024 rad/s | 0.056 deg/s | +-31 rad/s |
//!
//! Quantising rounds to nearest; the error is at most half a step. Out-of-range or
//! non-finite inputs are an error, never silently clamped.

use std::f64::consts::PI;

pub const POSITION_SCALE: f64 = 32.0;
pub const POSITION_LIMIT_M: f64 = 50_000_000.0;
pub const ANGLE_SCALE: f64 = 32768.0 / PI;
/// Angles beyond this magnitude are rejected rather than wrapped (they are corrupt).
pub const ANGLE_INPUT_LIMIT: f64 = 1.0e6;
pub const VELOCITY_SCALE: f64 = 256.0;
pub const VELOCITY_LIMIT_MPS: f64 = 1_000_000.0;
pub const ANGULAR_SCALE: f64 = 1024.0;
pub const ANGULAR_LIMIT_RPS: f64 = 31.0;

/// A value that cannot be quantised.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QuantError(pub &'static str);

fn fixed(value: f64, scale: f64, limit: f64, what: &'static str) -> Result<i64, QuantError> {
    if !value.is_finite() || value.abs() > limit {
        return Err(QuantError(what));
    }
    Ok((value * scale).round() as i64)
}

/// Metres in sector coordinates to 1/32 m.
pub fn position(m: [f64; 3]) -> Result<[i32; 3], QuantError> {
    let mut out = [0i32; 3];
    for (o, v) in out.iter_mut().zip(m) {
        *o = fixed(v, POSITION_SCALE, POSITION_LIMIT_M, "position")? as i32;
    }
    Ok(out)
}

pub fn position_m(q: [i32; 3]) -> [f64; 3] {
    q.map(|v| f64::from(v) / POSITION_SCALE)
}

/// Radians (any finite value up to [`ANGLE_INPUT_LIMIT`]) to a wrapped i16.
pub fn angles(rad: [f64; 3]) -> Result<[i16; 3], QuantError> {
    let mut out = [0i16; 3];
    for (o, v) in out.iter_mut().zip(rad) {
        let q = fixed(v, ANGLE_SCALE, ANGLE_INPUT_LIMIT, "angle")?;
        // Two's-complement wrap onto the circle: -pi..pi.
        *o = q as i16;
    }
    Ok(out)
}

/// Wrapped angles in `[-pi, pi)`.
pub fn angles_rad(q: [i16; 3]) -> [f64; 3] {
    q.map(|v| f64::from(v) / ANGLE_SCALE)
}

pub fn velocity(mps: [f64; 3]) -> Result<[i32; 3], QuantError> {
    let mut out = [0i32; 3];
    for (o, v) in out.iter_mut().zip(mps) {
        *o = fixed(v, VELOCITY_SCALE, VELOCITY_LIMIT_MPS, "velocity")? as i32;
    }
    Ok(out)
}

pub fn velocity_mps(q: [i32; 3]) -> [f64; 3] {
    q.map(|v| f64::from(v) / VELOCITY_SCALE)
}

pub fn angular(rps: [f64; 3]) -> Result<[i16; 3], QuantError> {
    let mut out = [0i16; 3];
    for (o, v) in out.iter_mut().zip(rps) {
        *o = fixed(v, ANGULAR_SCALE, ANGULAR_LIMIT_RPS, "angular velocity")? as i16;
    }
    Ok(out)
}

pub fn angular_rps(q: [i16; 3]) -> [f64; 3] {
    q.map(|v| f64::from(v) / ANGULAR_SCALE)
}

/// Speed (m/s) of a quantised velocity.
pub fn speed_mps(q: [i32; 3]) -> f64 {
    let v = velocity_mps(q);
    (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_and_rounding() {
        assert_eq!(position([1.0, -0.015, 0.016]).unwrap(), [32, 0, 1]);
        assert!(position([POSITION_LIMIT_M, 0.0, 0.0]).is_ok());
        assert!(position([POSITION_LIMIT_M + 1.0, 0.0, 0.0]).is_err());
        assert!(position([f64::NAN, 0.0, 0.0]).is_err());
        assert!(velocity([f64::INFINITY, 0.0, 0.0]).is_err());
        assert!(angular([32.0, 0.0, 0.0]).is_err());
        assert!(angular([31.0, -31.0, 0.0]).is_ok());
    }

    #[test]
    fn angles_wrap_onto_the_circle() {
        let q = angles([PI, -PI, 3.0 * PI]).unwrap();
        assert_eq!(q, [i16::MIN, i16::MIN, i16::MIN]);
        let back = angles_rad(angles([0.5, -0.5, 2.0 * PI + 0.25]).unwrap());
        assert!((back[0] - 0.5).abs() < 1e-4 && (back[1] + 0.5).abs() < 1e-4 && (back[2] - 0.25).abs() < 1e-4);
        assert!(angles([1.0e7, 0.0, 0.0]).is_err());
    }
}
