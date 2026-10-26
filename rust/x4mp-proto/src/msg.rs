//! v2 messages and their binary encoding. Layouts are documented in PROTOCOL-V2.md;
//! every multi-byte integer is little-endian.
//!
//! Decoding is zero-copy where it matters: snapshot records, intent parameters and
//! labels borrow from the datagram, and a relay can forward a record by copying its raw
//! bytes ([`RecordRef::raw`], [`SnapshotBuilder::push_record`]). Every decode path is
//! bounds- and domain-checked and returns [`DecodeError`] instead of panicking.

use crate::codec::{DecodeError, EncodeError, Reader, Writer};
use crate::ids::{Lease, Nonce, ObjectId, Owner, PlayerId, SectorId, Token};

/// First two bytes of every v2 datagram. `0xF4` is not ASCII, so the version-1 relay
/// (which drops non-ASCII datagrams) can never mistake a v2 datagram for its own.
pub const MAGIC: [u8; 2] = [0xF4, 0x32];
pub const MAJOR: u8 = 2;
/// The minor version this crate writes and fully understands.
pub const MINOR: u8 = 0;
pub const HEADER_LEN: usize = 18;
/// Largest v2 datagram: fits the 1280-byte IPv6 minimum MTU minus IP/UDP headers.
pub const MAX_DATAGRAM: usize = 1200;
pub const MAX_BODY: usize = MAX_DATAGRAM - HEADER_LEN;
pub const MAX_LABEL: usize = 64;
pub const MAX_SUBSCRIBED_SECTORS: usize = 32;
pub const MAX_INTENT_PARAMS: usize = 512;
pub const MAX_ROSTER: usize = 64;
/// Bytes of a snapshot record without / with the combat-intent block.
pub const RECORD_BASE_LEN: usize = 61;
pub const RECORD_COMBAT_LEN: usize = RECORD_BASE_LEN + 10;
/// Offset of the `age` field inside a record (the relay rewrites it when it re-bases).
pub const RECORD_AGE_OFFSET: usize = 13;
/// Offsets of `owner` (u16) and `sector` (u32) inside a record (patched by the relay
/// when an Event changes them).
pub const RECORD_OWNER_OFFSET: usize = 17;
pub const RECORD_SECTOR_OFFSET: usize = 19;
/// Snapshot body bytes before the first record (source, frame, count).
pub const SNAPSHOT_HEAD_LEN: usize = 11;
const SNAPSHOT_COUNT_OFFSET: usize = HEADER_LEN + 10;

const RECORD_FLAG_COMBAT: u8 = 1;
const RECORD_FLAG_AMBIENT: u8 = 2;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash, PartialOrd, Ord)]
#[repr(u8)]
pub enum Kind {
    Hello = 1,
    Welcome = 2,
    Ping = 3,
    Pong = 4,
    Subscribe = 5,
    Snapshot = 6,
    Intent = 7,
    IntentResult = 8,
    Event = 9,
    Roster = 10,
    Bye = 11,
}

impl Kind {
    pub const ALL: [Kind; 11] = [
        Kind::Hello,
        Kind::Welcome,
        Kind::Ping,
        Kind::Pong,
        Kind::Subscribe,
        Kind::Snapshot,
        Kind::Intent,
        Kind::IntentResult,
        Kind::Event,
        Kind::Roster,
        Kind::Bye,
    ];

    pub fn from_u8(v: u8) -> Option<Kind> {
        Kind::ALL.get(usize::from(v).wrapping_sub(1)).copied()
    }
}

/// The fixed 18-byte header.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Header {
    pub minor: u8,
    pub kind: Kind,
    pub flags: u8,
    /// Client to relay: the sender. Relay to client: the recipient. `None` before admission.
    pub player: Option<PlayerId>,
    pub lease: Option<Lease>,
}

impl Header {
    /// Whether this decoder must reject unknown bits and trailing bytes.
    // Written for every future MINOR; while MINOR is 0 the comparison is `== 0`.
    #[allow(clippy::absurd_extreme_comparisons)]
    pub fn strict(&self) -> bool {
        self.minor <= MINOR
    }
}

/// Capability bits offered in Hello and granted in Welcome. Unknown bits are kept.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Hash)]
pub struct Capabilities(pub u32);

impl Capabilities {
    pub const COMBAT_INTENT: u32 = 1 << 0;
    pub const MAP_TIER: u32 = 1 << 1;
    pub const INTENTS: u32 = 1 << 2;
    pub const EVENTS: u32 = 1 << 3;
    pub const ROSTER: u32 = 1 << 4;
    /// Everything this crate implements.
    pub const SUPPORTED: Capabilities =
        Capabilities(Self::COMBAT_INTENT | Self::MAP_TIER | Self::INTENTS | Self::EVENTS | Self::ROSTER);

    pub fn has(self, bit: u32) -> bool {
        self.0 & bit == bit
    }

    pub fn intersect(self, other: Capabilities) -> Capabilities {
        Capabilities(self.0 & other.0)
    }
}

/// `[A-Za-z0-9_.-]{1,64}`: build label and world (bubble/seed) identity.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct Label<'a>(&'a str);

impl<'a> Label<'a> {
    pub fn new(text: &'a str) -> Option<Self> {
        let ok = (1..=MAX_LABEL).contains(&text.len())
            && text.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'));
        ok.then_some(Label(text))
    }

