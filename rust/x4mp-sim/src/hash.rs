//! Stable hashing: FNV-1a 64 over an explicit little-endian byte encoding.
//!
//! Used for world state hashes (thread-count invariance tests), capture record checksums and
//! seed derivation. `std`'s `Hash` is not used for anything observable: its output is not
//! specified to be stable across Rust versions or platforms.

pub const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
pub const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

/// FNV-1a 64-bit hasher.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Fnv64(u64);

impl Default for Fnv64 {
    fn default() -> Self {
        Fnv64(FNV_OFFSET)
    }
}

impl Fnv64 {
    pub fn new() -> Self {
        Self::default()
    }

    #[inline]
    pub fn write(&mut self, bytes: &[u8]) {
        let mut h = self.0;
        for b in bytes {
            h = (h ^ u64::from(*b)).wrapping_mul(FNV_PRIME);
        }
        self.0 = h;
    }

    #[inline]
    pub fn write_u8(&mut self, v: u8) {
        self.write(&[v]);
    }
    #[inline]
    pub fn write_u16(&mut self, v: u16) {
        self.write(&v.to_le_bytes());
    }
    #[inline]
    pub fn write_u32(&mut self, v: u32) {
        self.write(&v.to_le_bytes());
    }
    #[inline]
    pub fn write_u64(&mut self, v: u64) {
        self.write(&v.to_le_bytes());
    }

    pub fn finish(&self) -> u64 {
        self.0
    }
}

/// FNV-1a 64 of a byte string.
pub fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut h = Fnv64::new();
    h.write(bytes);
    h.finish()
}

/// Deterministic, platform-independent hashing of a value's exact state.
///
/// Floats hash their bit patterns (so `-0.0 != 0.0` and NaN payloads count): two values with
/// equal hashes are, with overwhelming probability, bit-identical.
pub trait StableHash {
    fn stable_hash(&self, h: &mut Fnv64);
}

macro_rules! int_hash {
    ($($t:ty),*) => {$(
        impl StableHash for $t {
            #[inline]
            fn stable_hash(&self, h: &mut Fnv64) {
                h.write(&self.to_le_bytes());
            }
        }
    )*};
}
int_hash!(u8, u16, u32, u64, i8, i16, i32, i64);

impl StableHash for bool {
    #[inline]
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u8(u8::from(*self));
    }
}

impl StableHash for f32 {
    #[inline]
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u32(self.to_bits());
    }
}

impl StableHash for f64 {
    #[inline]
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u64(self.to_bits());
    }
}

impl<T: StableHash, const N: usize> StableHash for [T; N] {
    fn stable_hash(&self, h: &mut Fnv64) {
        for v in self {
            v.stable_hash(h);
        }
    }
}

impl<T: StableHash> StableHash for Option<T> {
    fn stable_hash(&self, h: &mut Fnv64) {
        match self {
            None => h.write_u8(0),
            Some(v) => {
                h.write_u8(1);
                v.stable_hash(h);
            }
        }
    }
}

impl<T: StableHash> StableHash for Vec<T> {
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u64(self.len() as u64);
        for v in self {
            v.stable_hash(h);
        }
    }
}

impl StableHash for String {
    fn stable_hash(&self, h: &mut Fnv64) {
        h.write_u64(self.len() as u64);
        h.write(self.as_bytes());
    }
}

impl<A: StableHash, B: StableHash> StableHash for (A, B) {
    fn stable_hash(&self, h: &mut Fnv64) {
        self.0.stable_hash(h);
        self.1.stable_hash(h);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnv_reference_vectors() {
        // Published FNV-1a 64 test vectors.
        assert_eq!(fnv1a64(b""), 0xcbf2_9ce4_8422_2325);
        assert_eq!(fnv1a64(b"a"), 0xaf63_dc4c_8601_ec8c);
        assert_eq!(fnv1a64(b"foobar"), 0x8594_4171_f739_67e8);
    }

    #[test]
    fn floats_hash_bits() {
        let hash = |v: f32| {
            let mut h = Fnv64::new();
            v.stable_hash(&mut h);
            h.finish()
        };
        assert_ne!(hash(0.0), hash(-0.0));
        assert_eq!(hash(1.5), hash(1.5));
    }
}
