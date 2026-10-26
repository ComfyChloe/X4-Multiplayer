//! Engine game-time conventions.
//!
//! Game time is an `f64` in seconds (TLS+0x300 on the engine thread, `GetCurrentGameTime`).
//! The economy code marks "no time" with `-1.0` (`0xbff0000000000000`) and tests a stamp
//! against the constant `-0.9999` (RVA 0x2cbf088) with `comisd`. For every finite value the
//! sites agree: a stamp is set when it is greater than `-0.9999`. They differ only for NaN
//! (some sites branch on `jb`, some on `jbe`), which the engine never stores in these
//! fields; captures containing a NaN time are rejected by the replay instead of guessed.

/// The engine's "unset" time value.
pub const UNSET: f64 = -1.0;

/// The engine's threshold constant at RVA 0x2cbf088.
pub const UNSET_THRESHOLD: f64 = -0.9999;

/// `true` when the finite stamp `t` holds a real time (engine: `t > -0.9999`).
#[inline]
pub fn is_set(t: f64) -> bool {
    t > UNSET_THRESHOLD
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sentinel_semantics() {
        assert!(!is_set(UNSET));
        assert!(!is_set(-0.9999));
        assert!(is_set(-0.99989));
        assert!(is_set(0.0));
        assert_eq!(UNSET.to_bits(), 0xbff0_0000_0000_0000);
        assert_eq!(UNSET_THRESHOLD.to_bits(), 0xbfef_ff2e_48e8_a71e);
    }
}