    pub fn as_str(&self) -> &'a str {
        self.0
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum Role {
    Player,
    Authority,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Hello<'a> {
    pub token: Token,
    pub nonce: Nonce,
    pub role: Role,
    /// With `resume_lease`: reclaim this player ID (reconnect). `None` for a fresh join.
    pub want_player: Option<PlayerId>,
    pub resume_lease: Option<Lease>,
    pub capabilities: Capabilities,
    pub build: Label<'a>,
    pub world: Label<'a>,
}

/// Why the relay refused a Hello. Wire value 1..=255; unknown values are kept.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct RefuseReason(u8);

impl RefuseReason {
    pub const FULL: RefuseReason = RefuseReason(1);
    pub const BUILD_MISMATCH: RefuseReason = RefuseReason(2);
    pub const WORLD_MISMATCH: RefuseReason = RefuseReason(3);
    pub const AUTHORITY_TAKEN: RefuseReason = RefuseReason(4);
    pub const RESUME_REJECTED: RefuseReason = RefuseReason(5);
    pub const VERSION: RefuseReason = RefuseReason(6);
    pub const NONCE_RETIRED: RefuseReason = RefuseReason(7);
    pub const ADDRESS_IN_USE: RefuseReason = RefuseReason(8);

    pub fn new(v: u8) -> Option<Self> {
        (v != 0).then_some(RefuseReason(v))
    }

    pub fn get(self) -> u8 {
        self.0
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Admission {
    Accepted { player: PlayerId, lease: Lease },
    Refused(RefuseReason),
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Welcome {
    /// Echo of the Hello nonce.
    pub nonce: Nonce,
    pub admission: Admission,
    pub capabilities: Capabilities,
    pub max_players: u8,
    /// Random per relay process; a change means the relay restarted (v1 `RESET`).
    pub relay_epoch: u64,
    pub relay_time_us: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Ping {
    pub seq: u32,
    /// Sender clock, microseconds.
    pub time_us: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Pong {
    pub seq: u32,
    /// The Ping's `time_us`, echoed.
    pub echo_time_us: u64,
    /// Responder clock, microseconds.
    pub time_us: u64,
}

/// How much of a sector a subscriber wants.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash, PartialOrd, Ord)]
pub enum Interest {
    /// The sector the player is in: tiers 1-3.
    Full = 0,
    /// One gate/highway away: everything at the tier-3 rate (preload for a jump).
    Adjacent = 1,
    /// Map only: tier 4, on change.
    Map = 2,
}

impl Interest {
    fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(Interest::Full),
            1 => Some(Interest::Adjacent),
            2 => Some(Interest::Map),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct SectorInterest {
    pub sector: SectorId,
    pub interest: Interest,
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Subscribe {
    /// Monotonic per player; an older or equal revision is ignored.
    pub revision: u32,
    /// The sector the player is in (implicitly [`Interest::Full`]).
    pub current: SectorId,
    pub own_ship: Option<ObjectId>,
    pub target: Option<ObjectId>,
    /// Every sector not listed at [`Interest::Map`].
    pub all_map: bool,
    /// At most [`MAX_SUBSCRIBED_SECTORS`], no sector twice.
    pub sectors: Vec<SectorInterest>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash, PartialOrd, Ord)]
pub enum MotionMode {
    Normal = 0,
    Boost = 1,
    Travel = 2,
    Highway = 3,
}

impl MotionMode {
    fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(MotionMode::Normal),
            1 => Some(MotionMode::Boost),
            2 => Some(MotionMode::Travel),
            3 => Some(MotionMode::Highway),
            _ => None,
        }
    }
}

/// NPC manoeuvre state. Unknown values are kept so newer authorities stay readable.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum Manoeuvre {
    Idle,
    Pursue,
    AttackRun,
    Orbit,
    Evade,
    Retreat,
    Other(u8),
}

impl Manoeuvre {
    pub fn from_u8(v: u8) -> Self {
        match v {
            0 => Manoeuvre::Idle,
            1 => Manoeuvre::Pursue,
            2 => Manoeuvre::AttackRun,
            3 => Manoeuvre::Orbit,
            4 => Manoeuvre::Evade,
            5 => Manoeuvre::Retreat,
            n => Manoeuvre::Other(n),
        }
    }

    pub fn to_u8(self) -> u8 {
        match self {
            Manoeuvre::Idle => 0,
            Manoeuvre::Pursue => 1,
            Manoeuvre::AttackRun => 2,
            Manoeuvre::Orbit => 3,
            Manoeuvre::Evade => 4,
            Manoeuvre::Retreat => 5,
            Manoeuvre::Other(n) => n,
        }
    }
}

/// Replicated combat intent: what an NPC is doing, not individual shots.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct CombatIntent {
    pub target: Option<ObjectId>,
    /// Bit `n` set: weapon group `n + 1` is firing.
    pub firing: u8,
    pub manoeuvre: Manoeuvre,
}

/// One object's replicated state. Motion fields are quantised (see [`crate::quant`]).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct ObjectState {
    pub id: ObjectId,
    /// Instance generation: a reused ID gets a higher generation.
    pub generation: u32,
    /// Publisher's simulation frame of this state.
    pub frame: u64,
    pub owner: Owner,
    pub sector: SectorId,
    /// Sector coordinates, 1/32 m.
    pub position: [i32; 3],
    /// Yaw, pitch, roll; 2*pi/65536 rad.
    pub rotation: [i16; 3],
    /// 1/256 m/s.
    pub velocity: [i32; 3],
    /// 1/1024 rad/s.
    pub angular_velocity: [i16; 3],
    pub mode: MotionMode,
    /// Ambient mass traffic: never replicated by the relay.
    pub ambient: bool,
    pub combat: Option<CombatIntent>,
}

impl ObjectState {
    pub fn wire_len(&self) -> usize {
        if self.combat.is_some() {
            RECORD_COMBAT_LEN
        } else {
            RECORD_BASE_LEN
        }
    }
}

/// A validated snapshot record, borrowed from its datagram.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct RecordRef<'a> {
    raw: &'a [u8],
    state: ObjectState,
}

impl<'a> RecordRef<'a> {
    /// The record's bytes exactly as received (including any newer-minor extension).
    pub fn raw(&self) -> &'a [u8] {
        self.raw
    }

