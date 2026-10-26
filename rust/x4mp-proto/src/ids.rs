//! Identifier and credential types. Each is only constructible through a validating
//! constructor, so a holder never re-checks it.

use std::fmt;

/// A participant. Humans are `1..=0x7FFE` (assigned by the relay, lowest free first);
/// `0x7FFF` is the server authority. `0` on the wire means "none".
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct PlayerId(u16);

impl PlayerId {
    pub const AUTHORITY: PlayerId = PlayerId(0x7FFF);
    pub const MAX_HUMAN: u16 = 0x7FFE;

    /// A human player ID (`1..=0x7FFE`).
    pub fn human(n: u16) -> Option<Self> {
        (1..=Self::MAX_HUMAN).contains(&n).then_some(PlayerId(n))
    }

    /// Any participant, including the authority (`1..=0x7FFF`).
    pub fn from_wire(n: u16) -> Option<Self> {
        (1..=0x7FFF).contains(&n).then_some(PlayerId(n))
    }

    pub fn get(self) -> u16 {
        self.0
    }

    pub fn is_authority(self) -> bool {
        self == Self::AUTHORITY
    }
}

impl fmt::Display for PlayerId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.is_authority() {
            f.write_str("authority")
        } else {
            write!(f, "player{}", self.0)
        }
    }
}

/// A replicated object's session network ID (never 0; 0 on the wire means "none").
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct ObjectId(u64);

impl ObjectId {
    pub fn new(n: u64) -> Option<Self> {
        (n != 0).then_some(ObjectId(n))
    }

    pub fn get(self) -> u64 {
        self.0
    }

    /// `Option<ObjectId>` as its wire value (0 = none).
    pub fn wire(id: Option<ObjectId>) -> u64 {
        id.map_or(0, ObjectId::get)
    }
}

/// A session sector index (never 0). The mapping from X4 sector macros to indices is
/// fixed by the authority for the session (see PROTOCOL-V2.md, open questions).
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct SectorId(u32);

impl SectorId {
    pub fn new(n: u32) -> Option<Self> {
        (n != 0).then_some(SectorId(n))
    }

    pub fn get(self) -> u32 {
        self.0
    }
}

/// An NPC faction index in the session faction table (`0..=0x7FFF`).
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct FactionId(u16);

impl FactionId {
    pub fn new(n: u16) -> Option<Self> {
        (n <= 0x7FFF).then_some(FactionId(n))
    }

    pub fn get(self) -> u16 {
        self.0
    }
}

/// Who owns a replicated object. Wire: `0` unowned, `1..=0x7FFE` human player,
/// `0x8000 | f` NPC faction `f`. `0x7FFF` (the authority's player ID) is not an owner.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub enum Owner {
    Unowned,
    Player(PlayerId),
    Faction(FactionId),
}

impl Owner {
    pub fn to_wire(self) -> u16 {
        match self {
            Owner::Unowned => 0,
            Owner::Player(p) => p.get(),
            Owner::Faction(f) => 0x8000 | f.get(),
        }
    }

    pub fn from_wire(v: u16) -> Option<Self> {
        match v {
            0 => Some(Owner::Unowned),
            0x7FFF => None,
            1..=0x7FFE => PlayerId::human(v).map(Owner::Player),
            _ => FactionId::new(v & 0x7FFF).map(Owner::Faction),
        }
    }

    /// `Some(player)` for a human-owned object.
    pub fn player(self) -> Option<PlayerId> {
        match self {
            Owner::Player(p) => Some(p),
            _ => None,
        }
    }
}

/// Constant-time equality over byte arrays.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// The 128-bit session token (the 32 lower-case hex characters of `session.json`).
#[derive(Clone, Copy)]
pub struct Token(pub [u8; 16]);

impl Token {
    /// Parse the same `[a-f0-9]{32}` text form used by the version-1 protocol.
    pub fn parse_hex(text: &str) -> Option<Self> {
        let b = text.as_bytes();
        if b.len() != 32 {
            return None;
        }
        let digit = |c: u8| match c {
            b'0'..=b'9' => Some(c - b'0'),
            b'a'..=b'f' => Some(c - b'a' + 10),
            _ => None,
        };
        let mut out = [0u8; 16];
        for (i, pair) in b.chunks(2).enumerate() {
            out[i] = digit(pair[0])? << 4 | digit(pair[1])?;
        }
        Some(Token(out))
    }

    /// Constant-time comparison.
    pub fn matches(&self, other: &Token) -> bool {
        ct_eq(&self.0, &other.0)
    }
}

impl PartialEq for Token {
    fn eq(&self, other: &Self) -> bool {
        self.matches(other)
    }
}

impl Eq for Token {}

impl fmt::Debug for Token {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Never print credentials.
        f.write_str("Token(..)")
    }
}

/// A 64-bit lease the relay issues at admission (never 0). Every later datagram
/// carries it; together with the source address it authenticates the sender.
#[derive(Clone, Copy)]
pub struct Lease(u64);

impl Lease {
    pub fn new(n: u64) -> Option<Self> {
        (n != 0).then_some(Lease(n))
    }

    pub fn get(self) -> u64 {
        self.0
    }

    /// Constant-time comparison.
    pub fn matches(&self, other: Lease) -> bool {
        ct_eq(&self.0.to_le_bytes(), &other.0.to_le_bytes())
    }

    pub fn wire(lease: Option<Lease>) -> u64 {
        lease.map_or(0, Lease::get)
    }
}

impl PartialEq for Lease {
    fn eq(&self, other: &Self) -> bool {
        self.matches(*other)
    }
}

impl Eq for Lease {}

impl fmt::Debug for Lease {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Lease(..)")
    }
}

/// A client instance nonce (128 bits, chosen by the client per game start).
pub type Nonce = [u8; 16];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owner_wire_values_round_trip() {
        for v in [0u16, 1, 6, 0x7FFE, 0x8000, 0x8001, 0xFFFF] {
            assert_eq!(Owner::from_wire(v).unwrap().to_wire(), v);
        }
        assert_eq!(Owner::from_wire(0x7FFF), None);
        assert_eq!(Owner::from_wire(3), Some(Owner::Player(PlayerId::human(3).unwrap())));
    }

    #[test]
    fn ids_reject_zero_and_tokens_parse_hex() {
        assert!(ObjectId::new(0).is_none() && SectorId::new(0).is_none() && Lease::new(0).is_none());
        assert!(PlayerId::human(0x7FFF).is_none() && PlayerId::from_wire(0x7FFF).is_some());
        let t = Token::parse_hex(&"ab".repeat(16)).unwrap();
        assert_eq!(t.0, [0xab; 16]);
        assert!(Token::parse_hex(&"AB".repeat(16)).is_none() && Token::parse_hex("ab").is_none());
        assert_eq!(format!("{t:?}"), "Token(..)");
    }
}
