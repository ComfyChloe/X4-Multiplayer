# Experimental Windows engine observer, MoveWorker seed and shared attention

Tasks: Stages 0–1 and the ship/zone slice of Stage 3 of
`x4-re/DETERMINISM-PLAN.md`; GitHub issue #57. Physics-tick work is separate.
Target: Windows X4 9.00, Steam build 23660954 only.

The dependency-free Rust crate is separate from the hook-free UDP transport. It
verifies the exact executable and all sites in `signatures.json`, then registers
an observer in MoveWorker VBI slot 12. Slot 7 remains available to the independent
simtick work. The module never patches executable bytes on disk or in memory.
Stage 0's only engine write was a verified-empty callback-slot pointer. Stage 1
additionally reseeds the current MoveWorker TLS RNG stream from the session seed
and the stable `MoveWorker` role at observer frame 0; the RNG-streams step (below)
also seeds the worker and job-thread streams once each. Timestep, physics stepping,
objects and orders are unchanged.

The shared-attention slice atomically replaces the verified ComputeAttention
pointer in two Zone and seven Ship primary vtables. The wrappers call the original
engine function and never lower its result. A live remote proxy raises its
containing Zone to level 8. The proxy and ships within its engine-computed
attention radius in an active directly-parented Zone can reach level 9. The
engine's transform, radius and squared-distance functions supply the proximity
result. Actual transitions still run through the engine's SetAttention path; the
module never writes Component+0x1ec. This does not claim camera/adjacent-zone
parity, deterministic physics or matched RNG sequences.

Build outputs stay under ignored `.local/` and are not release packages:

```
python native/hooks/build.py --test
```

In an explicitly isolated development installation, load
`x4lan_hooks_64.dll` through `engine_hooks.lua`, not manually into a normal game.
Observation and shared attention are enabled by default in the supported isolated
profile. They require profile 45841, Protected UI off and a loaded disposable
world. Wrong builds, modified signatures, occupied slots/vtables, loading, foreign
threads and lifetime changes refuse or disarm the module. These paths log
`HOOKS_DISABLED reason=...`. Disarm clears the proxy ID and restores only vtable
slots still owned by this module; foreign replacements are left untouched.

`module.start(callback, cadence)` returns an owning userdata or `false, reason`.
Retain the userdata. The callback receives
`(frame, gameTime, dt, moveWorkerRngHex, sessionSeedHex)`. Frames count observer
callbacks after registration, including paused frames; they are not save-persistent
engine frame numbers. `handle:active()`, `handle:stop()` and `handle:frames()`
inspect or end registration. `module.inspect(decimalComponentId)` returns read-only
registry generation, attention, absolute hull, evaluated shield percentage and
owner pointer while armed on the owning MoveWorker thread.
`module.walltime()` returns QueryPerformanceCounter seconds and `module.hash(rows)`
hashes an array of strings with FNV-1a, including one newline after every row.
Neither utility accesses engine state; both run synchronously on their Lua caller.

The recorder defaults to cadence 30 and an 8 ms QPC wall-time capture/hash budget.
`engine_hash_cadence` (including 1), `engine_hash_budget_ms` and
`engine_hash_max_ships` remain configurable. Every complete `STATEHASH` is a fresh,
complete capture (`fresh=1 complete=1`). A capture exceeding the budget between
engine calls, or during the measured phases, instead logs `fresh=0 complete=0`
without hashes. `HASHCOST` reports per-attempt and cumulative QPC milliseconds and
counts; its timings are wall time, unlike STATEHASH `gt`/`dt` game time. Individual
engine calls cannot be preempted, so the budget is a cooperative guard, not a hard
real-time deadline. See `reports/determinism-hash-performance.md`.

`module.set_remote_attention(decimalComponentId)` registers the remote ship proxy;
ID `0` clears it. Every use resolves the ID through the generation-checked engine
registry, so removal or generation reuse withdraws remote attention. Registration
changes recompute the old and new proxy/Zone through SetAttention(1), including
already-present proxies in low-attention Zones. The Lua adapter publishes the
existing NAV2 proxy's current ID and does not change motion or relay messages.

Role-keyed RNG streams (`src/rng.rs`, `reports/determinism-rng-streams.md`): at
registration the module verifies 29 further pinned sites, seeds MoveWorker with the
unchanged Stage 1 value and binds PathingManager, MassTraffic, RegionManager,
Gravidar and every JobThread through their engine singletons and the thread object at
TLS+0x320. Each of those streams is written once, to `H(session seed, role)`, at the
first observer frame where its thread provably cannot draw (after the pathing join,
under the worker's own handshake lock with `ready` set, or under the job-manager lock
with an empty queue and no running job). Unbound roles are never written. Because the
LCG step is a bijection, draw counts since seeding are exact. `module.rng_streams()`
returns the per-role counters, MoveWorker phase split and per-frame MoveWorker draws;
`module.rng_phases()` optionally places two read-only phase markers in verified-empty
VBI slots 2 and 5 (`engine_rng_phases=false` disables them). The Lua adapter logs
`RNGSTREAMS`/`RNGPOOL` with every sample and `dev/compare_divergence.py` reports the
first role, phase and exact tick whose draw counts differ.

`reports/determinism-stage-0.md` and `determinism-stage-1.md` record the native
read-only launches and the limited seeded comparison. The initial RNG matches, but
draw sequences diverge again at frame 2; this is not full Stage 1 acceptance.
Linux RVAs, hook behavior and acceptance are a separate later task; none of these
Windows addresses may be reused on the Linux executable.
