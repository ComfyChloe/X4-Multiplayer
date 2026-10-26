# Native combat census, 26 October 2026

The corrected pass-through census completed natively on lane b1 at 03:53:20 EDT,
source `36d858b`. It recorded 2,748 complete frames during the battle fixture's
97.678-game-second window (71.5241 to 169.202, ended `decided`). The original engine
remained authoritative. All vtable hooks stopped normally and x4lane restored the lane.

Evidence: `.local/evidence-077-withheld` contains `result.json`,
`x4lan-debug.txt`, `profile-files/x4mp-combatcensus.txt` and `census-summary.json`.
Capture SHA-256: `0d8d7f09f051b2a324e526e312a650200bde2f0e29070ebca95bc5c8697a7f26`.
The strict analyzer checks the table's version, columns, complete row/frame count,
finite values and clock calibration. Its totals match the native final line.

## Counts and coverage

- 4,042,950 weapon Move calls: 1,078,162 Weapon, 2,674,496 Turret, 279,300
  MissileLauncher and 10,992 MissileTurret. The class totals equal all weapon calls;
  no nested weapon calls were observed.
- 20,042 bullet FireShot calls and 49 missile FireShot calls.
- 942,086 bullet Move calls, 436,310 flight checks and 3,264 hit-counter increments.
- 194,960 RNG steps inside outermost weapon calls and 188,094 inside outermost bullet
  calls, out of 2,185,855 measured owner-thread steps between frame boundaries:
  17.5242% combined. All 2,748 frames have complete RNG counts.
- Zero dropped frames, unmatched call RNG counts, over-cap frame RNG counts,
  foreign Move calls, uninitialized TLS observations or failure markers.

The hooks count all owner-thread combat calls, including ambient world activity;
they do not isolate only the 87 ships spawned by the battle fixture. FireShot calls
are not an independent proof that every requested projectile was created.

Observed per-frame distributions useful for sizing experiments:

- Bullet FireShot calls: mean 7.293, p95 17, p99 25, maximum 62.
- Missile FireShot calls: mean 0.0178, p95 0, p99 1, maximum 2.
- Flight checks: mean 158.774, p95 253, p99 304, maximum 477.
- Hit increments: mean 1.188, p95 4, p99 7, maximum 21.

These are observed loads, not safe fixed-capacity bounds. Larger battles and different
weapons can exceed them; a deferred buffer needs explicit capacity/fallback handling.
No event order or parallel-execution equivalence is established by the census.

## Primary target correction and retained failed evidence

The first run, `census-battle-codex-001` on b4, completed and restored at 03:44:27.
Its 1,938 frames and RNG/call counts remain evidence, but its primary-target field
was invalid: all target counts were zero despite active aiming and firing.

Offline inspection of the pinned executable found the error: primary-target getter17
at 0x7e5d30 reads controller+0x10, while the census read +0x18. Commit `36d858b`
corrects the read and tests it against the actual mapped getter with deliberately
different values at the two offsets. Corrected tables carry
`primary_target_offset=16`; the analyzer will not reinterpret older captures' target
ratio as valid.

Run002 records 557,955 weapon calls with a primary target reference, mean 203.04
per frame. The complementary 86.1993% is reported as `no_primary_target_share`,
not an idle-weapon fraction. Secondary selection in MultipleShootController and
untargeted firing are outside that field's classification.

Fresh-bullet counters remain timestamp proxies: last-check == shot-time can persist,
and shot time includes salvo delay/jitter. Run002's 15,492 fresh-classified calls and
14,522 calls passing the newer-shot-time predicate do not prove unique first moves
or same-frame creation/traversal. A creation-to-Move identity trace is still needed
before using this to justify deferred bullet creation.

## Validation and limits

For `36d858b`: three synthetic Rust census tests and three mandatory mapped-original
tests passed with `X4MP_REQUIRE_ORACLE=1`; 39 combat Python/LuaJIT tests passed without
skips. The release DLL built successfully, then the corrected native run passed the
expected enable/write/stop/cleanup markers and semantic checks above. Independent
review found no additional actionable issue in the offset/metadata change.

Both runs had `timing_trustworthy=false`; elapsed instrumentation timings are not
accepted optimization evidence. This validates bounded instrumentation and its
counts in a native game, not multiplayer combat, a replacement, or a parallel walk.
