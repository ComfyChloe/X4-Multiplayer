# Deterministic background simulation: role-keyed RNG streams

25 October 2026 | Windows X4 9.00, Steam build 23660954 only
Branch: `contrib/ferrite-rng-streams` (from `contrib/ferrite-engine-hooks` 4b2458f).
Follows `reports/determinism-stage-1.md`.

**Status: offline only.** Nothing here has run in a native game. Every result below comes
from static analysis of `x4-re/bin/X4.exe`, synthetic unit tests or the comparator applied to
synthetic logs. The single native data point quoted (150 draws) re-reads values that the
Stage 1 report had already logged.

## 1. Stream map (Windows)

Every engine thread started through `XLib::ThreadEntryTrampoline` (0x14b7740) owns one u64
state at TLS block + 0x318. The trampoline stores the thread's `XLib::Thread` object at
TLS + 0x320, then `g_ThreadStartHook` → `X4::ThreadStartHook` (0x96350) →
`Random::SeedThreadState` (0x14dd110) sets the state to two steps of `_time64(0) + tid`.
The step is `s' = ror64(s·0x5851F42D4C957F2D + 0x14057B7EF767814F, 30)`, one step per draw
(0x14dde20, 0x14ddc90). It is a bijection, so draw counts are recoverable from states.

`GameClient::Init` creates every simulation thread **once per process**: 0x1226734 starts
Gravidar, Pathing, MassTraffic and RegionManager; MoveWorker comes from 0xef4390 and the
JobThread pool from 0x14bcf80. They are never restarted on load, so their streams carry
wall-clock and thread-id state plus every draw made since process start, including loading.
The module loads long after these threads exist, so the thread-start hook cannot be used.

Thread object layout (0x14b7680, high confidence): +0 exit code, +8 handle, +0x10 thread id,
+0x14 started flag. In every case `runnable+8` points at the thread object.

| Role (seed key) | Engine binding | Safe seeding point | Draw sites (examples) | Confidence |
|---|---|---|---|---|
| `MoveWorker` | `g_MoveWorker` 0x38f4ee8, vtable 0x2c49b00, thread at +0x18; names itself "Movement worker" | Own thread at observer registration (unchanged Stage 1) | MD/AI interpreter `chance` (0xb58bb0, 0x111c290), random ranges (0xc58da0, 0xca5d20), combat (0x606f20, 0x81b230, 0xece1b0), loadouts and drops (0x505fb0, 0x548f90), Sector::Move jitter (0x784230), BodyClass::Move (0x14de000), JobEngine slot 11 (0xf03e40 and 9 further). Direct reach: 36 RNG users from GameClient::Move (slot 3), 10 from JobEngine (slot 11), 0 from Ticker (slot 1) | binding high; draw list medium (lower bound, since virtual calls are invisible) |
| `PathingManager` | `g_PathingManager` 0x6cf21c8, vtable 0x2c4f560, thread at +0x20; "Pathing Manager" | Slot 12, after the universe-update join (`+0x88 == 1`, state `+0x84 == 2`). The thread only signals and loops after the join (0xeac107) | No direct draw from Run or the pass (0xeae3c0). The pass **queues JobThread jobs** (0xeae942, thunk 0xf29cd0, payload 0xeaeb60 → 0xeaecc0). Steering FCM `Update` (vfunc14, 0xec7200) and NavigationController vfunc33 (0xea77f0) draw from `&TLS+0x318`, but their executing thread is unresolved | binding high; draws low |
| `MassTraffic` | singleton 0x6cf23e0 (ctor 0x8c0450), vtable 0x2b96c70, thread at +0xb0; "MassTrafficUpdateTask" | Under its CS +0x60 while `ready` +0x88 == 1. The worker sets `ready` before waiting; MoveWorker clears it when kicking at frame end (0xef4796) | 0x8a65b0, 0x8b4880, 0x108e660, weighted template pick 0x8a7040 | binding/gate high; draws medium |
| `RegionManager` | singleton 0x6cf21e0 (ctor 0xef2bb0), runnable at +0x20 (vtable 0x2c49cd0), thread at +0x168; "RegionManager" | Under CS +0x118 while `ready` +0x140 == 1 (same handshake as MassTraffic, 0xef315a) | 0xee7290 → 0xede1f0, component factory path 0x6bb620 / 0x551fc0 / 0x3cf0f0 | binding/gate high; draws medium |
| `Gravidar` | singleton 0x6cf1650, vtable 0x2c68860, thread at +0x28; "Gravidar" | Under CS +0x6960 while `ready` +0x6988 == 1. It is kicked only from `PublishResults` (0x10b12c0) at MoveWorker frame start, and only when finished | No direct draws found from Run (depth 8). 0x10acd70 has an inline LCG, but its thread is unresolved | binding/gate high; draws low |
| `JobThread/NN` | `g_JobThreadManager` 0x6cf1540. Count +0x528 (cores−2, clamped 4..32). Records at +0x28+i·0x20 (vtable 0x2cabca8, +0x10 manager, +0x1c index; 0x14bbfc0, 0x14bcd25). Thread pointers at +0x428+i·8 | Under manager CS +0x540 while the queue (+0x588) is empty and every record's running category (+0x18, from job +0xac) is 0 | Jolt (0xc5a80), pathing agent batches, construction sequences (0xc24970), 0x76af50, 0x7801c0, 0x1049090, 0x9e5fc0. Draws inside jobs are not statically resolved | binding high; busy flag medium (every resolved QueueJob caller passes category 1 or 2; two do not resolve statically); draws unknown |
| main thread | seeded by 0x96350 and again by 0x14dd0e0 | not seeded | loading-mode VBI handlers, session-seed fallback 0x975c20, render | medium |
| other XLib threads | Rendering, NavMesh/Octree/Nav-spline calculation tasks, VideoAsset, Async File Saver, asset enable/cleanup, DLC/Workshop sync, OpenTrack, Network Engine | not seeded | none found directly for the three nav tasks | low |

