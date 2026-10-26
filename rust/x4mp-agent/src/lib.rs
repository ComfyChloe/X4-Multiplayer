//! Opt-in, build-pinned Linux X4 engine agent, loaded with `LD_PRELOAD` by the dedicated
//! server supervisor. It does nothing unless `X4MP_AGENT=1` and the process is the pinned
//! X4 build; then it registers a per-simulation-frame observer in VBI slot 13.

pub mod config;
pub mod elf;
pub mod export;
pub mod loadprof;
pub mod loadstages;
pub mod logic;
pub mod pinned;
pub mod procstat;
pub mod sha256;

#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
mod agent;
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
pub mod forkprobe;