    pub fn state(&self) -> &ObjectState {
        &self.state
    }
}

/// A decoded snapshot. Records are validated when the view is decoded.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct SnapshotView<'a> {
    /// The publisher: the authority, or the human whose own ship this is.
    pub source: PlayerId,
    /// Publisher frame when the message was assembled; each record's frame is
    /// `frame - age`.
    pub frame: u64,
    count: u8,
    strict: bool,
    records: &'a [u8],
}

impl<'a> SnapshotView<'a> {
    pub fn len(&self) -> usize {
        usize::from(self.count)
    }

    pub fn is_empty(&self) -> bool {
        self.count == 0
    }

    pub fn records(&self) -> RecordIter<'a> {
        RecordIter { reader: Reader::new(self.records), left: self.count, frame: self.frame, strict: self.strict }
    }
}

pub struct RecordIter<'a> {
    reader: Reader<'a>,
    left: u8,
    frame: u64,
    strict: bool,
}

impl<'a> Iterator for RecordIter<'a> {
    type Item = RecordRef<'a>;

    fn next(&mut self) -> Option<RecordRef<'a>> {
        if self.left == 0 {
            return None;
        }
        self.left -= 1;
        // Already validated when the view was decoded; an error here cannot happen
        // and would only end the iteration.
        read_record(&mut self.reader, self.frame, self.strict).ok()
    }
}

/// Kind of player action; interpreted by the authority, opaque to the relay.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash, PartialOrd, Ord)]
pub struct IntentKind(u16);

impl IntentKind {
    pub const CARGO_DROP: IntentKind = IntentKind(1);
    pub const CARGO_PICKUP: IntentKind = IntentKind(2);
    pub const DOCK: IntentKind = IntentKind(3);
    pub const UNDOCK: IntentKind = IntentKind(4);
    pub const TRADE: IntentKind = IntentKind(5);
    pub const FLEET_ORDER: IntentKind = IntentKind(6);
    pub const MISSION: IntentKind = IntentKind(7);
    pub const PILOT: IntentKind = IntentKind(8);

    pub fn new(v: u16) -> Option<Self> {
        (v != 0).then_some(IntentKind(v))
    }

    pub fn get(self) -> u16 {
        self.0
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Intent<'a> {
    /// Per player, chosen by the client; the dedup key with `player`.
    pub client_seq: u32,
    pub player: PlayerId,
    pub kind: IntentKind,
    pub object: Option<ObjectId>,
    pub generation: u32,
    /// Client frame at which the action was applied optimistically.
    pub frame: u64,
    /// Kind-specific parameters, at most [`MAX_INTENT_PARAMS`] bytes.
    pub params: &'a [u8],
}

/// Refusal / uncertainty reason (never 0). `0x01xx` codes are produced by the relay.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct Reason(u16);

impl Reason {
    pub const NOT_OWNER: Reason = Reason(1);
    pub const STALE_GENERATION: Reason = Reason(2);
    pub const INVALID_PARAMS: Reason = Reason(3);
    pub const OUT_OF_RANGE: Reason = Reason(4);
    pub const BUSY: Reason = Reason(5);
    pub const UNKNOWN_KIND: Reason = Reason(6);
    pub const RULES: Reason = Reason(7);
    pub const AUTHORITY_UNAVAILABLE: Reason = Reason(0x0101);
    pub const AUTHORITY_LOST: Reason = Reason(0x0102);
    pub const RELAY_BUSY: Reason = Reason(0x0103);

    pub fn new(v: u16) -> Option<Self> {
        (v != 0).then_some(Reason(v))
    }

