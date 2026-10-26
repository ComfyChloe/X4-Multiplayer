//! Per-frame Rust core for X4 (target architecture, section 9).
//!
//! This crate has **no hooks of its own**. A host that already owns a verified, per-frame
//! engine callback drives it:
//!
//! - Linux: `x4mp-agent` (LD_PRELOAD, VBI slot 13) calls [`runtime::frame`] from its
//!   `export::on_frame` extension point.
//! - Windows: `x4mp-core-win` (cdylib) exposes a C ABI that the slot-12 observer of the
//!   hooks crate, or a Lua `onUpdate` on the same MoveWorker thread, calls once per frame.
//!
//! Layers:
//!
//! - [`pins`]: the one supported build (Steam 23660954) and, per exported function, its
//!   pinned address and the SHA-256 of its first 16 code bytes on Linux and Windows.
//! - [`resolve`]: runtime symbol lookup (`dlsym` / `GetProcAddress` on the main module).
//! - [`ffi`]: typed bindings. A function that is missing, at another address, has other
//!   code bytes or is a compiled-out stub stays unbound; the capabilities that need it are
//!   disabled. Nothing is called through an unverified pointer.
//! - [`frame`]: the frame driver: subsystems, per-subsystem time budgets, cost counters
//!   and `X4MP_CORE COST` lines in the style of `HASHCOST`.
//! - [`mdq`]: bounded queue of engine actions that have no export, executed by one MD cue
//!   (`executor/`), with ids, results, timeouts and back-pressure.
//! - [`pose`]: the first subsystem, a read-only probe of the local player ship's pose.
//! - [`runtime`]: the process-wide instance and the C-ABI-shaped entry points hosts export.
//!
//! Engine calls are only valid on the engine's MoveWorker thread inside the frame callback.
//! The types enforce that: [`ffi::Engine`] exists only while the driver runs a frame.

pub mod config;
pub mod ffi;
pub mod frame;
pub mod logbuf;
pub mod mdq;
pub mod pins;
pub mod pose;
pub mod resolve;
pub mod runtime;
pub mod sha256;
