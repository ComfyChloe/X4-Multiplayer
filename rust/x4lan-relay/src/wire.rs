//! Validated wire vocabulary shared by every session layer.
//!
//! Each type can only be constructed through its validating parser, so a handler that
//! holds a `Hex32`, `Atom` or `Ship` never re-checks it and can never forward an
//! unchecked field. A failed parse is a [`Reject`]: the Python relay's silent drop.

use std::fmt;

/// Version-1 envelope limit (host.py `MAX_PACKET`).
pub const MAX_PACKET: usize = 768;
/// host.py `MAX_SEQ`.
pub const MAX_SEQ: u32 = 2_147_483_647;

/// A packet (or field) the relay drops without a reply.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Reject;

pub type Parsed<T> = Result<T, Reject>;

/// `[a-f0-9]{32}`: session token, nonce, lease, request ID.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Hex32([u8; 32]);

impl Hex32 {
    pub fn parse(text: &str) -> Parsed<Self> {
        let bytes = text.as_bytes();
        if bytes.len() != 32 || !bytes.iter().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
            return Err(Reject);
        }
        let mut out = [0u8; 32];
        out.copy_from_slice(bytes);
        Ok(Hex32(out))
    }

    pub fn from_bytes(random: [u8; 16]) -> Self {
        const DIGITS: &[u8; 16] = b"0123456789abcdef";
        let mut out = [0u8; 32];
        for (i, byte) in random.iter().enumerate() {
            out[2 * i] = DIGITS[usize::from(byte >> 4)];
            out[2 * i + 1] = DIGITS[usize::from(byte & 15)];
        }
        Hex32(out)
    }

    pub fn as_str(&self) -> &str {
        // Only ASCII hex digits are ever stored.
        std::str::from_utf8(&self.0).expect("ascii hex")
    }

    /// Constant-time comparison with untrusted text (`hmac.compare_digest`).
    pub fn matches(&self, text: &str) -> bool {
        let other = text.as_bytes();
        if other.len() != 32 {
            return false;
        }
        self.0.iter().zip(other).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
    }
}

impl fmt::Display for Hex32 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl fmt::Debug for Hex32 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Hex32({})", self.as_str())
    }
}

/// `[A-Za-z0-9_.-]{1,96}`: build label, bubble, sector, macro, owner.
#[derive(Clone, PartialEq, Eq, Hash, Debug, PartialOrd, Ord)]
pub struct Atom(String);

impl Atom {
    pub fn parse(text: &str) -> Parsed<Self> {
        let ok = (1..=96).contains(&text.len())
            && text.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'));
        if ok {
            Ok(Atom(text.to_owned()))
        } else {
            Err(Reject)
        }
    }

    /// An atom that names an X4 macro (`..._macro`), e.g. a sector.
    pub fn parse_macro(text: &str) -> Parsed<Self> {
        let atom = Self::parse(text)?;
        if atom.0.ends_with("_macro") {
            Ok(atom)
        } else {
            Err(Reject)
        }
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Atom {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// economy.py `WARE`: `[a-z][a-z0-9_]{0,63}`.
pub fn is_ware(text: &str) -> bool {
    let b = text.as_bytes();
    !b.is_empty()
        && b.len() <= 64
        && b[0].is_ascii_lowercase()
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
}

/// Authority p1, human guest p2, and a second human guest p3 in dedicated sessions.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, PartialOrd, Ord)]
pub enum Slot {
    P1,
    P2,
    P3,
}

impl Slot {
    pub fn parse(text: &str) -> Parsed<Self> {
        match text {
            "p1" => Ok(Slot::P1),
            "p2" => Ok(Slot::P2),
            "p3" => Ok(Slot::P3),
            _ => Err(Reject),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Slot::P1 => "p1",
            Slot::P2 => "p2",
            Slot::P3 => "p3",
        }
    }

    pub fn other(self) -> Self {
        match self {
            Slot::P1 => Slot::P2,
            Slot::P2 | Slot::P3 => Slot::P1,
        }
    }
}

/// One of the two stable co-op ships (`entity` 1/2 on the wire).
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, PartialOrd, Ord)]
pub enum Ship {
    One,
    Two,
}

impl Ship {
    pub const BOTH: [Ship; 2] = [Ship::One, Ship::Two];

    pub fn from_number(n: i128) -> Option<Self> {
        match n {
            1 => Some(Ship::One),
            2 => Some(Ship::Two),
            _ => None,
        }
    }

    pub fn number(self) -> u32 {
        match self {
            Ship::One => 1,
            Ship::Two => 2,
        }
    }

    pub fn index(self) -> usize {
        self.number() as usize - 1
    }
}

impl fmt::Display for Ship {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.number())
    }
}

/// host.py `sequence`: ASCII decimal digits, 0..=2**31-1.
pub fn seq(text: &str) -> Parsed<u32> {
    let b = text.as_bytes();
    if b.is_empty() || !b.iter().all(u8::is_ascii_digit) {
        return Err(Reject);
    }
    let value = b.iter().try_fold(0u64, |acc, d| {
        let next = acc * 10 + u64::from(d - b'0');
        (next <= u64::from(MAX_SEQ)).then_some(next)
    });
    value.map(|v| v as u32).ok_or(Reject)
}

/// economy.py `integer(value, limit, signed)`.
pub fn integer(text: &str, limit: i128, signed: bool) -> Parsed<i128> {
    let digits = if signed { text.strip_prefix('-').unwrap_or(text) } else { text };
    if digits.is_empty() || !digits.bytes().all(|d| d.is_ascii_digit()) {
        return Err(Reject);
    }
    let magnitude = digits.bytes().try_fold(0i128, |acc, d| {
        let next = acc * 10 + i128::from(d - b'0');
        (next <= limit).then_some(next)
    });
    let magnitude = magnitude.ok_or(Reject)?;
    Ok(if digits.len() != text.len() { -magnitude } else { magnitude })
}