    pub fn get(self) -> u16 {
        self.0
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Outcome {
    Accepted,
    Refused(Reason),
    /// The outcome cannot be known (authority lost while pending). Never retried.
    Uncertain(Reason),
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct IntentResult {
    pub client_seq: u32,
    pub player: PlayerId,
    pub outcome: Outcome,
    /// Authority frame from which the outcome is visible in the state stream (0: none).
    pub frame: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum EventKind {
    OwnerChange { from: Owner, to: Owner },
    Destroy { sector: SectorId },
    SectorChange { from: SectorId, to: SectorId },
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Event {
    /// Authority-wide, monotonic; receivers deduplicate on it.
    pub event_seq: u32,
    pub frame: u64,
    pub object: ObjectId,
    pub generation: u32,
    pub kind: EventKind,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct RosterEntry {
    pub player: PlayerId,
    pub connected: bool,
    /// Smoothed RTT in ms (saturating at 65534); `None` before the first sample.
    pub rtt_ms: Option<u16>,
    pub jitter_ms: Option<u16>,
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Roster {
    pub revision: u32,
    pub authority_connected: bool,
    /// Strictly ascending player IDs, at most [`MAX_ROSTER`].
    pub players: Vec<RosterEntry>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Bye {
    /// 0 = leaving; 1 = removed by the relay; others reserved.
    pub reason: u8,
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Message<'a> {
    Hello(Hello<'a>),
    Welcome(Welcome),
    Ping(Ping),
    Pong(Pong),
    Subscribe(Subscribe),
    Snapshot(SnapshotView<'a>),
    Intent(Intent<'a>),
    IntentResult(IntentResult),
    Event(Event),
    Roster(Roster),
    Bye(Bye),
}

impl Message<'_> {
    pub fn kind(&self) -> Kind {
        match self {
            Message::Hello(_) => Kind::Hello,
            Message::Welcome(_) => Kind::Welcome,
            Message::Ping(_) => Kind::Ping,
            Message::Pong(_) => Kind::Pong,
            Message::Subscribe(_) => Kind::Subscribe,
            Message::Snapshot(_) => Kind::Snapshot,
            Message::Intent(_) => Kind::Intent,
            Message::IntentResult(_) => Kind::IntentResult,
            Message::Event(_) => Kind::Event,
            Message::Roster(_) => Kind::Roster,
            Message::Bye(_) => Kind::Bye,
        }
    }
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Packet<'a> {
    pub header: Header,
    pub message: Message<'a>,
}

// ---------------------------------------------------------------- decoding

/// Validate and decode only the header (cheap routing/authentication check).
pub fn peek_header(datagram: &[u8]) -> Result<Header, DecodeError> {
    if datagram.len() > MAX_DATAGRAM {
        return Err(DecodeError::TooLong);
    }
    if datagram.len() < 2 {
        return Err(DecodeError::TooShort);
    }
    if datagram[..2] != MAGIC {
        return Err(DecodeError::BadMagic);
    }
    let mut r = Reader::new(datagram);
    r.take(2)?;
    let major = r.u8()?;
    if major != MAJOR {
        return Err(DecodeError::UnsupportedMajor(major));
    }
    let minor = r.u8()?;
    let kind_byte = r.u8()?;
    let kind = Kind::from_u8(kind_byte).ok_or(DecodeError::UnknownKind(kind_byte))?;
    let flags = r.u8()?;
    let body_len = usize::from(r.u16()?);
    let player = r.u16()?;
    let lease = r.u64()?;
    if body_len != datagram.len() - HEADER_LEN {
        return Err(DecodeError::LengthMismatch);
    }
    let header = Header {
        minor,
        kind,
        flags,
        player: match player {
            0 => None,
            n => Some(PlayerId::from_wire(n).ok_or(DecodeError::Invalid("header player"))?),
        },
        lease: Lease::new(lease),
    };
    if header.strict() && flags != 0 {
        return Err(DecodeError::ReservedBits);
    }
    Ok(header)
}

/// Decode one datagram.
pub fn decode(datagram: &[u8]) -> Result<Packet<'_>, DecodeError> {
    let header = peek_header(datagram)?;
    let mut r = Reader::new(&datagram[HEADER_LEN..]);
    let strict = header.strict();
    let message = match header.kind {
        Kind::Hello => Message::Hello(read_hello(&mut r)?),
        Kind::Welcome => Message::Welcome(read_welcome(&mut r)?),
        Kind::Ping => Message::Ping(Ping { seq: r.u32()?, time_us: r.u64()? }),
        Kind::Pong => Message::Pong(Pong { seq: r.u32()?, echo_time_us: r.u64()?, time_us: r.u64()? }),
        Kind::Subscribe => Message::Subscribe(read_subscribe(&mut r, strict)?),
        Kind::Snapshot => Message::Snapshot(read_snapshot(&mut r, strict)?),
        Kind::Intent => Message::Intent(read_intent(&mut r)?),
        Kind::IntentResult => Message::IntentResult(read_intent_result(&mut r)?),
        Kind::Event => Message::Event(read_event(&mut r)?),
        Kind::Roster => Message::Roster(read_roster(&mut r, strict)?),
        Kind::Bye => Message::Bye(Bye { reason: r.u8()? }),
    };
    if r.remaining() != 0 && strict {
        return Err(DecodeError::TrailingBytes);
    }
    Ok(Packet { header, message })
}

fn object(v: u64) -> Option<ObjectId> {
    ObjectId::new(v)
}

fn read_label<'a>(r: &mut Reader<'a>, what: &'static str) -> Result<Label<'a>, DecodeError> {
    let len = usize::from(r.u8()?);
    let bytes = r.take(len)?;
    let text = std::str::from_utf8(bytes).map_err(|_| DecodeError::Invalid(what))?;
    Label::new(text).ok_or(DecodeError::Invalid(what))
}

fn read_human(r: &mut Reader<'_>, what: &'static str) -> Result<PlayerId, DecodeError> {
    PlayerId::human(r.u16()?).ok_or(DecodeError::Invalid(what))
}

fn read_sector(r: &mut Reader<'_>, what: &'static str) -> Result<SectorId, DecodeError> {
    SectorId::new(r.u32()?).ok_or(DecodeError::Invalid(what))
}

fn read_owner(r: &mut Reader<'_>) -> Result<Owner, DecodeError> {
    Owner::from_wire(r.u16()?).ok_or(DecodeError::Invalid("owner"))
}

fn read_hello<'a>(r: &mut Reader<'a>) -> Result<Hello<'a>, DecodeError> {
    let token = Token(r.array()?);
    let nonce = r.array()?;
    let role = match r.u8()? {
        0 => Role::Player,
        1 => Role::Authority,
        _ => return Err(DecodeError::Invalid("role")),
    };
    let want_player = match r.u16()? {
        0 => None,
        n => Some(PlayerId::human(n).ok_or(DecodeError::Invalid("want_player"))?),
    };
    if role == Role::Authority && want_player.is_some() {
        return Err(DecodeError::Invalid("want_player"));
    }
    let resume_lease = Lease::new(r.u64()?);
    let capabilities = Capabilities(r.u32()?);
    let build = read_label(r, "build")?;
    let world = read_label(r, "world")?;
    Ok(Hello { token, nonce, role, want_player, resume_lease, capabilities, build, world })
}

fn read_welcome(r: &mut Reader<'_>) -> Result<Welcome, DecodeError> {
    let nonce = r.array()?;
    let status = r.u8()?;
    let reason = r.u8()?;
    let player = r.u16()?;
    let lease = r.u64()?;
    let admission = match (status, reason, player, lease) {
        (0, 0, p, l) => Admission::Accepted {
            player: PlayerId::from_wire(p).ok_or(DecodeError::Invalid("welcome player"))?,
            lease: Lease::new(l).ok_or(DecodeError::Invalid("welcome lease"))?,
        },
        (1, r, 0, 0) => Admission::Refused(RefuseReason::new(r).ok_or(DecodeError::Invalid("refuse reason"))?),
        _ => return Err(DecodeError::Invalid("welcome status")),
    };
    Ok(Welcome {
        nonce,
        admission,
        capabilities: Capabilities(r.u32()?),
        max_players: r.u8()?,
        relay_epoch: r.u64()?,
        relay_time_us: r.u64()?,
    })
}

fn read_subscribe(r: &mut Reader<'_>, strict: bool) -> Result<Subscribe, DecodeError> {
    let revision = r.u32()?;
    let current = read_sector(r, "current sector")?;
    let own_ship = object(r.u64()?);
    let target = object(r.u64()?);
    let flags = r.u8()?;
    if strict && flags & !1 != 0 {
        return Err(DecodeError::ReservedBits);
    }
    let count = usize::from(r.u8()?);
    if count > MAX_SUBSCRIBED_SECTORS {
        return Err(DecodeError::Invalid("sector count"));
    }
    let mut sectors: Vec<SectorInterest> = Vec::with_capacity(count);
    for _ in 0..count {
        let sector = read_sector(r, "sector")?;
        let interest = Interest::from_u8(r.u8()?).ok_or(DecodeError::Invalid("interest"))?;
        if sectors.iter().any(|s| s.sector == sector) {
            return Err(DecodeError::Invalid("duplicate sector"));
        }
        sectors.push(SectorInterest { sector, interest });
    }
    Ok(Subscribe { revision, current, own_ship, target, all_map: flags & 1 != 0, sectors })
}

fn read_record<'a>(r: &mut Reader<'a>, base_frame: u64, strict: bool) -> Result<RecordRef<'a>, DecodeError> {
    let start = r.clone();
    let len = usize::from(r.u8()?);
    if len < RECORD_BASE_LEN {
        return Err(DecodeError::Invalid("record length"));
    }
    let body = r.take(len - 1)?;
    let raw = start.clone().take(len)?;
    let mut b = Reader::new(body);
    let id = ObjectId::new(b.u64()?).ok_or(DecodeError::Invalid("object id"))?;
    let generation = b.u32()?;
    let age = b.u32()?;
    let frame = base_frame.checked_sub(u64::from(age)).ok_or(DecodeError::Invalid("record age"))?;
    let owner = read_owner(&mut b)?;
    let sector = read_sector(&mut b, "record sector")?;
    let position = [b.i32()?, b.i32()?, b.i32()?];
    let rotation = [b.i16()?, b.i16()?, b.i16()?];
    let velocity = [b.i32()?, b.i32()?, b.i32()?];
    let angular_velocity = [b.i16()?, b.i16()?, b.i16()?];
    let mode = MotionMode::from_u8(b.u8()?).ok_or(DecodeError::Invalid("motion mode"))?;
    let flags = b.u8()?;
    if strict && flags & !(RECORD_FLAG_COMBAT | RECORD_FLAG_AMBIENT) != 0 {
        return Err(DecodeError::ReservedBits);
    }
    let combat = if flags & RECORD_FLAG_COMBAT != 0 {
        Some(CombatIntent { target: object(b.u64()?), firing: b.u8()?, manoeuvre: Manoeuvre::from_u8(b.u8()?) })
    } else {
        None
    };
    if strict && b.remaining() != 0 {
        return Err(DecodeError::TrailingBytes);
    }
    let state = ObjectState {
        id,
        generation,
        frame,
        owner,
        sector,
        position,
        rotation,
        velocity,
        angular_velocity,
        mode,
        ambient: flags & RECORD_FLAG_AMBIENT != 0,
        combat,
    };
    Ok(RecordRef { raw, state })
}

fn read_snapshot<'a>(r: &mut Reader<'a>, strict: bool) -> Result<SnapshotView<'a>, DecodeError> {
    let source = PlayerId::from_wire(r.u16()?).ok_or(DecodeError::Invalid("snapshot source"))?;
    let frame = r.u64()?;
    let count = r.u8()?;
    let mut records = r.clone();
    let before = r.remaining();
    for _ in 0..count {
        read_record(r, frame, strict)?;
    }
    let used = before - r.remaining();
    let records = records.take(used)?;
    Ok(SnapshotView { source, frame, count, strict, records })
}

fn read_intent<'a>(r: &mut Reader<'a>) -> Result<Intent<'a>, DecodeError> {
    let client_seq = r.u32()?;
    let player = read_human(r, "intent player")?;
    let kind = IntentKind::new(r.u16()?).ok_or(DecodeError::Invalid("intent kind"))?;
    let object = object(r.u64()?);
    let generation = r.u32()?;
    let frame = r.u64()?;
    let len = usize::from(r.u16()?);
    if len > MAX_INTENT_PARAMS {
        return Err(DecodeError::Invalid("intent params length"));
    }
    let params = r.take(len)?;
    Ok(Intent { client_seq, player, kind, object, generation, frame, params })
}

fn read_intent_result(r: &mut Reader<'_>) -> Result<IntentResult, DecodeError> {
    let client_seq = r.u32()?;
    let player = read_human(r, "result player")?;
    let outcome = r.u8()?;
    let reason = r.u16()?;
    let outcome = match (outcome, Reason::new(reason)) {
        (0, None) => Outcome::Accepted,
        (1, Some(reason)) => Outcome::Refused(reason),
        (2, Some(reason)) => Outcome::Uncertain(reason),
        _ => return Err(DecodeError::Invalid("outcome")),
    };
    Ok(IntentResult { client_seq, player, outcome, frame: r.u64()? })
}

fn read_event(r: &mut Reader<'_>) -> Result<Event, DecodeError> {
    let event_seq = r.u32()?;
    let frame = r.u64()?;
    let kind = r.u8()?;
    let object = ObjectId::new(r.u64()?).ok_or(DecodeError::Invalid("event object"))?;
    let generation = r.u32()?;
    let kind = match kind {
        1 => EventKind::OwnerChange { from: read_owner(r)?, to: read_owner(r)? },
        2 => EventKind::Destroy { sector: read_sector(r, "event sector")? },
        3 => EventKind::SectorChange { from: read_sector(r, "event sector")?, to: read_sector(r, "event sector")? },
        _ => return Err(DecodeError::Invalid("event kind")),
    };
    Ok(Event { event_seq, frame, object, generation, kind })
}

fn read_roster(r: &mut Reader<'_>, strict: bool) -> Result<Roster, DecodeError> {
    let revision = r.u32()?;
    let flags = r.u8()?;
    if strict && flags & !1 != 0 {
        return Err(DecodeError::ReservedBits);
    }
    let count = usize::from(r.u8()?);
    if count > MAX_ROSTER {
        return Err(DecodeError::Invalid("roster count"));
    }
    let mut players: Vec<RosterEntry> = Vec::with_capacity(count);
    let optional = |v: u16| (v != u16::MAX).then_some(v);
    for _ in 0..count {
        let player = read_human(r, "roster player")?;
        if players.last().is_some_and(|p| p.player >= player) {
            return Err(DecodeError::Invalid("roster order"));
        }
        let entry_flags = r.u8()?;
        if strict && entry_flags & !1 != 0 {
            return Err(DecodeError::ReservedBits);
        }
        players.push(RosterEntry {
            player,
            connected: entry_flags & 1 != 0,
            rtt_ms: optional(r.u16()?),
            jitter_ms: optional(r.u16()?),
        });
    }
    Ok(Roster { revision, authority_connected: flags & 1 != 0, players })
}

// ---------------------------------------------------------------- encoding

fn begin(kind: Kind, player: Option<PlayerId>, lease: Option<Lease>, capacity: usize) -> Writer {
    let mut w = Writer::with_capacity(HEADER_LEN + capacity);
    w.bytes(&MAGIC);
    w.u8(MAJOR);
    w.u8(MINOR);
    w.u8(kind as u8);
    w.u8(0);
    w.u16(0); // body length, patched by `finish`
    w.u16(player.map_or(0, PlayerId::get));
    w.u64(Lease::wire(lease));
    w
}

fn finish(mut w: Writer) -> Result<Vec<u8>, EncodeError> {
    if w.len() > MAX_DATAGRAM {
        return Err(EncodeError::TooLarge);
    }
    let body = (w.len() - HEADER_LEN) as u16;
    w.buf[6..8].copy_from_slice(&body.to_le_bytes());
    Ok(w.buf)
}

/// Encode a message into one datagram.
pub fn encode(player: Option<PlayerId>, lease: Option<Lease>, message: &Message<'_>) -> Result<Vec<u8>, EncodeError> {
    let mut w = begin(message.kind(), player, lease, 64);
    match message {
        Message::Hello(h) => {
            w.bytes(&h.token.0);
            w.bytes(&h.nonce);
            w.u8(match h.role {
                Role::Player => 0,
                Role::Authority => 1,
            });
            if h.role == Role::Authority && h.want_player.is_some() {
                return Err(EncodeError::Invalid("want_player"));
            }
            w.u16(h.want_player.map_or(0, PlayerId::get));
            w.u64(Lease::wire(h.resume_lease));
            w.u32(h.capabilities.0);
            for label in [h.build, h.world] {
                w.u8(label.0.len() as u8);
                w.bytes(label.0.as_bytes());
            }
        }
        Message::Welcome(m) => {
            w.bytes(&m.nonce);
            match m.admission {
                Admission::Accepted { player, lease } => {
                    w.u8(0);
                    w.u8(0);
                    w.u16(player.get());
                    w.u64(lease.get());
                }
                Admission::Refused(reason) => {
                    w.u8(1);
                    w.u8(reason.get());
                    w.u16(0);
                    w.u64(0);
                }
            }
            w.u32(m.capabilities.0);
            w.u8(m.max_players);
            w.u64(m.relay_epoch);
            w.u64(m.relay_time_us);
        }
        Message::Ping(p) => {
            w.u32(p.seq);
            w.u64(p.time_us);
        }
        Message::Pong(p) => {
            w.u32(p.seq);
            w.u64(p.echo_time_us);
            w.u64(p.time_us);
        }
        Message::Subscribe(s) => {
            if s.sectors.len() > MAX_SUBSCRIBED_SECTORS {
                return Err(EncodeError::Invalid("sector count"));
            }
            for (i, a) in s.sectors.iter().enumerate() {
                if s.sectors[..i].iter().any(|b| b.sector == a.sector) {
                    return Err(EncodeError::Invalid("duplicate sector"));
                }
            }
            w.u32(s.revision);
            w.u32(s.current.get());
            w.u64(ObjectId::wire(s.own_ship));
            w.u64(ObjectId::wire(s.target));
            w.u8(u8::from(s.all_map));
            w.u8(s.sectors.len() as u8);
            for e in &s.sectors {
                w.u32(e.sector.get());
                w.u8(e.interest as u8);
            }
        }
        Message::Snapshot(view) => {
            w.u16(view.source.get());
            w.u64(view.frame);
            w.u8(view.count);
            w.bytes(view.records);
        }
        Message::Intent(i) => {
            if i.params.len() > MAX_INTENT_PARAMS {
                return Err(EncodeError::Invalid("intent params length"));
            }
            w.u32(i.client_seq);
            w.u16(i.player.get());
            w.u16(i.kind.get());
            w.u64(ObjectId::wire(i.object));
            w.u32(i.generation);
            w.u64(i.frame);
            w.u16(i.params.len() as u16);
            w.bytes(i.params);
        }
        Message::IntentResult(res) => {
            if res.player.is_authority() {
                return Err(EncodeError::Invalid("result player"));
            }
            let (outcome, reason) = match res.outcome {
                Outcome::Accepted => (0, 0),
                Outcome::Refused(r) => (1, r.get()),
                Outcome::Uncertain(r) => (2, r.get()),
            };
            w.u32(res.client_seq);
            w.u16(res.player.get());
            w.u8(outcome);
            w.u16(reason);
            w.u64(res.frame);
        }
        Message::Event(e) => {
            w.u32(e.event_seq);
            w.u64(e.frame);
            w.u8(match e.kind {
                EventKind::OwnerChange { .. } => 1,
                EventKind::Destroy { .. } => 2,
                EventKind::SectorChange { .. } => 3,
            });
            w.u64(e.object.get());
            w.u32(e.generation);
            match e.kind {
                EventKind::OwnerChange { from, to } => {
                    w.u16(from.to_wire());
                    w.u16(to.to_wire());
                }
                EventKind::Destroy { sector } => w.u32(sector.get()),
                EventKind::SectorChange { from, to } => {
                    w.u32(from.get());
                    w.u32(to.get());
                }
            }
        }
        Message::Roster(roster) => {
            if roster.players.len() > MAX_ROSTER {
                return Err(EncodeError::Invalid("roster count"));
            }
            if roster.players.windows(2).any(|p| p[0].player >= p[1].player)
                || roster.players.iter().any(|p| p.player.is_authority())
            {
                return Err(EncodeError::Invalid("roster order"));
            }
            w.u32(roster.revision);
            w.u8(u8::from(roster.authority_connected));
            w.u8(roster.players.len() as u8);
            let optional = |v: Option<u16>| v.map_or(u16::MAX, |v| v.min(u16::MAX - 1));
            for p in &roster.players {
                w.u16(p.player.get());
                w.u8(u8::from(p.connected));
                w.u16(optional(p.rtt_ms));
                w.u16(optional(p.jitter_ms));
            }
        }
        Message::Bye(b) => w.u8(b.reason),
    }
    finish(w)
}

fn write_record(w: &mut Writer, s: &ObjectState, base_frame: u64) -> Result<(), EncodeError> {
    let age = base_frame.checked_sub(s.frame).ok_or(EncodeError::Invalid("record frame after snapshot frame"))?;
    let age = u32::try_from(age).map_err(|_| EncodeError::Invalid("record age"))?;
    w.u8(s.wire_len() as u8);
    w.u64(s.id.get());
    w.u32(s.generation);
    w.u32(age);
    w.u16(s.owner.to_wire());
    w.u32(s.sector.get());
    s.position.iter().for_each(|v| w.i32(*v));
    s.rotation.iter().for_each(|v| w.i16(*v));
    s.velocity.iter().for_each(|v| w.i32(*v));
    s.angular_velocity.iter().for_each(|v| w.i16(*v));
    w.u8(s.mode as u8);
    let mut flags = 0;
    if s.combat.is_some() {
        flags |= RECORD_FLAG_COMBAT;
    }
    if s.ambient {
        flags |= RECORD_FLAG_AMBIENT;
    }
    w.u8(flags);
    if let Some(c) = s.combat {
        w.u64(ObjectId::wire(c.target));
        w.u8(c.firing);
        w.u8(c.manoeuvre.to_u8());
    }
    Ok(())
}

/// Builds one Snapshot datagram record by record, never exceeding [`MAX_DATAGRAM`].
pub struct SnapshotBuilder {
    w: Writer,
    count: u8,
    frame: u64,
}

impl SnapshotBuilder {
    pub fn new(player: Option<PlayerId>, lease: Option<Lease>, source: PlayerId, frame: u64) -> Self {
        let mut w = begin(Kind::Snapshot, player, lease, MAX_BODY);
        w.u16(source.get());
        w.u64(frame);
        w.u8(0);
        SnapshotBuilder { w, count: 0, frame }
    }

