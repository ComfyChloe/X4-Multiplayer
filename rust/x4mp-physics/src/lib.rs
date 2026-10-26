//! Physics and flight-job rewrite (physics agent, contrib/ferrite-rw-physics).
//!
//! - [`flight`]: the pathing agents' flight control models (`U::Path::*FlightControlModel::Update`),
//!   original operation order, CRT math injectable.
//! - [`oracle`] (Windows): maps the pinned `X4.exe` into the process and calls single original
//!   functions, for offline differential tests of reimplemented kernels.
//!
//! See reports/REWRITE-PHYSICS.md. Every result says whether it is static, offline or native.

pub mod flight;
#[cfg(all(windows, target_arch = "x86_64"))]
pub mod oracle;
