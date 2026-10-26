//! Per-sector ECS core for the X4 engine rewrite (reports/REWRITE-ECS-CORE.md).
//!
//! Every sector is its own small world; systems run per sector on worker threads, and every
//! cross-sector effect goes through ordered messages applied at a frame barrier. The goal is
//! the program's exactness policy: replacing an engine system must not change results, and
//! running on 1, 2 or 16 threads must give bit-identical state.
//!
//! Frame model ([`Universe::step`]): for each [`System`] in order,
//! 1. **parallel phase**: `run_sector` for every world on the [`Pool`]; a job mutates only
//!    its own [`World`]'s values, reads other sectors through the phase [`Snapshot`], and
//!    writes messages and structural commands into its [`Outbox`];
//! 2. **barrier** (calling thread): outboxes are drained in [`SectorIx`] order (engine tree
//!    order), each in emission order: commands are applied, worlds committed, messages
//!    routed to the next phase's inboxes or the system's serial barrier hook.
//!
//! Modules:
//! - [`id`]: UniverseId, EntityId, SectorIx, OrderKey, IdMap
//! - [`comp`]: the Component trait and core components with the engine's field types
//! - [`world`], [`universe`]: storage, structure staging, step, state hash
//! - [`sched`]: worker pool, System, SectorCtx, reports
//! - [`bus`]: messages, targets, outbox
//! - [`rng`]: the engine generator and per-entity streams
//! - [`shadow`]: exactness policy, comparator, replay, shadow statistics, feature switch
//! - [`capture`]: versioned, build-pinned per-frame capture files and the mirror loader
//!
//! No engine code runs here; the crate is pure Rust and offline-testable. No third-party
//! crates.

pub mod bus;
pub mod capture;
pub mod column;
pub mod comp;
pub mod hash;
pub mod id;
pub mod rng;
pub mod sched;
pub mod shadow;
pub mod snapshot;
pub mod universe;
pub mod world;

pub use bus::{Envelope, Message, Outbox, Target};
pub use column::Bundle;
pub use comp::Component;
pub use id::{EntityId, IdMap, Loc, OrderKey, SectorIx, UniverseId};
pub use sched::{BarrierCtx, FrameInfo, FrameReport, PhaseReport, Pool, SectorCtx, System};
pub use snapshot::Snapshot;
pub use universe::Universe;
pub use world::{World, WorldKind};
