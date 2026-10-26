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
runs slow; catch-up scheduling (Stage 2) is not implemented.

## Simulation profiler (`profile` mode, simprof)

`X4LAN_FIXEDTICK_MODE='profile'` arms the probe exactly like the default probe (no patches)
and also starts a sampling profiler of the MoveWorker thread (`simprof.c`, included by
`fixedtick.c`). Options: `X4LAN_SIMPROF_INTERVAL_MS` (0.5..10, default 1),
`X4LAN_SIMPROF_DEPTH` (8..64 frames, default 32), `X4LAN_SIMPROF_REPORT_S` (default 10),
`X4LAN_SIMPROF_FOLD_LINES` (folded stacks logged per frame, default 20). Lua API:
`start('profile'[, interval_ms[, depth]])`, `handle:profile()` (closes a report window),
`handle:folds(n)`; `stats()` adds `simprof` (1 running, 2 disabled) and `simprof_reason`.

**Sampling.** A dedicated thread (time-critical priority, high-resolution waitable timer,
absolute schedule with +-10% jitter) suspends the MoveWorker, reads its context
(`CONTEXT_CONTROL|CONTEXT_INTEGER`; `GetThreadContext` also waits until the asynchronous
`SuspendThread` has taken effect), walks the stack into a preallocated ring buffer, resumes,
and only then aggregates. While the target is suspended the sampler calls only
`GetThreadContext`, `QueryPerformanceCounter`, its own binary search over the in-memory
`.pdata` and `RtlVirtualUnwind`; it allocates nothing, takes no lock, no loader or CRT call.
`RtlLookupFunctionEntry` is not used: on a miss it walks the dynamic function table list under
an SRW lock (older Windows also locks the inverted function table) that the suspended thread may
hold. `RtlVirtualUnwind` is a pure function of image base, function entry and context. Stack
reads stay in `[leaf Rsp, StackBase)`; a leaf Rsp below the TEB `StackLimit` skips the sample, so
no guard page is touched. Unwindable modules are fixed at start: X4.exe, this DLL, ntdll,
kernelbase, kernel32 and X4's static imports (lua51_64, OpenAL, ...), each held by a reference
until stop. A leaf in other code (LuaJIT traces, drivers) is recovered by a bounded scan (4 KB)
for a return address after a call instruction whose own frame also unwinds to one; frames after
a scan get extra frame-pointer and frame-extent checks. The walk stops at `U::MoveWorker::Run`.

**Phases.** When the stack reaches Run at its slot call (return address 0xef46b6, inside the
`vbi_dispatch` signature) the unwinder has restored Run's RBX, which holds the slot index
(0..16). After a scan, or elsewhere in Run, the frame below Run is matched against the slot
handlers' `Move()` functions, which the probe snapshots on the MoveWorker every frame (tail
calls and thunks then count as `frame`). `frame` is Run outside a slot, `idle` is Run waiting
(for `g_semSimStart`), `loading` is not walked, `lost`/`lostout` did not reach Run (depth cap
or unknown frame) inside/outside the slot 0/15 brackets. A sample is *waiting* when its leaf is
in an ntdll wait syscall stub (`NtWaitForSingleObject`, `...MultipleObjects`, `NtDelayExecution`,
`NtWaitForAlertByThreadId`, keyed events, I/O completion, `NtYieldExecution`).

**Safety.** No engine memory is written. The sampler stops (thread joined, 2 s limit, tables
leaked rather than freed if it does not exit) before any disarm step, on `stop()`/`__gc`;
DLL process detach only signals it (loader lock held; the module is pinned while armed). Five
consecutive `SuspendThread` or `GetThreadContext` failures (with 100..800 ms back-off) disable
the sampler, logged once as `FEATURE simprof DISABLED reason=...`; the probe keeps running.
A failed `ResumeThread` (after three retries) disables immediately.

**Log format** (debug log, every report window):
```
[X4LAN] FEATURE simprof ENABLED intervalUs=1000 depth=32 reportS=10
[X4LAN] SIMPROF window seconds=10.00 samples=9990 rateHz=999 depthMean=17.3 suspendUsMean=22.0 suspendUsMax=180.0 walkUsMean=2.5 overheadPct=2.20 truncated=12 scanned=40 lost=13 loading=0 failures=0 skipped=0 droppedFunctions=0 droppedStacks=0 foldPending=812
[X4LAN] SIMPROF phases slot3=61.0%(w4.0) idle=12.0%(w12.0) slot6=9.0%(w0.0) ...   share (waiting share) of all samples
[X4LAN] SIMPROF self 9fdef0=5.0% [ext]=4.0% ntdll!NtWaitForSingleObject=3.1% ...  top 20, share of samples
[X4LAN] SIMPROF incl ef4400=88.0% 122b360=61.0% ...                                top 20
[X4LAN] SIMPROF fold slot3;ef4400;122b360;9fdef0;3e4f40 17                         FlameGraph folded stack
```
`overheadPct` is the time the MoveWorker was held suspended, as a share of wall time. Frames:
bare hex = X4.exe function-start RVA (cold fragments map to their parent), `module+offset`,
`ntdll!Nt...` (wait stub), `[ext]` (unlisted code), `[truncated]`. Each window's folded stacks
are drained from a snapshot over the following frames (the active table keeps accumulating);
`dev/simprof_report.py` sums all windows. The drain runs in slot 6 (UI) and is visible there.

```
python dev/simprof_report.py x4lan-debug.txt --folded simprof.folded [--phase slot3] [--with-rva]
```
names RVAs from `../x4-re/labels/merged.tsv`, the other label TSVs and `symbols.csv` (`--x4re`),
prints phase shares and top lists and writes FlameGraph input (`flamegraph.pl`, speedscope).

**Synthetic validation** (`build.py --fixedtick --test`, `test_simprof.c`): a worker runs a fake
Run with an assembly slot dispatcher shaped like the engine's (RBX index, `call [rax+8]`),
a tail-called slot, a JIT leaf without unwind data, waits and an 80-deep recursion. Sampled
phase shares match the worker's own QPC measurements within 4 points; self/inclusive, waits,
scan recovery, truncation and folds are checked; injected `GetThreadContext`/`SuspendThread`
failures disable with a reason and never leave the target suspended. Overhead on this PC
(busy worker): at 1 ms the target is suspended 2.3-2.5% of wall time (about 23 us per sample,
of which the unwind is about 1 us; the rest is the kernel suspend/context/resume; rare maxima
up to 2 ms), throughput slowdown 1.6-3.9% (noisy, median of 7); 2 ms: 1.4% suspended; 0.5 ms:
3.8-4.5% suspended. Native overhead is logged as `overheadPct`.

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
