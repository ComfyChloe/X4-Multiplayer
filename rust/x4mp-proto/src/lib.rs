//! X4 multiplayer wire protocol, version 2.
//!
//! Foundations of the target architecture (TARGET-ARCHITECTURE-PROPOSAL.md sections
//! 2-8, migration step 1): N players identified by [`PlayerId`] instead of slots
//! p1/p2/p3, simulation-frame stamps on all state, an [`Owner`] on every replicated
//! object, and sector channels with relevance tiers ([`sched`]).
//!
//! This crate is pure: no sockets, no clock, no engine hooks, no third-party crates.
//! It is shared by the relay (`x4lan-relay`, opt-in v2 mode) and later by the game-side
//! core. Wire layouts, versioning rules and the migration from the version-1 text
//! protocols are in `PROTOCOL-V2.md`.
//!
//! The version-1 protocol (bounded ASCII `KIND|1|...` envelopes) is untouched; a v2
//! datagram starts with the non-ASCII magic `F4 32` and can never parse as version 1.

pub mod codec;
pub mod ids;
pub mod msg;
pub mod quant;
pub mod rtt;
pub mod sched;

pub use codec::{DecodeError, EncodeError};
pub use ids::{FactionId, Lease, Nonce, ObjectId, Owner, PlayerId, SectorId, Token};
pub use msg::{
    decode, encode, encode_snapshots, peek_header, retarget, Admission, Bye, Capabilities, CombatIntent, Event,
    EventKind, Header, Hello, Intent, IntentKind, IntentResult, Interest, Kind, Label, Manoeuvre, Message, MotionMode,
    ObjectState, Outcome, Packet, Ping, Pong, Reason, RecordRef, RefuseReason, Role, Roster, RosterEntry,
    SectorInterest, SnapshotBuilder, SnapshotView, Subscribe, Welcome, HEADER_LEN, MAGIC, MAJOR, MAX_DATAGRAM, MINOR,
};
