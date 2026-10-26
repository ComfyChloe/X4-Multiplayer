# Experimental Windows simulation callback

Task owner: Codex, `contrib/codex-engine-patches`, based on `be94436`.
Internal instrumentation; default Windows driver in this branch, separate from
the UDP transport. Not released or installed in the current playtest.

`simtick.c` registers a callback in empty MoveWorker VBI slot 7. It runs after
pathing/physics and the UI update, before render preparation. It changes one data
pointer in the running process, never the executable on disk. It refuses unknown
executables, modified dispatch signatures, occupied slots, loading mode and
non-main Lua threads. The DLL is pinned before registration. A Lua userdata owns
the registration and disarms it on collection/state closure. Callback errors stop
dispatch; no Lua exception crosses an engine frame.

This is a **prototype requiring native validation**, not headless support. It
still uses the UI Lua state. With `-disableui` that state does not exist. Minimized
Windows X4 stops the entire simulation; a VBI callback cannot unstop it. A future
headless driver needs its own runtime and bootstrap, and minimized operation needs
a separately verified main-loop change. Do not call engine exports on a UDP worker.

The slot dispatches once per MoveWorker frame, including frames whose game time is
paused. Existing adapter pause checks still guard simulation writes; transport
heartbeats may continue. A frame callback is not a fixed simulation timestep.

The internal packager compiles this module and includes it in the Windows package.
No runtime switch or manual DLL installation is required. Release builders supply
Zig 0.15.2 using `--zig`, `X4LAN_ZIG` or PATH; a build failure stops packaging before
creating the destination. The DLL is never committed. For standalone development
checks (output stays under ignored `.local/`):

```
python native/engine/build.py --zig /path/to/zig --test
```

In an explicitly isolated development installation, load the resulting DLL with
`package.loadlib(path, 'luaopen_x4lan_simtick')()`. `module.start(function)` returns
an owning userdata or `nil, reason`; retain the userdata. `handle:active()` reports
whether it is armed, `handle:stop()` disarms it, `handle:frames()` counts successful
callbacks. Only one owner may be active per process. Do not register the same work
through both this API and UI `onUpdate`.

The Windows adapters always select this driver in the supported profile-45841
configuration. Linux retains its existing UI driver until its native scheduling
ABI is implemented. `X4LAN_SIMTICK_PATH` can supply a development path.
UI `onUpdate` bootstraps
registration and watches its status; only the native slot calls the network frame.
Loading retries at most once a second. Other refusals stop the Windows adapter;
they do not silently revert to UI networking.

Native lifecycle checks remain required: register after world startup; count callbacks; pause/unpause; load a
disposable world; return to menu; reload UI; collect/stop the handle; provoke a Lua
error; try a second owner; verify no callback after teardown. Also measure focused,
unfocused and minimized behavior. No normal saves or live playtest installations.

## Fixed-tick stage 0 probe

Task owner: Kimi, `contrib/kimi-fixed-sim-scheduler`; see
`reports/FIXED-SIM-SCHEDULER.txt`. `fixedtick.c` is a separate, unpackaged and
opt-in module. It registers a native, non-Lua callback in unused VBI slot 5, leaving
slot 7 available for networking. It only reads TimerBase and records cadence; it
does not change time, release semaphores, patch code or dispatch Lua from the callback.
`handle:stats()` returns frame/loading/pause counters, latest and maximum wall delta,
game/real-time deltas and current clocks/speed. `handle:active()` and `handle:stop()`
control its lifetime.

Build and test it independently (output remains under ignored `.local/`):

```
python native/engine/build.py --zig /path/to/zig --fixedtick --test
python dev/check_hook_signatures.py /path/to/copied/X4.exe
```

