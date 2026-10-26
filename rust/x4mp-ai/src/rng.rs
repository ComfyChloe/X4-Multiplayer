//! Engine random numbers, reproduced bit-exactly.
//!
//! Target: X4 9.00, Steam build 23660954, Windows `X4.exe` (SHA-256 19750A65...6891). Every function
//! below names the engine routine it reproduces. Semantics come from disassembly (2026-10-26, this
//! crate's author, `x4-re/scripts/rw-ai-orders/dis.py`); no engine code is copied.
//!
//! The engine has one generator, a 64-bit LCG with PCG constants whose *rotated* value becomes the new
//! state. The script interpreter draws from the executing thread's stream (TLS block + 0x318, the
//! MoveWorker stream for AI and MD). The rewrite gives every entity its own stream instead
//! ([`stream_seed`]); in replay mode the stream is reset to the captured TLS state at the start of each
//! captured run, so the values are the original ones as long as our draw *order and count* match.
//!
//! [`EngineLcg`] mirrors `x4mp_sim::rng::EngineLcg` (ecs-core, rewrite-coord/interfaces.md v0) and is
//! kept API-compatible so it can become a re-export once x4mp-sim is on the base branch.

/// LCG multiplier (immediate in 0x14ddb30 and 117 inlined copies).
pub const MULTIPLIER: u64 = 0x5851_F42D_4C95_7F2D;
/// LCG increment.
pub const INCREMENT: u64 = 0x1405_7B7E_F767_814F;

/// `2^-32` as f32 (bits 0x2f800000), the constant at RVA 0x2cbe008 used by `Random::FloatState` 0x14ddc90.
pub const FLOAT_SCALE: f32 = 1.0 / 4294967296.0;
/// `100 * 2^-32` as f32 (= 25 * 2^-30, exact; bits 0x32c80000), the constant at RVA 0x2cbe040 used by the
/// interpreter's `chance` roll in `Scripts::ExecState_Run` 0xb58bb0.
pub const CHANCE_SCALE: f32 = 25.0 / 1073741824.0;

/// One engine LCG step: `ror64(state * MULTIPLIER + INCREMENT, 30)`. The rotated value is stored as the
/// new state (so this is not textbook PCG). The step is a bijection on u64.
#[inline]
pub const fn step(state: u64) -> u64 {
    state.wrapping_mul(MULTIPLIER).wrapping_add(INCREMENT).rotate_right(30)
}

/// Inverse of [`step`] (for tests and for walking captured states backwards).
pub const fn unstep(state: u64) -> u64 {
    // MULTIPLIER is odd; its inverse mod 2^64 by Newton iteration (each round doubles the valid bits).
    let mut inv: u64 = MULTIPLIER;
    let mut i = 0;
    while i < 6 {
        inv = inv.wrapping_mul(2u64.wrapping_sub(MULTIPLIER.wrapping_mul(inv)));
        i += 1;
    }
    state.rotate_left(30).wrapping_sub(INCREMENT).wrapping_mul(inv)
}

/// Number of steps from `from` to `to`, if it is at most `cap` (used to verify captured draw counts).
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

/// The engine generator over an explicit state (API of `x4mp_sim::rng::EngineLcg`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct EngineLcg(pub u64);

impl EngineLcg {
    /// One step; returns the new state.
    #[inline]
    pub fn next_state(&mut self) -> u64 {
        self.0 = step(self.0);
        self.0
    }

    /// `Random::U64State` 0x14ddb30 (TLS variant `Random::U64` 0x14dde20): `n == 0` logs an error and
    /// returns 0 **without a step**; otherwise one step and `state % n`.
    #[inline]
    pub fn u64_below(&mut self, n: u64) -> u64 {
        if n == 0 {
            return 0;
        }
        self.next_state() % n
    }