    pub fn len(&self) -> usize {
        usize::from(self.count)
    }

    pub fn is_empty(&self) -> bool {
        self.count == 0
    }

    fn fits(&self, bytes: usize) -> bool {
        self.count < u8::MAX && self.w.len() + bytes <= MAX_DATAGRAM
    }

    /// Append a record; `Ok(false)` when it does not fit (finish and start a new one).
    pub fn push(&mut self, state: &ObjectState) -> Result<bool, EncodeError> {
        if !self.fits(state.wire_len()) {
            return Ok(false);
        }
        write_record(&mut self.w, state, self.frame)?;
        self.count += 1;
        Ok(true)
    }

    /// Append a received record verbatim, rewriting only its age for this snapshot's
    /// frame. `Ok(false)` when it does not fit.
    pub fn push_record(&mut self, record: &RecordRef<'_>) -> Result<bool, EncodeError> {
        self.push_raw(record.raw(), record.state().frame)
    }

    /// [`push_record`](Self::push_record) for record bytes kept from an earlier
    /// [`RecordRef::raw`] whose state had frame `record_frame`.
    pub fn push_raw(&mut self, raw: &[u8], record_frame: u64) -> Result<bool, EncodeError> {
        if raw.len() < RECORD_BASE_LEN || usize::from(raw[0]) != raw.len() {
            return Err(EncodeError::Invalid("record bytes"));
        }
        if !self.fits(raw.len()) {
            return Ok(false);
        }
        let age = self
            .frame
            .checked_sub(record_frame)
            .and_then(|a| u32::try_from(a).ok())
            .ok_or(EncodeError::Invalid("record age"))?;
        let at = self.w.len();
        self.w.bytes(raw);
        self.w.buf[at + RECORD_AGE_OFFSET..at + RECORD_AGE_OFFSET + 4].copy_from_slice(&age.to_le_bytes());
        self.count += 1;
        Ok(true)
    }

