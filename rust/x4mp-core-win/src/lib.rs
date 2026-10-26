//! Windows entry for `x4mp-core`: a cdylib with a C ABI and **no hooks of its own**.
//!
//! It verifies the running `X4.exe` exactly as the hooks crate does (PE machine,
//! timestamp, SizeOfImage, file size and SHA-256 of build 23660954), binds the pinned
//! exports through `GetProcAddress` on the main module, and then runs the core whenever
//! a caller on the engine thread drives a frame. Built as `x4mp_core_win.dll`.
//!
//! # C ABI
//!
//! ```c
//! int      x4mp_core_win_start(void);   // 0 running, 1 switched off (X4MP_CORE=0), <0 refused
//! int      x4mp_core_win_frame(uint64_t frame, double game_time, double dt, int paused);
//!                                       // 0 ok, <0 refused (core then uninstalled)
//! intptr_t x4mp_core_md_take(uint8_t* buf, size_t cap);    // as on Linux
//! intptr_t x4mp_core_md_result(uint64_t id, int status);    // as on Linux
//! intptr_t x4mp_core_log_take(uint8_t* buf, size_t cap);    // one log line, 0 = none
//! ```
//!
//! # Integration points (all on X4's MoveWorker thread)
//!
//! 1. **Hooks crate observer (preferred, not edited here).** In `move_observer` of
//!    `native/hooks` (`contrib/ferrite-engine-hooks`, VBI slot 12), after
//!    `context_valid()` and the loading-flag check, call `x4mp_core_win_frame(TICK,
//!    game_time, game_time - previous, paused)` through a pointer obtained once with
//!    `GetProcAddress(GetModuleHandleW(L"x4mp_core_win.dll"), "x4mp_core_win_frame")`.
//!    A negative return means the core withdrew itself; the observer keeps running.
//! 2. **Lua fallback.** A UI `onUpdate` script (same thread) can `ffi.load` this DLL and
//!    call `x4mp_core_win_frame` once per update, then run the MD bridge
//!    (`x4mp-core/executor/x4mp_core_bridge.lua`) and print `x4mp_core_log_take` lines.
//!
//! Use exactly one of the two per process: the core claims the first calling thread.
//! `x4mp_core_win_start` hashes the 55 MB executable once (about a second), so call it
//! during setup, not every frame. The DLL pins itself so LuaJIT cannot unload it.

pub mod pe;

#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
mod win;

/// Return codes of `x4mp_core_win_start` / `x4mp_core_win_frame`.
pub mod code {
    pub const OK: i32 = 0;
    pub const SWITCHED_OFF: i32 = 1;
    pub const INVALID_CONFIG: i32 = -1;
    pub const WRONG_BUILD: i32 = -2;
    pub const INSTALL_FAILED: i32 = -3;
    pub const NOT_STARTED: i32 = -4;
    pub const REFUSED: i32 = -5;
}
