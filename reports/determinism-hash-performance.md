# Deterministic background simulation — hashing cost controls

25 October 2026 | Offline implementation; no X4 launch or installation/profile/save change
Branch: `contrib/ferrite-hash-performance`; base `f71ca62`.

## Change

- The default observer cadence is now 30 instead of 1. `engine_hash_cadence=1`
  remains supported for deliberate complete-capture diagnostics.
- `engine_hash_budget_ms` defaults to 8 ms (configurable from 0.1 to 1000). The
  recorder checks QueryPerformanceCounter wall time between ship inspections and
  after capture and hashing. Over-budget work is abandoned without a digest.
- Every new complete STATEHASH record has `fresh=1 complete=1`. Failed or partial
  attempts have `fresh=0 complete=0`, scanned/sampled counts and no hash fields.
  They are never equality evidence. The comparator rejects contradictory freshness
  and completeness while retaining compatibility with older records lacking
  `fresh`.
- HASHCOST records contain per-attempt capture/hash/total milliseconds, scanned and
  sampled counts, cumulative attempts/completions/failures/time, maximum time,
  cadence and budget. These are QPC wall-time measurements, not STATEHASH game-time
  `gt` or `dt` values.
- Dependency-free Rust now performs the existing FNV-1a row hashing instead of the
  LuaJIT byte loop and supplies QPC wall time. Hash contents and newline framing are
  unchanged. No C code, new crate dependency or background thread was added.
- Exact executable/signature refusal, observer thread/lifetime guards and the
  Stage 1 frame-0 MoveWorker-only reseed are unchanged. Physics, attention and
  relay protocols are outside this change.

## Bound and remaining hot-path work

This is a cooperative cost guard, not preemptive scheduling. One engine API call,
one faction allocation/enumeration or the final sort/hash phase can individually
exceed the budget before the next check. An over-budget completed hash is discarded
and logged incomplete, so cost overruns do not weaken comparison correctness.
Complete captures are retried from scratch at a later cadence; no stale partial
state is reused.

Faction buffers, ship row tables, formatted strings, three bucket arrays and sort
work are still allocated per attempt. Enumeration and native inspection remain on
MoveWorker. There is no incremental scan or claimed native FPS improvement. If
measurements show the guard aborting too often, the next work should reduce row and
string allocations or introduce explicitly freshness-tracked incremental captures;
it must not label a partial/stale digest complete.

## Offline validation

- `cargo fmt --check --manifest-path native/hooks/Cargo.toml` — PASS.
- `cargo test --manifest-path native/hooks/Cargo.toml` — 2 passed, 0 failed;
  includes FNV known answers, monotonic QPC and the existing observer lifecycle.
- `python native/hooks/build.py --test` — release DLL built under `.local/evidence-052-withheld`;
  the same 2 Rust tests passed.
- Real LuaJIT/Lupa 2.8 focused tests: `test_engine_hooks.py` 11 passed and
  `test_statehash.py` 6 passed. Coverage includes default/override cadence, wall
  cost counters, capture and hash budget refusal, incomplete freshness, recovery
  with a later fresh complete capture, stable hashes and comparator rejection.
- Adjacent regressions: `test_divergence_probe.py` 22 passed and
  `test_ship_spec.py` 10 passed. Temporary test outputs were not retained as
  packages.
- Independent WSL Ubuntu 24.04 validation with Lupa/LuaJIT 2.8 ran all 682 tests:
  681 passed, no skips, with only the pre-existing world-launcher failure that
  expects `content mismatch` but receives a timeout. The same failure reproduces
  on the base. No hashing or adjacent-suite failure was observed.

No native X4 run was performed, so native wall-time distributions, complete-capture
rates at the default 8 ms budget and gameplay performance remain unmeasured. The
standalone Lupa process is not ABI-compatible with the game's Lua DLL resolver;
it is not evidence for or against the native module's in-game behavior.
