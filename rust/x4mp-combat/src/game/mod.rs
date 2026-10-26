//! In-game side (Windows x64), loaded only in lane runs by the lane script
//! `dev/combat_scenario/combat_capture.lua` as `x4mp_combat.dll`.
//!
//! * the recorder/shadow/switch of the joint step: one build-pinned code patch at 0xe1f840
//!   (reports/REWRITE-COMBAT.md section 9, rewrite-coord/hooks.tsv): verified executable
//!   (headers, size, SHA-256), verified site (16 pinned bytes + SHA-256 of 32 bytes), 16-byte
//!   atomic install and removal, fail closed on any mismatch;
//! * the combat census ([`census`]): data-only vtable-entry hooks (verified originals,
//!   atomic swap and restore) that count and measure weapon and bullet updates per frame.

pub mod census;
pub mod lua;
pub mod patch;
pub mod recorder;
pub mod session;
pub mod vthook;
