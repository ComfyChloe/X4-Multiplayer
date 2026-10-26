//! In-game side (Windows x64): the recorder/shadow of the joint step, loaded by the lane
//! script `dev/combat_scenario/combat_capture.lua` as `x4mp_combat.dll`.
//!
//! One build-pinned code patch at 0xe1f840 (see reports/REWRITE-COMBAT.md section 9 and
//! rewrite-coord/hooks.tsv): verified executable (headers, size, SHA-256), verified site
//! (16 pinned bytes + SHA-256 of 32 bytes), 16-byte atomic install and removal, original
//! always authoritative, fail closed on any mismatch.

pub mod lua;
pub mod patch;
pub mod recorder;
pub mod session;
