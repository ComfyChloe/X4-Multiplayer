# Experimental Windows engine observer and MoveWorker seed

Tasks: Stages 0–1 of `x4-re/DETERMINISM-PLAN.md`; GitHub issue #57.
Target: Windows X4 9.00, Steam build 23660954 only.

The dependency-free Rust crate is separate from the hook-free UDP transport. It
verifies the exact executable and all sites in `signatures.json`, then registers
an observer in MoveWorker VBI slot 12. Slot 7 remains available to the independent
simtick work. The module never patches executable bytes on disk or in memory.
Stage 0's only engine write was a verified-empty callback-slot pointer. Stage 1
additionally reseeds the current MoveWorker TLS RNG stream from the session seed
and the stable `MoveWorker` role at observer frame 0. Other streams, timestep,
attention, physics, objects and orders are unchanged.

Build outputs stay under ignored `.local/` and are not release packages:

```
python native/hooks/build.py --test
```

In an explicitly isolated development installation, load
`x4lan_hooks_64.dll` through `engine_hooks.lua`, not manually into a normal game.
Observation is enabled by default in the supported isolated profile. It requires
profile 45841, Protected UI off and a loaded disposable world. Wrong builds,
modified signatures, occupied slots, loading, foreign threads and lifetime changes
refuse or disarm observation. These paths log `HOOKS_DISABLED reason=...`.

`module.start(callback, cadence)` returns an owning userdata or `false, reason`.
Retain the userdata. The callback receives
`(frame, gameTime, dt, moveWorkerRngHex, sessionSeedHex)`. Frames count observer
callbacks after registration, including paused frames; they are not save-persistent
engine frame numbers. `handle:active()`, `handle:stop()` and `handle:frames()`
inspect or end registration. `module.inspect(decimalComponentId)` returns read-only
registry generation, attention, absolute hull, evaluated shield percentage and
owner pointer while armed on the owning MoveWorker thread.

`reports/determinism-stage-0.md` and `determinism-stage-1.md` record the native
read-only launches and the limited seeded comparison. The initial RNG matches, but
draw sequences diverge again at frame 2; this is not full Stage 1 acceptance.
Linux RVAs, hook behavior and acceptance are a separate later task; none of these
Windows addresses may be reused on the Linux executable.
