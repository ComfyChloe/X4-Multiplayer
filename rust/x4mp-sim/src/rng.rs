//! Per-entity random streams with the engine's exact generator.
//!
//! X4 uses one generator everywhere (`x4-re/coord/reports/rng.md`): a u64 state stepped as
//! `s' = ror64(s * 0x5851F42D4C957F2D + 0x14057B7EF767814F, 30)`; the rotated value is the new
//! state. The library functions were re-verified by disassembly for this crate (Windows build
//! 23660954, 2026-10-26):
//!
//! | engine function | rva | behaviour reproduced by |
//! |---|---|---|
//! | `Random::U64State(state*, n)` | 0x14ddb30 | [`EngineLcg::u64_below`]: n == 0 -> 0 without a step; else `state % n` |
//! | `Random::I64State(state*, n)` | 0x14ddb90 | [`EngineLcg::i64_below`]: n <= 0 -> 0 without a step; else `state % n` (unsigned) |
//! | `Random::U32State(state*, n)` | 0x14ddbf0 | [`EngineLcg::u32_below`]: n == 0 -> 0 without a step |
//! | `Random::I32State(state*, n)` | 0x14ddc40 | [`EngineLcg::i32_below`]: n <= 0 -> 0 without a step |
//! | `Random::FloatState(state*, max)` | 0x14ddc90 | [`EngineLcg::float`]: `!(max >= 0)` (negative or NaN) -> 0.0 without a step; else `((low32 as f32) * 2^-32) * max` in f32 (`cvtsi2ss` of the zero-extended low 32 bits, two `mulss`) |
//!
//! Because `low32 as f32` rounds to nearest, values >= 2^32 - 128 round to 2^32 and
//! `float(max)` can return exactly `max`: the engine's range is `[0, max]`, not `[0, max)`.
//!
//! The engine draws from per-thread streams (TLS+0x318), which a parallel rewrite cannot
//! reproduce. Replacements use **per-entity streams** stored in the [`crate::comp::RngState`]
//! column, seeded from the session seed and the entity's UniverseId; draws are then independent
//! of thread count and scheduling. Seeds use FNV-1a exactly like the role seeds of
//! `contrib/ferrite-rng-streams` (`role_seed`).

use crate::hash::{FNV_OFFSET, FNV_PRIME};
use crate::id::UniverseId;

pub const MULTIPLIER: u64 = 0x5851_f42d_4c95_7f2d;
pub const INCREMENT: u64 = 0x1405_7b7e_f767_814f;
/// 2^-32 as stored at RVA 0x2cbe008 (`0x2f800000`).
pub const FLOAT_SCALE: f32 = 2.328_306_4e-10;

/// One engine generator step.
#[inline]
pub fn step(state: u64) -> u64 {
    state.wrapping_mul(MULTIPLIER).wrapping_add(INCREMENT).rotate_right(30)
}

/// A stream of engine-generator states.
pub trait Rng {
    /// Advances one step and returns the new state.
    fn next_state(&mut self) -> u64;
}

/// The engine's generator over an explicit state (like the `Random::*State` functions).
#[repr(transparent)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct EngineLcg(pub u64);

impl Rng for EngineLcg {
    #[inline]
    fn next_state(&mut self) -> u64 {
        self.0 = step(self.0);
        self.0
    }
}

impl EngineLcg {
    pub fn new(state: u64) -> EngineLcg {
        EngineLcg(state)
    }

    /// Views a raw state in place (e.g. a [`crate::comp::RngState`] column entry).
    pub fn from_state_mut(state: &mut u64) -> &mut EngineLcg {
        // SAFETY: EngineLcg is repr(transparent) over u64.
        unsafe { &mut *(state as *mut u64 as *mut EngineLcg) }
    }

    pub fn state(&self) -> u64 {
        self.0
    }

    /// `Random::U64State`.
    pub fn u64_below(&mut self, n: u64) -> u64 {
        if n == 0 {
            return 0;
        }
        self.next_state() % n
    }

    /// `Random::I64State` (n > 0 required; the division is unsigned).
    pub fn i64_below(&mut self, n: i64) -> i64 {
        if n <= 0 {
            return 0;
        }
        (self.next_state() % n as u64) as i64
    }

    /// `Random::U32State`.
    pub fn u32_below(&mut self, n: u32) -> u32 {
        if n == 0 {
            return 0;
        }
        (self.next_state() % u64::from(n)) as u32
    }

    /// `Random::I32State`.
    pub fn i32_below(&mut self, n: i32) -> i32 {
        if n <= 0 {
            return 0;
        }
        (self.next_state() % n as u64) as i32
    }

