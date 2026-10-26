//! Engine rewrite program, combat system (weapons, turrets, projectiles, damage).
//!
//! First milestone: the turret joint step ([`ikjoint`]), the innermost per-frame update
//! of turret aiming and of turrets returning to rest, reimplemented bit-exactly from the
//! behaviour of X4 build 23660954 (`IKValue_RotateToward`, RVA 0xe1f840), with
//!
//! * [`capture`]: a capture/replay format for the original's inputs and outputs,
//! * [`batch`]: per-sector batched steps whose results do not depend on the thread count,
//! * [`oracle`] (Windows x64): the original machine code of the local pinned `X4.exe`
//!   mapped into this process, used by the tests as the reference,
//! * [`game`] (Windows x64): the in-game recorder/shadow (`luaopen_x4mp_combat` in the
//!   cdylib `x4mp_combat.dll`) that captures the original's calls in a lane run.
//!
//! The method, findings (RVAs, data layouts) and the shadow design are in
//! `reports/REWRITE-COMBAT.md`. No game code is copied here; the Rust follows the
//! documented behaviour, operation order included.

pub mod batch;
pub mod capture;
#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
pub mod game;
pub mod ikjoint;
#[cfg(target_arch = "x86_64")]
pub mod mxcsr;
#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
pub mod oracle;
pub mod testrng;
