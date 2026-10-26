//! Bit-exact models of X4's component pose math and view-cache build, for the engine
//! rewrite program (`reports/REWRITE-TRANSFORMS.md`).
//!
//! Target: X4: Foundations 9.00, Steam build 23660954 (X4.exe SHA-256 `19750a65...6891`).
//! All addresses are Windows RVAs (VA = 0x140000000 + RVA).
//!
//! - [`posrot`]: the `Math::PosRot` pose layout and the exact SSE arithmetic of
//!   `GetLocalTransform` (0x7461c0) and `GetTransformRelativeTo` (0x3db8a0).
//! - [`cache`]: the view-cache build `U::Component::BuildCache` (0x3d4a80) with its cache
//!   specifications, truncation, timers, expiry and per-component refresh
//!   `PropagateCacheFlags` (0x3d46b0), as an event-logging model.
//! - [`parallel`]: the same build with per-sector subtrees on worker threads, merged so
//!   that the log and the final state are identical, with explicit fallback hazards.
//! - [`scenario`]: scenario/result files shared with the offline oracle, and a generator
//!   of engine-like universes.
//!
//! The crate has no hooks and no engine access. Exactness claims are checked offline
//! against the original machine code by `native/engine/xform_oracle.c` (see
//! `tests/oracle.rs`); native in-game capture and shadow runs are separate milestones.

pub mod cache;
pub mod parallel;
pub mod posecases;
pub mod posrot;
pub mod scenario;