Explicit-state streams (GodEngine, SpawnSource/NPC seeds, construction state, hash-local
streams) are unchanged. They follow from the session seed, the save and object IDs
(`x4-re/coord/reports/rng.md` §1–3).

## 2. Implementation

New module `native/hooks/src/rng.rs`. `lib.rs` gains only registration hooks:
`mod rng;`, `rng::arm` replacing the inline MoveWorker write, `rng::tick` once per observer
frame, `rng::after_callback`, `rng::disarm` and `rng::register_lua`.

- **Guards.** `arm` runs only after the existing exact-build check and verifies 29 additional
  pinned sites, also listed as separate entries in `signatures.json`. They cover the LCG
  step, thread object/trampoline layout, singleton constructors and starts, each worker
  handshake, the MoveWorker frame-end kick, the pathing join and tail, the JobThread
  run/pool/record layout and the six Run vtable slots.
  - Any mismatch refuses registration (`HOOKS_DISABLED`).
  - Per role, binding requires: the singleton pointer is readable; the runnable vtable equals
    the pinned RVA; `runnable+8` equals the expected thread object; the thread has started;
    the thread id resolves through `NtQueryInformationThread` → TEB+0x58 →
    TLS slot 0; and `TLS+0x320` equals that thread object.
  - A role that fails is flagged `u` and is never written. `TLS+0x320` is rechecked before
    every access.
- **Seeding.** Seeds are `H(session seed, role)` with FNV-1a and the role keys above, applied in
  fixed order (MoveWorker, PathingManager, MassTraffic, RegionManager, Gravidar,
  JobThread/00..). Each stream is written once, at the first observer frame on which its
  gate holds. That frame is logged per role.
- **Counting.** Each quiescent observation walks forward from the last known state, capped at
  2^21 steps per observation. A count beyond the cap is flagged `l` (lost), never guessed.
  For each role the module also counts passes (changes of TLS+0x300) and the busy state.
