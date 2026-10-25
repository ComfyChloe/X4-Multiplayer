# Deterministic background simulation — Stage 1 RNG evidence

25 October 2026 | Windows X4 9.00, Steam build 23660954
Branch: `contrib/ferrite-engine-hooks`, following `reports/determinism-stage-0.md`.

## Change

The Rust observer now reads `g_SessionSeed` at the build-pinned address and includes it in `HOOKS_OBSERVE` and STATEHASH metadata. At successful observer registration on the MoveWorker thread, it replaces only that thread's TLS RNG state with FNV-1a-derived `H(session seed, "MoveWorker")`. Registration still requires the exact executable, all runtime signatures, the isolated profile and valid MoveWorker TLS.

This is a frame-0 reseed, not a thread-start hook for every engine stream. RegionManager, MassTraffic, Gravidar, Pathing and job-thread role mapping/seeding remain unimplemented. Physics-tick work is outside this task.

The comparator now validates seed compatibility and reports first divergence plus mismatch counts for every bucket, rather than only the first overall mismatch.

## Validation

- Rust release build and two native unit tests PASS, including role-seed determinism/separation and five-argument callback delivery.
- Eight Lua recorder tests and six STATEHASH tests PASS.
- The full Stage 0 validation is recorded in the previous report; no gameplay suite result is reused as Stage 1 native evidence.

Two sequential native runs (C and D) used the same owned `save_010`, the same development extension and `-seed:123456789`. Both loaded through the verified seed-admission path and produced complete STATEHASH streams.

The logged session seed in both runs was `00000000075bcd15` (123456789). **The `-seed` value therefore survives this save-load path.** Both runs also began with MoveWorker RNG `28acd4a7fac17879`, confirming the deterministic role reseed.

## Comparison and limit

Command:

```
python dev/compare_divergence.py .local/evidence-119-withheld \
  .local/evidence-120-withheld --statehash \
  --report .local/evidence-125-withheld
```

There were 22 complete shared observer frames and 18 unmatched tail frames. Unmatched frames are warnings, not equality evidence.

```
FIRST DIVERGENCE frame=1 buckets=identity,pose,health,time
BUCKET identity first=1 differences=22/22
BUCKET pose    first=1 differences=22/22
BUCKET health  first=1 differences=22/22
BUCKET rng     first=2 differences=21/22
BUCKET time    first=1 differences=22/22
```

Stage 0's RNG bucket differed at frame 1. Stage 1 aligns the initial RNG value and delays that particular mismatch until frame 2. This is an improvement over the baseline, but **not deterministic RNG and not a full Stage 1 acceptance pass**. Different update/attention sequences consume different numbers of draws after registration. Identity, pose, health and time also differ at the first observation, so no claim is made that seeding repaired the universe.

## Next non-physics work

Static attention review identifies `U::Zone::vfunc703` at `0x843b20`, `U::Ship::ComputeAttention` at `0x7d5110`, and object proximity at `0x6d20b0` as separate relevant paths. The local player chain receives high attention; an NPC near only the remote player can remain in reduced simulation. Zone level 8 alone is insufficient because detailed ship updates require level 9 and physics-body eligibility has additional conditions. Do not write `Component+0x1ec` directly: that bypasses transition events and is overwritten by recomputation. A shared-attention patch must cover zone, ship/proximity and body transitions, then verify AI attention bodies and RNG draw equality in different sectors.

A per-frame reseed could bound long-term stream drift but would not make different draw sequences equivalent and must not be presented as a determinism fix. Map the remaining thread roles before seeding other streams. Coordinate fixed-timestep/barrier work with its existing owner rather than duplicating it here.

The original owned guest extension/settings/log were restored after the runs. Save inventory, bytes and timestamps remain unchanged and X4 is closed. Raw evidence stays under `.local/evidence-117-withheld`; no package was built or sealed.
