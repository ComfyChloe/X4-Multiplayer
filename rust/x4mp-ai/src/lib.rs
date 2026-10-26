//! Native AI order behaviours for the per-sector rewrite (reports/REWRITE-AI-ORDERS.md).
//!
//! X4's AI orders are XML scripts run by one interpreter (`Scripts::ExecState_Run` 0xb58bb0) under
//! `AIDirector::Move` 0x106d250. This crate re-implements the hottest ones as Rust state machines that
//! make the same decisions, in the same order, with the same RNG draws, given the same world inputs.
//!
//! Layers:
//! - [`rng`]: the engine generator and every draw routine the scripts reach (bit-exact).
//! - [`val`], [`trace`]: script values and the X4AITRACE v1 capture/replay format.
//! - [`world`]: what a behaviour reads ([`world::Facts`]), the engine queries it issues
//!   ([`world::Query`]), the effects it emits ([`world::Effect`]) and its outlet ([`world::Io`]).
//! - [`runtime`]: the per-entity context: script stack, wakes, call/return, interrupt precedence.
//! - [`orders`]: the ported scripts (`order.fight.patrol`, `move.seekenemies`).
//! - [`replay`]: offline replay of a trace with a bit-exact diff.
//! - [`system`]: the per-sector driver with per-entity RNG streams and a thread-invariance test bed.
//!
//! No third-party crates (workspace rule). No engine code is copied; semantics come from the game's
//! own XML content and from disassembly notes cited at each item.

pub mod orders;
pub mod replay;
pub mod rng;
pub mod runtime;
pub mod system;
pub mod trace;
pub mod val;
pub mod world;