`dev/fixed_tick_probe.lua` is the explicit isolated-profile loader and emits one
`FRAMEPROBE` record per second. `X4LAN_FIXEDTICK_PATH` selects the development DLL.
The module is not included by the packager and tracked configuration does not load it.
Both modules use the signatures generated from `native/engine/signatures.py`.
The executable's verified PE timestamp is `0x6a1eaded`; an earlier `0x6a1eaeed`
transcription was incorrect. Offline hashes cover nine sites, including future
FrameTick/main-loop work. Stage 1: `X4LAN_FIXEDTICK_MODE='fixed60'` makes the loader call
`start('fixed60')`, which NOPs FrameTick's movie-capture branch (0xfa3302) and sets
the capture frame time to 1/60; it requires and re-checks a 60 Hz frame-rate limit
every frame and restores the original bytes on any disarm. Below 60 fps game time
runs slow under Stage 1; Stage 2 (`fixed60catchup`) adds bounded catch-up. Stage 3
(`fixed60stage3`) also redirects only the displacement of the main loop's
`WaitForSingleObject(g_semSimDone, 16)` call (0x12ae895) to a module-owned slot near the
image, whose wrapper re-presents the last completed frame on a gated timeout and
returns the real wait result. Options come from `X4LAN_FIXEDTICK_OPTIONS`
(`kcap`, `represent`, `shortwait`, `stall_ms`, `stall_every`, `stall_slot`). The
offline signature check now covers 15 sites. Details, gates, counters and the native
experiment: `reports/FIXED-SIM-SCHEDULER.txt`.

## Remaining patch tasks

1. **H3:** zone attention alone does not establish high-attention NPC simulation.
   Zone computation returns 8 for the local player's zone; object computation has
   a separate proximity test for 9, and body eligibility also depends on the
   physics scene. Do not replace this with a repeated write to `Component+0x1ec`:
   that bypasses transition events and is overwritten by recomputation. The
   attention-agent's completed design and a zone/NPC/body probe are prerequisites
   for choosing the production hook.
2. **Kinematic proxy:** the latest motion analysis supersedes the initial buffer
   hypothesis. `SetObjectSectorPos` queues a write to both slots before the swap.
   ForcedFlightControlModel is a candidate, but target reference/time semantics,
   competing target writers and takeover restoration are not yet validated.
   Preserve the separate `contrib/ferrite-proxy-motion` mitigation for review.
3. **Networking:** default Windows adapter migration is implemented but unvalidated in X4;
   an independent headless bootstrap remains to be implemented.
4. **H2:** rendering suppression and the minimized main-message-loop gate are
   separate. Neither is solved by skipping UI or a render callback blindly.
5. **Player identity:** keep the singleton intact. Review the MD inventory and
   PR 50 before selecting any remaining engine-specific checks.

Evidence inputs: local `x4-re/coord/reports/{simtick,motion,entity,player}.md`,
attention-agent static observations. They are analyses, not gameplay test results.

## Validation on 25 October 2026

- Windows x64, Zig 0.15.2: `build.py --zig ... --test` builds with
  `-Wall -Wextra -Werror`; synthetic lifecycle tests pass. They exercise dispatch,
  stack restoration, loading/inactive UI, foreign thread, held UI lock, replaced
  table/slot/Anark pointer, callback error, self-stop and idempotent cleanup.
- `check_lua_abi.py --lua-runtime <local lua51_64.dll> --module <built DLL>`:
  real Lua ABI, invalid arguments, coroutine rejection and non-X4 process
  rejection pass. This is a separate Python process, not an injected game test.
- Six new LuaJIT tests pass on Windows and Linux: native ownership without UI
  ticks, no duplicate dispatch, loading retry, terminal refusal/disarm, Linux UI
  path and profile guards. A Windows adapter test also verifies that networking
  selects native dispatch without a configuration flag. Package assembly checks
  Windows inclusion, Linux exclusion and aborting on compiler failure.
- WSL Ubuntu 24.04, Python 3.12, Lupa 2.8: `dev/check.py` ran 642 tests,
  **641 pass, one failure, no skips**. Failure:
  `test_failed_native_capture_is_reported_and_retry_retains_evidence` expects
  "content mismatch" but receives "World preparation timed out". The same
  failure reproduces in the unchanged `be94436` mainsync worktree (15/16 launcher
  tests pass). No launcher code was changed to hide the failure.
- X4 was already running. No game attachment, installation change, save access,
  game launch or gameplay acceptance test was performed for this contribution.
  Full integration remains gated on the suite failure and native lifecycle tests.