    /// `Random::FloatState` 0x14ddc90 (TLS variant `Random::Float` 0x14ddfe0). `!(max >= 0)` (negative
    /// or NaN) logs and returns 0.0 **without a step**. Otherwise one step and
    /// `(f32(low32(state)) * 2^-32f) * max`, all in f32 (cvtsi2ss of the zero-extended low 32 bits, two
    /// mulss). The result lies in `[0, max]`: low32 values >= 2^32 - 128 round to 2^32 and give `max`.
    #[inline]
    #[allow(clippy::neg_cmp_op_on_partial_ord)] // NaN must take the error path, as comiss/jae does
    pub fn float(&mut self, max: f32) -> f32 {
        if !(max >= 0.0) {
            return 0.0;
        }
        let low = self.next_state() as u32;
        (low as f32) * FLOAT_SCALE * max
    }
}

/// Random number profile of the script `random` attribute group (`profile=`), as the distribution id
/// passed to `Random::WithDistribution` 0x14ddd10. Ids 2/3/5 are from the rng report (medium); 4 (the
/// plain average of `scale` samples) and 1 (flat) are read from the fold in 0x14ddd10.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Profile {
    Flat = 1,
    Increasing = 2,
    Decreasing = 3,
    Bell = 4,
    InverseBell = 5,
}

/// Script value types that matter for random ranges: the engine computes `max - min` in the type of
/// `max` and converts the result back to it (0xc58da0, tags 4..6 float, 7..9 double, 1..3 integer).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum NumKind {
    /// f32-valued script numbers (lengths, plain floats).
    F32,
    /// f64-valued script numbers (times; `<wait>` asks for value tag 8).
    F64,
}

/// A per-entity engine stream with a draw counter.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Stream {
    lcg: EngineLcg,
    draws: u64,
}

impl Stream {
    pub const fn from_state(state: u64) -> Stream {
        Stream { lcg: EngineLcg(state), draws: 0 }
    }

    /// Current state (what the engine would hold at TLS+0x318).
    pub const fn state(&self) -> u64 {
        self.lcg.0
    }

    /// Draws made since construction or the last [`Stream::reset_to`].
    pub const fn draws(&self) -> u64 {
        self.draws
    }

    /// Replay mode: continue from a captured state (the counter restarts at 0).
    pub fn reset_to(&mut self, state: u64) {
        self.lcg = EngineLcg(state);
        self.draws = 0;
    }

    /// Advance by `n` raw steps whose values the behaviour does not use (draws made inside engine
    /// services such as `get_safe_pos`, whose count comes from the capture).
    pub fn skip(&mut self, n: u64) {
        for _ in 0..n {
            self.raw();
        }
    }

    #[inline]
    fn raw(&mut self) -> u64 {
        self.draws += 1;
        self.lcg.next_state()
    }

    /// See [`EngineLcg::u64_below`].
    pub fn u64_below(&mut self, n: u64) -> u64 {
        if n == 0 {
            return 0;
        }
        self.raw() % n
    }

    /// See [`EngineLcg::float`].
    #[allow(clippy::neg_cmp_op_on_partial_ord)] // NaN semantics of comiss
    pub fn float(&mut self, max: f32) -> f32 {
        if !(max >= 0.0) {
            return 0.0;
        }
        let low = self.raw() as u32;
        (low as f32) * FLOAT_SCALE * max
    }

    /// The interpreter's `chance=` roll (`Scripts::ExecState_Run` 0xb58bb0, also used for chance on
    /// conditions): the chance expression is evaluated to f32 first, then **one step is always made**
    /// (also for chance 0 and 100): `x = f32(low32(state)) * (100 * 2^-32f)`; the action runs iff
    /// `!(x >= chance)` (comiss + jb, so a NaN chance runs it). A failed roll jumps to the action's
    /// skip index, which makes a failed `<do_if chance>` behave like a false condition.
    #[allow(clippy::neg_cmp_op_on_partial_ord)] // NaN semantics of comiss/jb
    pub fn chance(&mut self, chance: f32) -> bool {
        let low = self.raw() as u32;
        let x = (low as f32) * CHANCE_SCALE;
        !(x >= chance)
    }

