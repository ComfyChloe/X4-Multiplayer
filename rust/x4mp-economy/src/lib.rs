//! Per-sector economy model for the engine rewrite program (reports/REWRITE-ECONOMY.md).
//!
//! The economy of X4 9.00 (build 23660954) as the engine runs it, reproduced as data plus
//! pure transition functions, so that one sector's economy can run on a worker thread:
//!
//! - [`production`]: the production-module state machine (`U::ProductionModule::Step`
//!   0x761720) and the lazy, event-ordered catch-up (`U::Container::UpdateProduction`
//!   0x41a910), transition by transition, with the engine's arithmetic.
//! - [`market`]: trade offers in per-sector books and the barrier-published, read-only
//!   [`market::OfferSnapshot`] that trade searches of every sector read.
//! - [`prices`]: the engine's offer price conversions (total price with a modifier, relative
//!   price, quantity factor) and the stock price curve of station offers (0x34cde0), bit for
//!   bit.
//! - [`reservations`]: a container's ware reservations (Container+0x980) with the engine's
//!   merge, expiry and removal rules, order-linked amounts and the per-ware totals the price
//!   computation uses (0x4126d0).
//! - [`msg`]: reservations at containers of other sectors as stamped messages, applied to the
//!   owner's reservation list at the barrier in the original order, with stale-decision
//!   detection and coalesced offer refreshes.
//! - [`sector`]: the per-sector economy world and a deterministic parallel runner used by the
//!   thread-invariance tests (a stand-in for `x4mp_sim::Universe::step` until x4mp-sim is
//!   integrated).
//! - [`capture`]: the `X4ECON` capture records written by the engine-side sampler
//!   (native/engine/econcap.h) and the replay that compares them with [`production`].
//!
//! Stations, their storage and production are sector-local. Nothing in a sector job writes
//! another sector's state: cross-sector effects are [`msg::EconMsg`]s.
//!
//! No third-party crates (workspace rule). Nothing here runs inside X4; every native claim
//! (captures replayed against these models) is in the report with its evidence.

// Negated float comparisons (`!(a > b)`) are deliberate: they keep the engine's NaN
// behaviour of `comisd` + `jbe`/`jae` branches bit for bit.
#![allow(clippy::neg_cmp_op_on_partial_ord)]

pub mod capture;
pub mod ids;
pub mod market;
pub mod msg;
pub mod prices;
pub mod production;
pub mod reservations;
pub mod sector;
pub mod time;