    /// `Random::FloatState`: `[0, max]` (see the module docs for the inclusive upper end).
    pub fn float(&mut self, max: f32) -> f32 {
        // comiss + jae: taken only for max >= 0 (not for NaN), hence the negated comparison.
        #[allow(clippy::neg_cmp_op_on_partial_ord)]
        if !(max >= 0.0) {
            return 0.0;
        }
        let low = self.next_state() as u32;
        (low as f32) * FLOAT_SCALE * max
    }
}

fn fnv(mut h: u64, bytes: &[u8]) -> u64 {
    for b in bytes {
        h = (h ^ u64::from(*b)).wrapping_mul(FNV_PRIME);
    }
    h
}

/// Seed of a named role: FNV-1a over the session seed (little endian) followed by the role
/// bytes. Identical to `role_seed` on `contrib/ferrite-rng-streams`.
pub fn role_seed(session_seed: u64, role: &[u8]) -> u64 {
    fnv(fnv(FNV_OFFSET, &session_seed.to_le_bytes()), role)
}

/// Seed of an entity's stream for one purpose: `role_seed(session, b"entity" ++ uid_le ++
/// purpose_le)`. Different purposes give independent streams for the same entity.
pub fn entity_seed(session_seed: u64, uid: UniverseId, purpose: u32) -> u64 {
    let mut role = [0u8; 18];
    role[..6].copy_from_slice(b"entity");
    role[6..14].copy_from_slice(&uid.0.to_le_bytes());
    role[14..].copy_from_slice(&purpose.to_le_bytes());
    role_seed(session_seed, &role)
}

/// Number of steps that turn `from` into `to`, `None` beyond `cap`. The step is a bijection,
/// so this recovers exact draw counts from two observed states.
pub fn steps_between(from: u64, to: u64, cap: u64) -> Option<u64> {
    let mut s = from;
    for n in 0..=cap {
        if s == to {
            return Some(n);
        }
        s = step(s);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn step_matches_engine_constants() {
        let s = 0x0123_4567_89ab_cdefu64;
        let expect = s.wrapping_mul(0x5851f42d4c957f2d).wrapping_add(0x14057b7ef767814f).rotate_right(30);
        assert_eq!(step(s), expect);
        assert_eq!(FLOAT_SCALE.to_bits(), 0x2f80_0000);
    }

    #[test]
    fn invalid_arguments_do_not_step() {
        let mut r = EngineLcg(42);
        assert_eq!(r.u64_below(0), 0);
        assert_eq!(r.i64_below(-3), 0);
        assert_eq!(r.u32_below(0), 0);
        assert_eq!(r.i32_below(0), 0);
        assert_eq!(r.float(-1.0), 0.0);
        assert_eq!(r.float(f32::NAN), 0.0);
        assert_eq!(r.state(), 42, "no draw consumed");
        assert!(r.float(0.0) == 0.0 && r.state() != 42, "max == 0 is valid and steps");
    }

    #[test]
    fn modulo_uses_full_state() {
        let mut a = EngineLcg(7);
        let s = step(7);
        assert_eq!(a.u64_below(1000), s % 1000);
        let mut b = EngineLcg(7);
        assert_eq!(b.i32_below(97), (s % 97) as i32);
    }

    #[test]
    fn float_rounding_can_reach_max() {
        // Find a state whose next low 32 bits are >= 2^32 - 128 by inverting nothing: just
        // check the conversion rule directly on the boundary value.
        let low = u32::MAX;
        assert_eq!((low as f32) * FLOAT_SCALE * 3.0, 3.0);
        let low = (1u32 << 24) + 1; // not representable: rounds to even
        assert_eq!((low as f32).to_bits(), ((1u32 << 24) as f32).to_bits());
    }

    #[test]
    fn role_seed_matches_rng_streams_vector() {
        // contrib/ferrite-rng-streams native/hooks/src/rng.rs test vector.
        assert_eq!(role_seed(123_456_789, b"MoveWorker"), 0xf3da_54e4_8829_4596);
        let a = entity_seed(1, UniverseId(5), 0);
        assert_ne!(a, entity_seed(1, UniverseId(5), 1));
        assert_ne!(a, entity_seed(1, UniverseId(6), 0));
        assert_ne!(a, entity_seed(2, UniverseId(5), 0));
    }

    #[test]
    fn steps_between_recovers_counts() {
        let mut r = EngineLcg(99);
        for _ in 0..1000 {
            r.next_state();
        }
        assert_eq!(steps_between(99, r.state(), 2000), Some(1000));
        assert_eq!(steps_between(99, r.state(), 10), None);
    }
}