    /// `Random::WithDistribution` 0x14ddd10 `(state, max, dist, samples)`: one `float(max)` draw; if
    /// `samples >= 2` and `dist != 1`, `samples - 1` more draws are summed (f32 adds), divided by
    /// `samples as f32`, and folded around `half = max * 0.5`:
    /// 2: `r >= half ? max - r : r`, then `2r`; 3: same reflection, then `max - 2r`; 4: `r`;
    /// 5: `r >= half ? r - half : r + half`; any other id logs and returns `r`.
    pub fn with_distribution(&mut self, max: f32, dist: i32, samples: u32) -> f32 {
        let mut r = self.float(max);
        if samples < 2 || dist == 1 {
            return r;
        }
        for _ in 1..samples {
            r += self.float(max);
        }
        r /= samples as f32;
        let half = max * 0.5;
        match dist {
            2 => {
                if r >= half {
                    r = max - r;
                }
                r + r
            }
            3 => {
                if r >= half {
                    r = max - r;
                }
                max - (r + r)
            }
            5 => {
                if r >= half {
                    r - half
                } else {
                    r + half
                }
            }
            _ => r,
        }
    }

    /// Script random range `min=.. max=..` with an optional profile (`maybe_Scripts::EvaluateRandomRange`
    /// 0xc58da0, range kind 2), for floating-point values:
    /// `diff = max - min` in the type of `max` (f32 or f64 arithmetic); `d = f64(diff)`;
    /// if `!(d > 0)` the result is `min` **without a draw**; otherwise
    /// `r = WithDistribution(f32(d), profile, scale)` and the result is `f64(min) + f64(r)`, converted to
    /// the type of `max` (f32 rounding for lengths). Integer ranges (d + 1, truncation) are not needed
    /// by the ported scripts yet and are deliberately not offered.
    #[allow(clippy::neg_cmp_op_on_partial_ord)] // NaN semantics of comisd/ja
    pub fn range(&mut self, kind: NumKind, min: f64, max: f64, profile: Option<(Profile, u32)>) -> f64 {
        let d = match kind {
            NumKind::F32 => ((max as f32) - (min as f32)) as f64,
            NumKind::F64 => max - min,
        };
        if !(d > 0.0) {
            return min;
        }
        let (dist, samples) = match profile {
            None => (Profile::Flat as i32, 1),
            Some((p, s)) => (p as i32, s),
        };
        let r = self.with_distribution(d as f32, dist, samples);
        let v = min + (r as f64);
        match kind {
            NumKind::F32 => (v as f32) as f64,
            NumKind::F64 => v,
        }
    }
}

/// Seed of an entity's script stream in authoritative (non-replay) mode: FNV-1a 64 over the
/// little-endian bytes of `session_seed`, `uid` and `purpose`, then one LCG step. Placeholder with the
/// same inputs as `x4mp_sim::rng::stream_seed`; replaced by that function when x4mp-sim lands.
pub fn stream_seed(session_seed: u64, uid: u64, purpose: u32) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    let mut eat = |b: u8| {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    };
    for b in session_seed.to_le_bytes() {
        eat(b);
    }
    for b in uid.to_le_bytes() {
        eat(b);
    }
    for b in purpose.to_le_bytes() {
        eat(b);
    }
    step(h)
}