/// `integer(value, limit)` (unsigned) narrowed to u32-sized limits.
pub fn uint(text: &str, limit: u32) -> Parsed<u32> {
    integer(text, i128::from(limit), false).map(|v| v as u32)
}

/// A ship number from `integer(f, 2)`: 0 is valid syntax but not a ship.
pub fn ship_field(text: &str) -> Parsed<Option<Ship>> {
    Ok(Ship::from_number(integer(text, 2, false)?))
}

/// `float()` of a field (see [`crate::pynum::py_float`]).
pub fn float(text: &str) -> Parsed<f64> {
    crate::pynum::py_float(text).ok_or(Reject)
}

/// zlib.adler32 of ASCII text.
pub fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for chunk in data.chunks(5552) {
        for &byte in chunk {
            a += u32::from(byte);
            b += a;
        }
        a %= 65521;
        b %= 65521;
    }
    (b << 16) | a
}

/// coop.py `SHOT_DIGEST`: `[a-z0-9_]{1,48}:\d{1,9}\.\d{3}`.
pub fn is_shot_digest(text: &str) -> bool {
    let Some((name, strength)) = text.split_once(':') else { return false };
    let Some((whole, fraction)) = strength.split_once('.') else { return false };
    (1..=48).contains(&name.len())
        && name.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
        && (1..=9).contains(&whole.len())
        && whole.bytes().all(|c| c.is_ascii_digit())
        && fraction.len() == 3
        && fraction.bytes().all(|c| c.is_ascii_digit())
}

/// identity.py `CODE`: `[A-Z0-9-]{1,32}`.
pub fn is_code(text: &str) -> bool {
    (1..=32).contains(&text.len()) && text.bytes().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'-')
}

/// Lower-case hex of even length (`(?:[0-9a-f]{2})+` without the length bound).
pub fn is_lower_hex_pairs(text: &str) -> bool {
    !text.is_empty() && text.len() % 2 == 0 && text.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
}

/// `bytes.fromhex(text).decode('utf-8')` for text already checked by
/// [`is_lower_hex_pairs`].
pub fn hex_utf8(text: &str) -> Parsed<String> {
    let bytes: Vec<u8> = text
        .as_bytes()
        .chunks(2)
        .map(|pair| {
            let digit = |c: u8| if c.is_ascii_digit() { c - b'0' } else { c - b'a' + 10 };
            digit(pair[0]) << 4 | digit(pair[1])
        })
        .collect();
    String::from_utf8(bytes).map_err(|_| Reject)
}

/// A finite pose: x/y/z bounded by `position`, three angles bounded by tau.
pub fn pose(fields: &[&str], position: f64) -> Parsed<[f64; 6]> {
    if fields.len() != 6 {
        return Err(Reject);
    }
    let mut out = [0.0; 6];
    for (i, text) in fields.iter().enumerate() {
        out[i] = float(text)?;
    }
    bounded(&out, position, std::f64::consts::TAU).then_some(out).ok_or(Reject)
}

/// Every value finite; the first three within `linear`, the rest within `angular`.
pub fn bounded(values: &[f64], linear: f64, angular: f64) -> bool {
    values.iter().enumerate().all(|(i, v)| v.is_finite() && v.abs() <= if i < 3 { linear } else { angular })
}

/// Python's `'|'.join(...)` of an envelope.
pub fn join<S: AsRef<str>>(parts: &[S]) -> Vec<u8> {
    let mut out = Vec::with_capacity(96);
    for (i, part) in parts.iter().enumerate() {
        if i > 0 {
            out.push(b'|');
        }
        out.extend_from_slice(part.as_ref().as_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex32_and_atoms() {
        assert!(Hex32::parse(&"a".repeat(32)).is_ok());
        assert!(Hex32::parse(&"A".repeat(32)).is_err());
        assert!(Hex32::parse(&"a".repeat(31)).is_err());
        let h = Hex32::from_bytes([0xab; 16]);
        assert_eq!(h.as_str(), "ab".repeat(16));
        assert!(h.matches(&"ab".repeat(16)) && !h.matches("ab"));
        assert!(Atom::parse(&"x".repeat(96)).is_ok() && Atom::parse(&"x".repeat(97)).is_err());
        assert!(Atom::parse("a b").is_err() && Atom::parse("").is_err());
        assert!(Atom::parse_macro("cluster_01_sector001_macro").is_ok());
        assert!(Atom::parse_macro("cluster").is_err());
    }

    #[test]
    fn sequences_and_integers() {
        assert_eq!(seq("0"), Ok(0));
        assert_eq!(seq("0002147483647"), Ok(MAX_SEQ));
        assert!(seq("2147483648").is_err() && seq("").is_err() && seq("+1").is_err() && seq(" 1").is_err());
        assert_eq!(integer("-12", 12, true), Ok(-12));
        assert!(
            integer("-12", 12, false).is_err() && integer("13", 12, true).is_err() && integer("-", 5, true).is_err()
        );
        assert_eq!(integer(&"9".repeat(60), 5, false), Err(Reject));
    }

    #[test]
    fn digests_and_adler() {
        assert!(is_shot_digest("weapon_gen_s_laser_01_mk1_macro:123.456"));
        assert!(!is_shot_digest("Weapon:1.000") && !is_shot_digest("w:1.00") && !is_shot_digest("w:1234567890.000"));
        assert_eq!(adler32(b"Wikipedia"), 0x11E6_0398);
        assert_eq!(adler32(b""), 1);
    }
}
