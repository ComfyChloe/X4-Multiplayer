# Experimental Windows engine observer, MoveWorker seed and shared attention

Tasks: Stages 0–1 and Stage 3 (shared attention) of `x4-re/DETERMINISM-PLAN.md`;
GitHub issue #57. Physics-tick work is separate.
Target: Windows X4 9.00, Steam build 23660954 only.

The dependency-free Rust crate is separate from the hook-free UDP transport. It
verifies the exact executable and all sites in `signatures.json`, then registers
an observer in MoveWorker VBI slot 12. Slot 7 remains available to the independent
simtick work. The module never patches executable bytes on disk or in memory.
Stage 0's only engine write was a verified-empty callback-slot pointer. Stage 1
additionally reseeds the current MoveWorker TLS RNG stream from the session seed
and the stable `MoveWorker` role at observer frame 0. Other streams, timestep,
physics stepping, objects and orders are unchanged.

Shared attention (`src/attention.rs`) atomically replaces the verified
ComputeAttention pointer (vtable slot 703) in all 51 primary vtables whose result
depends on the player: Zone/Highway, Sector, Cluster, seven Ship classes, five
Controllable classes (Station, Container, Defensible, BuildStorage), 27 Object
classes (asteroids, mines, satellites, collectables, ...), Gate, four Room classes
and three WalkableModule classes. Each wrapper calls the original and raises, never
lowers, its result with the engine's own player rules applied to up to eight remote
proxies: cluster 3, sector 5 (4 at the end of a superhighway), zone 8 or adjacent
7/6, objects 9 inside the source's parent chain or within their attention radius in
an active zone, rooms 11/10, and the walkable-module 1 km gate. The engine's own
transform, distance, adjacency, defaults and connection functions supply the inputs.
When the local player or any remote source changes zone, sector or cluster, the
module recomputes the old and new cluster, sector and zone and the sectors/zones
below them through SetAttention, identically in both games. Transitions still run
through SetAttention; the module never writes Component+0x1ec.

The module also wraps `U::Sector::Move` (slot 674). Below attention 5 the engine
reschedules the sector's periodic pass at `now + 5 + FloatState(TLS+0x318, 1)`.
When exactly that one generator step happened, the wrapper restores the MoveWorker
RNG state and writes `now + 5 + j`, where `j` is seeded from the session seed, the
sector's static position and the previous due time. Any other draw pattern is left
unchanged and counted as `jitter_skipped`.

Not covered: local render cameras (`Universe+0x2c0`; `camera_offchain` in
`HOOKS_STATUS` counts cameras anchored outside the local player chain), the
Positional room-root level 10 of station modules, Ship physics-body eligibility's
250 km player-distance fallback, cutscene anchors, and the per-object attention
refresh timer (`0.75 + FloatState(0.5)` s), which stays on the MoveWorker stream.
See `reports/determinism-attention-parity.md`. This is not native evidence.

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

`module.set_remote_attention_sources({id, ...})` registers up to eight distinct
remote ship proxies (an empty table clears them); `module.set_remote_attention(id)`
is the single-proxy form (`"0"` clears). Every use resolves IDs through the
generation-checked engine registry, so removal or generation reuse withdraws remote
attention. The Lua adapter publishes `state.proxy` and every ID in `state.proxies`
and does not change motion or relay messages. `module.attention_level(id)` reads
Component+0x1ec of any component and `module.attention_status()` returns hook
counters; both are read-only.

Every complete `STATEHASH` carries an `attention` bucket: sorted rows
`sector|identity|kind|level` for ships (`s`), stations (`t`) and the sampled sectors
themselves, plus `attn_rows` and a level histogram `attn_levels` (levels 0..11). The
combined `h` includes it. `engine_hash_attention_rows = true` additionally logs every
row as `ATTNROW`, so `dev/compare_divergence.py --statehash` can name the objects whose
level differs at the first attention mismatch.

In-memory verification treats 8-byte vtable sites as relocated pointers: X4.exe has
`DYNAMIC_BASE` and the loader rewrites them under ASLR, so they are compared after
removing the load delta. Code sites contain no relocations.

`reports/determinism-stage-0.md` and `determinism-stage-1.md` record the native
read-only launches and the limited seeded comparison. The initial RNG matches, but
draw sequences diverge again at frame 2; this is not full Stage 1 acceptance.
Linux RVAs, hook behavior and acceptance are a separate later task; none of these
Windows addresses may be reused on the Linux executable.