/// Purpose key of the AI script stream (`'AISC'`).
pub const PURPOSE_AI_SCRIPT: u32 = 0x4149_5343;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constants_have_the_engine_bits() {
        assert_eq!(FLOAT_SCALE.to_bits(), 0x2F80_0000);
        assert_eq!(CHANCE_SCALE.to_bits(), 0x32C8_0000);
        assert_eq!(CHANCE_SCALE, 100.0 / 4294967296.0);
    }

    #[test]
    fn unstep_inverts_step() {
        for s in [0u64, 1, 0xf3da54e488294596, u64::MAX, 0x1234_5678_9abc_def0] {
            assert_eq!(unstep(step(s)), s);
            assert_eq!(step(unstep(s)), s);
        }
    }

    #[test]
    fn step_matches_native_evidence() {
        // Native evidence (contrib/ferrite-rng-streams, Stage 1 runs C and D): the MoveWorker stream
        // seeded with 0xf3da54e488294596 was logged as 0x28acd4a7fac17879 exactly 150 draws later.
        assert_eq!(steps_between(0xf3da54e488294596, 0x28acd4a7fac17879, 1000), Some(150));
    }

    #[test]
    fn float_and_u64_edge_cases_do_not_step() {
        let mut s = Stream::from_state(1);
        assert_eq!(s.float(-1.0), 0.0);
        assert_eq!(s.float(f32::NAN), 0.0);
        assert_eq!(s.u64_below(0), 0);
        assert_eq!(s.draws(), 0);
        // max == 0 still steps (comiss 0 >= 0 passes).
        assert_eq!(s.float(0.0), 0.0);
        assert_eq!(s.draws(), 1);
    }

    #[test]
    fn float_can_return_max_itself() {
        // A state whose next low32 is >= 2^32 - 128 rounds to 2^32 in f32, so FloatState returns max.
        let st = unstep(0x0123_4567_FFFF_FFC0);
        let mut s = Stream::from_state(st);
        assert_eq!(s.float(2.0), 2.0);
        // ...and the chance roll then yields exactly 100.0: chance 100 fails.
        let mut c = Stream::from_state(st);
        assert!(!c.chance(100.0));
        // 2^32 - 256 is representable and stays below max
        let mut d = Stream::from_state(unstep(0x0123_4567_FFFF_FF00));
        assert!(d.float(2.0) < 2.0);
    }

    #[test]
    fn chance_always_steps_and_compares_like_comiss() {
        let mut s = Stream::from_state(0x1234_5678_9abc_def0);
        assert!(!s.chance(0.0)); // x >= 0 always: never runs, but steps
        assert_eq!(s.draws(), 1);
        assert!(s.chance(f32::NAN)); // unordered: runs
        assert_eq!(s.draws(), 2);
        // chance 30: reproduce by hand
        let before = s.state();
        let x = ((step(before) as u32) as f32) * CHANCE_SCALE;
        assert_eq!(s.chance(30.0), x < 30.0);
    }

    #[test]
    fn range_without_width_does_not_draw() {
        let mut s = Stream::from_state(7);
        assert_eq!(s.range(NumKind::F64, 2.0, 2.0, None), 2.0);
        assert_eq!(s.range(NumKind::F64, 3.0, 2.0, None), 3.0);
        assert_eq!(s.draws(), 0);
    }

    #[test]
    fn range_flat_is_one_float_draw() {
        let mut a = Stream::from_state(99);
        let mut b = Stream::from_state(99);
        let v = a.range(NumKind::F64, 1.0, 2.0, None);
        let r = b.float(1.0);
        assert_eq!(v, 1.0 + r as f64);
        assert_eq!(a.draws(), 1);
        // f32 kind: rounding back to f32
        let mut c = Stream::from_state(5);
        let mut d = Stream::from_state(5);
        let y = c.range(NumKind::F32, -2000.0, 2000.0, None);
        let r = d.float(4000.0);
        assert_eq!(y, ((-2000.0f64 + r as f64) as f32) as f64);
    }

    #[test]
    fn with_distribution_folds() {
        // dist 4 with 2 samples is the f32 average of two float draws
        let mut a = Stream::from_state(42);
        let mut b = Stream::from_state(42);
        let v = a.with_distribution(10.0, 4, 2);
        let r1 = b.float(10.0);
        let r2 = b.float(10.0);
        assert_eq!(v, (r1 + r2) / 2.0);
        assert_eq!(a.draws(), 2);
        // flat ignores samples
        let mut c = Stream::from_state(42);
        let _ = c.with_distribution(10.0, 1, 5);
        assert_eq!(c.draws(), 1);
    }

    #[test]
    fn stream_seed_is_stable_and_distinct() {
        let a = stream_seed(123456789, 0x1001, PURPOSE_AI_SCRIPT);
        assert_eq!(a, stream_seed(123456789, 0x1001, PURPOSE_AI_SCRIPT));
        assert_ne!(a, stream_seed(123456789, 0x1002, PURPOSE_AI_SCRIPT));
        assert_ne!(a, stream_seed(123456789, 0x1001, 0));
    }
}
