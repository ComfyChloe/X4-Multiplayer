//! Small deterministic generator for tests and synthetic captures (splitmix64; not the
//! engine's RNG, which is `Random::FloatState` 0x14ddc90 on a per-thread LCG state).

#[derive(Debug, Clone)]
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(seed ^ 0x9e37_79b9_7f4a_7c15)
    }

    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }

    /// Uniform in [0, 1).
    pub fn unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// Uniform f32 in [lo, hi).
    pub fn range(&mut self, lo: f32, hi: f32) -> f32 {
        (lo as f64 + (hi as f64 - lo as f64) * self.unit()) as f32
    }

    pub fn chance(&mut self, p: f64) -> bool {
        self.unit() < p
    }

    /// Any f32 bit pattern that is finite.
    pub fn finite_bits(&mut self) -> f32 {
        loop {
            let v = f32::from_bits(self.next_u64() as u32);
            if v.is_finite() {
                return v;
            }
        }
    }
}
