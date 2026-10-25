# Deterministic background simulation — Stage 0 evidence

25 October 2026 | Windows X4 9.00, Steam build 23660954
Branch: `contrib/ferrite-engine-hooks`; base: `7c6fc419c282179bc553e77c4aa48ec19715a520` plus the Stage 0 changes in this branch. Task record: GitHub issue #57.

## Implemented

- `dev/check_hook_signatures.py` validates the full Windows executable identity and 14 file-backed hook-site SHA-256 values. `native/hooks/signatures.json` contains addresses, lengths and hashes only, not game bytes.
- A dependency-free Rust `cdylib` in `native/hooks/` registers a read-only MoveWorker VBI slot-12 observer. It checks the exact executable, PE identity and runtime signatures before registration. `x4lan_net` is unchanged.
- `engine_hooks.lua` is enabled by default in the supported isolated profile. It retries transient loading, emits canonical identity/pose/health/RNG/time buckets and marks incomplete captures. Source multiplayer/session settings remain disabled and credential-free.
- `dev/compare_divergence.py --statehash` aligns observer frames, reports the first differing bucket, and rejects mixed, malformed, regressing or inconsistent streams. Missing and incomplete frames are not equality evidence.
- Future internal Windows package assembly builds/includes the Rust DLL; Linux excludes it. No package was built or sealed for this task.

The only engine write is atomic ownership of a verified-empty callback-slot pointer. Stage 0 does not alter RNG, timestep, attention, physics, objects, orders or network authority. Exact-build/profile guards remain mandatory even though observation is default-enabled.

## Offline and synthetic validation

Commands run from this worktree except the full suite, which ran in WSL Ubuntu 24.04 with Python 3.12 and Lupa/LuaJIT 2.8.

- `python dev/check_hook_signatures.py` — PASS against both `x4-re/bin/X4.exe` and the installed Windows `X4.exe`; exact build and 14 signatures.
- `python native/hooks/build.py --test` — release DLL built with Rust 1.94.1/MSVC; two Rust tests PASS, no ignored tests. Covers build rejection, registration lifetime, cadence, thread/loading guards, callback arguments, ID parsing and teardown.
- `cargo fmt --check --manifest-path native/hooks/Cargo.toml` — PASS.
- Seven signature tests, eight Lua recorder tests, six STATEHASH tests, 22 existing divergence tests and ten ship/package tests — all PASS.
- Full `.venv/bin/python dev/check.py`: 679 run, 678 PASS, no skips, one failure. `test_failed_native_capture_is_reported_and_retry_retains_evidence` expects `content mismatch` but receives the launcher timeout. The same single failure reproduces on unchanged base `7c6fc41`; it was not hidden or waived.

Synthetic tests do not establish native ABI behavior. Native evidence is listed separately below.

## Native read-only runs

The original owned guest extension and test settings were backed up before installing development files. X4 was closed for every file change and closed normally after each run. Profile 45841 retained `autosave=false` and `uisafemode=false`. Normal profiles, saves and Workshop settings were not touched.

Initial runs found and corrected two startup issues: the observer now sends its own MD readiness request when networking is disabled, and a native `loading` refusal is retried instead of becoming terminal. The first successful new-world run then proved slot-12 dispatch and complete STATEHASH output. Seed-admission runs A and B subsequently loaded the same owned `save_010` and emitted complete per-frame records.

Both matched runs reached `SEED_GATE READY` and `HOOKS_READY`. `HOOKS_OBSERVE` showed a live component with generation 0, attention 2, absolute hull 4000, shield 0, a nonzero owner pointer, MoveWorker RNG state and game time/dt. The complete samples contained 522 non-player-owned ships in the configured sectors and zero weak local identities. These readbacks agree with the static field layout; they are not a semantic proof for every object class or a full registry generation audit.

Run A contained 37 frames and run B 27 frames at shutdown. The comparator aligned 27 complete frames; the ten run-A-only tail frames were reported as missing, not treated as equal. Result:

```
FIRST DIVERGENCE frame=1 game_time=6.069319300 elapsed=0.000000000
buckets=identity,pose,health,rng,time
```

This is the Stage 0 baseline: divergence is already observable at the first shared observer frame. It does not show that the games were ever deterministic. First-frame identity/health differences also include different world state at observation time; no narrower cause is inferred from bucket hashes alone.

## Overhead and limits

Cadence 1 sampled hundreds of ships and called native inspection for each. Typical later-frame game-dt values were about 0.78–0.84 seconds with the inherited test `gamespeed=0.5`. This shows a large sampling cost, but wall-time overhead was not independently instrumented, so it is not a controlled FPS benchmark. Long baseline runs and production-cadence choices require cheaper enumeration/hashing and explicit wall-time measurement.

Stage 0 used one Windows PC, sequential runs and one save. It did not test simultaneous instances, mixed platforms, Linux hooks, all sectors, player-owned background ships, wrong-build native refusal, long-duration stability or desync repair. Rust tests reject a non-X4 process; offline checks reject wrong files. No Stage 1+ behavior-changing hook was installed.

Raw logs, backups, the development extension and evidence remain under `.local/evidence-117-withheld`, outside Git. The comparison is `.local/evidence-123-withheld`. No private session config or credentials were added to the report or repository.

## Cleanup and gate decision

The original owned guest extension, package ID, config/content settings and previous log were restored. Save inventory, bytes and actual timestamps match the pre-run manifest; no test save was created (`.local/evidence-124-withheld`). X4 is closed.

**Stage 0 gate: PASS for read-only tooling and an initial baseline, not for deterministic gameplay.** Continue with narrowly scoped Stage 1 RNG seeding and non-physics divergence analysis. Physics-tick work belongs to the separate existing task and is not duplicated here.