- **MoveWorker phases** (optional). Two read-only markers in verified-empty VBI slots 2 and 5
  split each frame's MoveWorker draws:

  | phase | covers |
  |---|---|
  | `a` | slots 13–16, the pre-pass, and slots 0–1 (Ticker) |
  | `b` | slots 3–4 (GameClient::Move/universe update, input) |
  | `c` | slots 6–11 (UI .. JobEngine) |
  | `o` | this module's own callback |
  | `x` | unattributed |

  The module also records per-frame MoveWorker draw totals for the exact tick. Markers are
  removed on disarm. `engine_rng_phases=false` disables them; an occupied slot only
  disables the split.
- **Logs.** With every sample, `engine_hooks.lua` logs:
  - `RNGSTREAMS run= session= observer= v=1 frame= seed= mw=steps:state:passes:flag:seedframe mw_a.. mw_x phases= mw_tick0= mw_ticks= path= mt= rm= grav=`
  - `RNGPOOL ... frame= pool= pool_steps= jNN=...`

  Both lines are logged for incomplete captures too. Flags: `u` unbound, `p` pending seed,
  `q` counted this frame, `b` busy (last exact value kept), `l` lost.
- **Comparator.** `dev/compare_divergence.py --statehash` appends an RNG section when both logs
  contain RNGSTREAMS lines; `--rng` reports only that section. It reports:
  - the first divergent role and the draws in that window
  - the first divergent MoveWorker phase
  - the exact tick
  - busy and lost counts, pool-size mismatch and seed-frame mismatch
  - equal counts with different states (which indicates a foreign write or a different seed)

  `--derive-rng-steps` recovers MoveWorker draw counts from the `rng=` field of existing
  Stage 1+ STATEHASH logs.

**Per-call-site counters were not implemented.** About 116 functions inline the LCG and 135 call
the TLS wrappers. Counting per site would require patching code, which this module
deliberately never does. The phase split, the exact tick and per-role counts are the feasible
granularity.

## 3. Offline validation

- **Rust** (`python native/hooks/build.py --test`): 8/8 PASS, 4 of them new.
  - The engine LCG and seed vector.
  - Gated seeding: busy streams are never written, and the role seeds appear exactly at the
    first quiescent frame.
  - Exact counts, pass detection, busy/lost flags, and refusal of a foreign `TLS+0x320`.
  - Refusal of a wrong vtable, an unstarted thread, a foreign TLS owner, a bad pool index or a
    bad pool count, plus the wrong-build signature refusal.
  - The phase split, including rollback when a slot is occupied.
- **Release DLL** builds to `.local/evidence-053-withheld`.
- **Signatures:** `python dev/check_hook_signatures.py ../x4-re/bin/X4.exe` passes the exact
  build and 56 signatures (27 existing + 29 new). `--manifest-only` also passes.
- **Python:** `tests/test_rng_streams.py` 13/13 PASS (LuaJIT available). It covers:
  - Rust/manifest signature sync.
  - Python/Rust LCG equality.
  - Lua logging, the split pool line, refusal paths and the config switch.
  - Comparator role/phase/tick, busy/lost/pool/state warnings, malformed records, and the
    derived STATEHASH counts.

  Unchanged and still passing: `test_engine_hooks` 14, `test_hook_signatures` 7,
  `test_statehash`, `test_divergence_probe`, `test_resync`, `test_resync_adapter`.
- **Full discovery:** 712 tests. The 35 errors/failures (symlink, Linux-starter and native UDP
  environment tests) are identical on the untouched base commit, which ran 701 tests.
- **Stage 1 evidence, re-read:** `role_seed(123456789, "MoveWorker") = f3da54e488294596`, and
  Stage 1's logged first state `28acd4a7fac17879` is exactly **150 draws** later in both runs
  C and D. The draw count therefore matched up to the first sample; the Stage 1 frame-2
  divergence happened after it.

## 4. Native test procedure (not run)