    pub fn finish(mut self) -> Vec<u8> {
        self.w.buf[SNAPSHOT_COUNT_OFFSET] = self.count;
        // The builder never grows past MAX_DATAGRAM.
        finish(self.w).unwrap_or_default()
    }
}

/// Encode any number of states into as many Snapshot datagrams as needed.
pub fn encode_snapshots(
    player: Option<PlayerId>,
    lease: Option<Lease>,
    source: PlayerId,
    frame: u64,
    states: &[ObjectState],
) -> Result<Vec<Vec<u8>>, EncodeError> {
    let mut out = Vec::new();
    let mut builder = SnapshotBuilder::new(player, lease, source, frame);
    for state in states {
        if !builder.push(state)? {
            out.push(std::mem::replace(&mut builder, SnapshotBuilder::new(player, lease, source, frame)).finish());
            if !builder.push(state)? {
                return Err(EncodeError::TooLarge);
            }
        }
    }
    if !builder.is_empty() || out.is_empty() {
        out.push(builder.finish());
    }
    Ok(out)
}

/// Copy a datagram with another header player and lease (the relay's forwarding
/// path). `None` if it is not a v2 datagram.
pub fn retarget(datagram: &[u8], player: Option<PlayerId>, lease: Option<Lease>) -> Option<Vec<u8>> {
    if datagram.len() < HEADER_LEN || datagram[..2] != MAGIC {
        return None;
    }
    let mut out = datagram.to_vec();
    out[8..10].copy_from_slice(&player.map_or(0, PlayerId::get).to_le_bytes());
    out[10..18].copy_from_slice(&Lease::wire(lease).to_le_bytes());
    Some(out)
}