Use the primary PC with X4 closed: profile 45841, autosave disabled, the owned disposable
`save_010` used in Stage 1, the development extension from this branch with
`.local/evidence-053-withheld`, and `-seed:123456789`. Keep the defaults
`engine_hooks=true` and cadence 30; set `engine_hash_run` to the same value for both runs.

1. **Run E:** load `save_010`, do not touch input for 3 minutes, quit without saving. Copy the
   debug log to `.local/evidence-121-withheld`.
2. **Run F:** repeat identically → `runF-rngstreams.log`. Restore the guest extension, settings
   and log as before.
3. **Acceptance checks per log:**
   - `HOOKS_READY`
   - `RNGPHASES installed=1 slots=2,5`, or record the reason
   - the first `RNGSTREAMS` has `seed=00000000075bcd15` and `mw=…:q:0`
   - `path/mt/rm/grav` reach `q` within the first frames; record any `u` or `l`
   - `pool=` is equal in both runs; `j00..` reach `q`
4. **Compare:**
   ```
   python dev/compare_divergence.py .local/evidence-121-withheld \
     .local/evidence-122-withheld --statehash --derive-rng-steps \
     --report .local/evidence-118-withheld
   ```
   Record:
   - `FIRST DRAW DIVERGENCE` (frame and role)
   - `PHASE mw_*` (which MoveWorker system consumed the extra draws)
   - `EXACT MoveWorker tick`
   - each `ROLE` first-divergence line and its busy count
   - seed-frame warnings, which reveal worker timing

   Treat the STATEHASH buckets as before.
5. **Retroactive analysis:** run the same command with `--derive-rng-steps` on Stage 1 runs C/D
   (`runC-stage1-seeded.log`, `runD-stage1-seeded.log`). This gives the exact MoveWorker
   draw count at each Stage 1 sample.
6. **Optional control:** repeat one run with `engine_rng_phases=false` and confirm that only
   `mw_x` replaces `mw_a..c`.

Expected without fixed timestep or lock-step: MoveWorker counts diverge within the first
seconds, most likely in phase `b`. Wall-clock dt changes when timers, AI wake-ups and MD
checks fire. Worker seed frames and pass counts may differ. The result shows **where** the
draws diverge; it is not a determinism pass.

## 5. Open risks

1. **Worker pass counts are timing-dependent.**
   - MassTraffic and RegionManager are kicked at frame end only if idle; Gravidar is kicked
     at frame start only if finished.
   - Seeding at the first quiescent frame can therefore land on different observer frames,
     and later pass counts can differ.
   - Diagnostics expose this; fixing it needs always-join (Stage 4).
2. **JobThread streams cannot be made deterministic by per-thread seeding.**
   - Job-to-worker assignment is first-come.
   - The worker count depends on the machine.
   - Pathing agent batches and Jolt run in the pool.
   - Only pool totals are comparable. A real fix needs per-job seeding at job start.
3. **MoveWorker draws depend on MassTraffic timing.** `U::Zone::Move` only try-enters
   `g_MassTrafficLock` (0x3949b28).
4. **Variable dt and attention.** Variable dt (no fixed tick on this branch) and differing
   attention change MoveWorker draw counts regardless of seeding. Coordinate with the
   fixed-tick and attention branches.
5. **JobThread busy flag (medium confidence).** It assumes job categories are non-zero;
   construction-sequence (0xc24970) and 0x1213d8f callers are unresolved.
6. **Unseeded streams.** The main thread and the non-simulation XLib threads are not seeded.
7. **Phase-slot conflicts.** The phase markers occupy VBI slots 2 and 5 (statically empty,
   checked at runtime). Sibling work must not claim them, or should disable them.
8. **Walk cap.** Bursts above 2^21 draws between two observations flag the stream lost; its
   counts are then relative.
9. **Debug-log line length unverified.** Lines are split, so each is about 800 characters or less.
10. **Static derivation.** The quiescence gates come from static code reading, pinned by
    signatures. A native run must confirm them, for example that `ready` is set while idle and
    that seeds take effect.
