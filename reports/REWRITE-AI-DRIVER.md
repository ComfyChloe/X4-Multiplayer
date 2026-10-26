# Engine rewrite: AI phase census and per-sector AI driver

System owner: agent `ai-driver`, branch `contrib/ferrite-rw-ai-driver`. Part of the engine rewrite program
(`reports/REWRITE-PROGRAM.md`). Build 23660954; Windows RVAs (VA = 0x140000000 + RVA). Terminology: the
dedicated X4 is the **server**, players connect as **clients** (user decision 26 Oct); no host/guest.

This covers the engine-side AI *phase driver*: the loop in `AIDirector::Move` that runs woken AI script
contexts once per simulation frame. The Rust per-sector *order behaviours* are the sibling system
`ai-orders` (`rust/x4mp-ai/`). Static RE background: `x4-re/coord/reports/sector-partition-scripts.md`
(the design this implements), `script.md`, `parallel-walk-design.md`, `parallel-sim.md`.

## 0. Status

- **Stage 0 (measurement-only census): native on lanes for all three loads**: idle, attnload (8
  high-attention sectors) and the combat scenario v3 `battle-400` (400 ships; the v1 combat run was invalid).
  Results: `rewrite-coord/results/2026-10-26-ai-driver-census.md`, summary in section 3a. Timing values are
  not trustworthy (shared PC); the structural numbers are.
- **Stage 1 (our AIDirector::Move, single-threaded, normal / rounds / compare): built and tested offline**
  (section 5, `native/engine/aidriver_move.h`, `test_aidriver_move.c`). **Native smoke passed**
  (lane b2, 2026-10-26, build 1a6e2c0/4e86886, run `s1-normal-smoke`): our Move ran every AI phase for
  60 s of a new-game world in `normal` mode, 54-60 driver frames per window, 0 pre-flight refusals, no
  disarm, no `bad_caller`, no loop diagnostic; the start-up burst (1,856 runs and 1,487 interrupt runs per
  frame, 13.5 passes) went through it. This checks the 38 helper calling conventions natively; it is not
  yet an equivalence result (the interleaved census/normal A/B and compare runs are queued, section 7).

Own files: `native/engine/aidriver.h` (census, hooks, Lua), `native/engine/aidriver_move.h` (stage 1),
`native/engine/test_aidriver.c`, `native/engine/test_aidriver_move.c`, `dev/aiprof_report.py`, this report,
and the additive AIPROF/AIDRIVE logging in `dev/fixed_tick_probe.lua`. Shared-file edits (additive):
`native/engine/signatures.py` (AI sites, 38 stage-1 helper pins), `native/engine/build.py` (two suites in the
`--test` list), `native/engine/fixedtick.c` (include + four calls), `tests/test_fixed_tick_probe.py` (6 cases).

## 1. What the AI phase is (RE, high confidence)

`U::UniverseFrameUpdate` 0x9fdef0 calls, in order: `EventSystem_ProcessDueEvents` 0x99bb50, the galaxy
component walk (0x9fdf95), a post-walk step, the release list 0x1092e80, `MDScheduler_Update` 0x1121f80
(0x9fe087), then **`AIDirector::Move` 0x106d250 at 0x9fe093** (`call` on `g_AIDirector` *(0x6cf15f0)).

`AIDirector::Move` (verified by full disassembly, `work/rw-ai-driver/out/move.asm`):
- A double-buffered intrusive wake vector at dir+0x200/+0x218, write index dir+0x230; a timed-wake red-black
  tree at dir+0x1c0 (leftmost dir+0x1e0). Each context stores its slot index in each buffer at ctx+0x1b0/+0x1b8
  (−1 = not queued).
- Up to **0x40 passes** (counter in `[rsp+0x58]`; the "Possibly infinite wake request loop" diagnostic fires
  at pass 0x20). Each pass: flip the write index; the pre-flip write vector becomes this pass's read vector;
  if that read vector is empty, harvest due timed wakes into it (so timed wakes run only after the event-woken
  cascade of earlier passes drains).
- Per context: set dir+0x238 = owner entity; handle global-interrupt (mask bit 0x40 → runs the interrupt
  runner 0xb58ce0 at 0x106d8d6), interrupt restart (bit 4 → 0xb58ce0 at 0x106e2ef) and attention-block change
  (`AIScriptContext_AttentionBlockChanged` 0x10664a0 → `AIScript_GetAttentionBlock` 0x1074850 +
  `ExecState_JumpToLabel` + 0xb58ce0 at 0x106e34a); then run the body with **`ExecState_Run` 0xb58bb0 at
  0x106e3f6**; on return, re-wake the caller (`RequestWake` 0x106f910 at 0x106e612, mask bit 0x20) or remove
  the context (`RemoveContext` 0x106f710). At frame end it processes script hot-reload lists (not per-frame).

So the per-frame cost is `ExecState_Run` plus the four interrupt/attention `0xb58ce0`/jump calls, over every
run of every context in every pass. Those five call sites are exactly what stage 0 brackets.

## 2. Cost breakdown from the existing profile (estimate, not an AI census)

Symbolized from `X4-Multiplayer-fixed-tick/.local/evidence-239-withheld`
(8 high-attention sectors; `scripts/rw-ai-driver/folded_ai.py`). Shares are of all MoveWorker samples.

- `AIDirector::Move` = **7.95%** of the whole run (matches the program's 8.0%).
- Inside Move: `ExecState_Run` **91%** of Move; the interrupt runner 0xb58ce0 another ~7.5% inclusive.
- Expression evaluation dominates a run: `Expression_Evaluate` 0xc97cf0 **36% of Move**, `Expression_ApplyOperator`
  0xc97fa0 28%, `Expression_PropertyLookup` 0xcec1a0 25% (the B6 blocker: property lookup under the evaluator —
  located here at 0xcec1a0 / `EvaluateComponentProperty` 0xcedeb0, which §10 of the partition report left open).
- Top action `Execute` bodies (inclusive share of Move): `DoIf` 24%, `FindTradeOffer` 13% (+ its filters
  `TradeFilterAnd` 0x892260, `ShipFilterByCanFindDockForTrading` 0x7ca940), `GetSafePos` 9%, `MoveTo` 7%,
  `GetGlobalPath` 4%, `IncludeInterruptActions` 5%, `FindComponent`/`FindGravidarContact`/`CreateTradeOrder`/
  `Dock` ~1.5% each. `ExecState_Run` attributed to the AI phase is 7.25% of the run; attributed to the MD
  scheduler 1.07% — i.e. the same interpreter is ~87% AI-driven here.

Decision-rule read (partition report §9, needs the native census to confirm): AI phase ≈ 8% of the frame in
this 8-sector load — **below the 20% "do Stage 2" bar**. Stage 2 (threads) is likely not worth it at this load;
the census must check a heavy-AI load (large fleets / many trading ships) before that call is final. Stage 1
(single-threaded rounds) is still worth building: it is the correctness harness for any later parallelism and
it isolates MD/event work out of the AI phase.

## 3. Stage 0 census — what it measures (`native/engine/aidriver.h`, mode `census`)

Included by `fixedtick.c` after the Lua helpers; driven from `dev/fixed_tick_probe.lua` (`X4LAN_AIDRIVER='census'`)
like WALKPROF, emitting one `AIPROF` line per second via `handle:aistats()`. Per 1 s window (per-frame means):

- **AI phase time** and its share of the slot-loop (`ms`, `share`), max frame.
- **contexts run** (`runs`), **interrupt runs** (`irq_runs`), **passes** and **max passes**, frames that hit the
  0x20 loop warning (`loop_frames`).
- **per-sector distribution**: each run is bucketed by the sector of the owner entity (ctx+0x120, parent chain
  +0x70 up to the `U::Sector` vtable 0x2b1ea48) — `sectors`, `sector_sum_ms`, `sector_max_ms`,
  `speedup = sum/max` (ideal per-sector parallel bound), `max_share` (balance). Also bucketed by **fleet root**
  (the report's recommended key; `fleet_speedup`) — currently the owner's sector until the commander-chain
  resolver is wired (`ad_fleet_root`; `GetCommander` 0x1f4350 / `IsFleetCommander` 0x1fd360 give the chain).
  Keyless contexts (superhighway ships, player, galaxy/cluster owners) fall in the **serial** bucket
  (`keyless_runs`, `serial_ms`).
- **wake sources**: external wakes present at frame start by reason mask (`ext_wakes` + `ext_*`), due timed
  wakes (`timed_due`), and wakes generated during a run split **own vs foreign sector** (`wake_own`,
  `wake_foreign`) — the proxy for how much cross-sector wake traffic a rounds driver must route to the barrier.
- **foreign-touch rate**: events delivered during a run, split own-sector / foreign-sector / MD-cue /
  director (`ev_own`, `ev_foreign`, `ev_cue`, `ev_director`), via vtable slot 565 (`HandleScriptEvent`) of
  AIScriptContext / AICallStackContext / MD::Cue. A run that wakes or delivers to a foreign sector, or to any
  cue, is counted **hazardous** (`hazard`) — the fraction that would migrate to the serial tail in Stage 1.
- **top scripts** (by time, from AIScript name ctx+0xd8→+0x10) and **top sectors** (by UniverseID) for the window.

All counters are per-frame means over the window; the window resets on each `aistats()` read.

## 3a. Native census results (lanes, 2026-10-26; timing not trustworthy)

| measure | idle (463 s) | attnload, 8 sectors (292 s) | battle-400 window (121 s) |
|---|---|---|---|
| AI phase share of the slot loop | 9.1 % | 9.2 % | 10.3 % (windows up to 18.5 %) |
| contexts run / interrupt runs per frame | 48.7 / 5.1 | 71.2 / 8.4 | 121.3 / 23.2 |
| passes per frame (max) | 6.3 (14) | 7.5 (15) | 8.5 (14) |
| foreign-sector share of wakes made during runs | 33.8 % | 35.6 % | 37.1 % |
| events during runs: own / foreign / MD cue | 5.5 / 0.31 / 3.5 | 8.1 / 0.21 / 4.9 | 16.7 / 0.15 / 174.5 |
| runs touching a foreign sector or cue | 5.0 % | 6.2 % | 8.5 % |
| AI-active sectors; largest sector's share | 26.7; 26 % | 36.9; 20 % | 50.0; 30 % (windows up to 52 %) |
| runs that advance the MoveWorker random stream | - | - | 93 % |

Script ranking: move.generic ≈ 24 % (18 % in the battle), trade.find.free 15-22 %, fight.attack.object.*
13-15 % at idle and 34 % in the battle (fighter, medium, capital), order.dock ≈ 8 %, move.idle ≈ 8 %. Against
the partition report's rule the AI phase is under the 20 % bar in all three loads (threads for the AI phase
alone do not pay); balance and hazard are within bounds; a third of the wakes cross sectors, which a
pass-synchronous rounds driver absorbs (they land in the next pass in both orders) but threads would turn
into outbox traffic. Fleet-root keying moves only 0.8 % of the foreign wakes into the waker's bucket (battle).
Nearly every run draws from the MoveWorker's random stream, so reordering hands runs different random
numbers unless the streams are per entity. Battle census: build d922398, run `census-b400` (lane b1).

## 4. Hooks and safety (ENGINE-INSTRUMENTATION.txt; registered in `rewrite-coord/hooks.tsv`)

| site | kind | what |
|---|---|---|
| 0x9fe093 | call rel32 → near stub → `ad_phase` | brackets/times the whole AI phase |
| 0x106e3f6 | call rel32 → stub → `ad_exec_run` | per-context run (ExecState_Run), census only |
| 0x106d8d6, 0x106e2ef, 0x106e34a | call rel32 → stub → `ad_irq_*` | interrupt/attention runs (0xb58ce0), census only |
| 0x2c6b418, 0x2c6a260, 0x2c76318 | vtable slot 565 entry (data-only cmpxchg) | event own/foreign/cue counting |

- **Fail closed.** The whole `AIDirector::Move` body (0x106d250, 0x140e bytes), `ExecState_Run`, the interrupt
  runner and the call window at 0x9fe080 are SHA-256 pinned in `signatures.py` (`check_hook_signatures.py`
  passes: 25 sites). `ad_install` verifies every code hash and every call target/vtable entry against the
  verified original and patches **all or nothing**; a per-frame `ad_check` on the owner thread disarms on any
  drift; `disarm` restores every rel32 and vtable entry.
- **Code patches** are 4-byte rel32 edits of existing `E8` calls, compare-checked against the verified original
  rel32 and redirected to a near stub (allocated within ±2 GB of the image, never freed, `jmp [rip]` to the
  wrapper). The stub saves `r13` (the context) and `rbp` (Move's frame, for the wake mask at `[rbp+0x170]`)
  before the wrapper runs — these registers hold the context/frame at each call site (verified in the
  disassembly). Only the MoveWorker executes these sites and it is never inside them while patching (the patch
  runs from the slot-6 UI `onUpdate`, not slot 3), so the unaligned write is never observed torn.
- **Data-only** vtable entries are `InterlockedCompareExchangePointer` from the verified original.
- No hook of mine touches `PostEvent` / `DeliverEvent` / `CancelPendingEvents` / the networking transport.
  Wrappers pass straight through off the owner thread or when not armed.

## 5. Stage 1 — our AIDirector::Move (built; `native/engine/aidriver_move.h`)

**What it is.** A behavioural reimplementation of the whole Move loop, written from the full disassembly and
the decompile of the pinned body (the report describes behaviour; no decompiled code is copied). The control
flow is ours; every engine operation is the engine's own helper called with the original's arguments (38
helpers, first 32 bytes SHA-256 pinned in `signatures.py AIDRIVER_HELPERS`, verified before a driver mode is
allowed; calling conventions read from each call site, e.g. `GetAttentionBlock(script, attention)`,
`AbortContextStack` with 8 arguments, the interrupt runner's byte flag on the stack). Covered behaviour:
- outer loop: up to 0x40 passes; flip the write index except in pass 0x21; the pre-flip write vector is the
  read vector; when empty, harvest due timed wakes in time order (each node released into the director's
  node pool, leftmost cache and size updated exactly as the original's inline code), leave when still empty;
  pass 0x21 can therefore only run timed wakes, and wakes made in pass 0x20 carry to the next frame;
- per context: dequeue (slot of the read buffer = -1), current entity, wake mask (timed pass: timed mask
  when the wake time is due, else 0), the pass-0x20 "Possibly infinite wake request loop" diagnostic (same
  text through the engine's formatter and logger), global interrupt handlers (mask 0x40: first pending
  record whose chosen handler has no script or a script whose priority beats the holder's; unknown script ->
  the engine's error; apply: optional label, abort of the context stack, target cleanup, push or restart
  with the 0x80 re-wake), cleanup when the mask is empty or a nested frame runs, interrupt restart (mask 4),
  attention-block restart, resume, ExecState_Run, completion (run_script / run_order_script return value or
  the engine's "did not provide a return value" error, caller re-woken with 0x20 or restored), removal;
- end of frame: current entity cleared, refresh lists (dry list first).
Deviations: only where the original would dereference a null caller / blocking action (it would fault);
counted (`bad_caller`).

**Modes.** `normal`: original order. `rounds`: each pass grouped by bucket (sector of the owner entity;
keyless = serial bucket), buckets in order of first appearance in the pass's read vector, stable inside a
bucket; with the walk agent's buffer bound (integrated build, `#ifdef X4LAN_WALKPAR_H`, Q4/Q7/Q9) every post
made while a bucket runs is deferred in a scope keyed by the sector UniverseID and replayed **at the end of
the pass, before the next flip**. Barrier order (ecs-core's answer to Q5): the original sequential order
(pass, wake index of the emitting context, emission order), the key of x4mp-sim's `Outbox::set_order`. With
the optional `set_order`/`replay_ordered` pair (proposed to walk, Q20; bound under `WP_HAS_ORDER`) each op
carries its context's key, a cancel kills only buffered posts with a smaller key, and the replay runs by
(key, emission), so the engine sees posts and cancels in the original order (tests 10f/10h); with today's
walkpar the replay is per task in bucket order (= rounds execution order). The same rule holds for wakes:
rounds appends a pass's wakes in rounds execution order, so at the end of the pass it puts them back in the
original order (stable by the waker's read-vector position, emission order inside a run; each context's slot
index moves with its entry; a slot mismatch skips the reorder, counted) and the next pass reads the
original's vector (`wakeReordered` passes in AIDRIVE). Not observed: a context woken from two buckets keeps
the entry of its first waker in rounds order. Because cross-sector wakes go into
the write vector, they run in the next pass in both orders: rounds keeps the original **pass structure**. Without
the buffer (this branch alone) rounds only reorders. `compare`: rounds plus these order-equivalence counters,
which define exactly when a pass's outcome can differ from the original order:
1. `cmp_null_lost`: a context of another bucket that the original would already have run (smaller index) was
   nulled (ClearWake) before its turn — rounds skipped a run the original makes;
2. `cmp_wake_absorb`: a wake of a context of another bucket that sits in this pass's read vector — it is
   absorbed (ClearWake of the processed context nulls the new entry) iff the wakee runs later in the pass;
   counted when the original and rounds orders disagree on that;
3. `cmp_event_pending` / `cmp_event_ran`: an event delivered during a run to a context of another bucket
   that has not run yet, or ran although the original would run it after the poster;
4. `cmp_post_cancel`: a cancel and a post for the same source and type from different buckets whose rounds
   order is the opposite of the original order of the two issuing contexts (immediate posts counted too,
   conservative, until the buffer readout exposes the flag).
A pass with none of these has no observed event/wake hazard (`cmp_passes_clean`); RNG inversions are
counted separately in `cmp_rng_passes`, and shared-state writes (blackboards,
`global.$`) are not observed by these counters. A per-frame hash of the observed context/wake vectors
(masks, wake time, queue membership, frame pcs) is logged in compare mode; see the coverage failure below.

**Fail closed.** Driver modes need the helper signatures; a structural pre-flight (write index, vector
bounds, slot-index consistency of the first read vector, timed tree) runs before every frame and again
after it; any failure runs the original Move and switches to census mode with a loud `FEATURE aidriver
DISABLED reason=... (census continues)`. Census hooks stay installed in all modes.

Blockers B1 (value live-lists) and B2 (non-atomic refcounts) matter only once threads run; stage 1 does not
touch them.

## 6. Offline tests (synthetic; `native/engine/test_aidriver.c`, `tests/test_fixed_tick_probe.py`)

`python native/engine/build.py --zig <zig> --fixedtick --test` builds with `-Wall -Wextra -Werror` and runs
fixedtick + simprof + **aidriver** synthetic suites: **all pass**. The aidriver suite (fake VirtualAlloc image,
fake `U::Sector` vtable, fake contexts, a fake `AIDirector::Move` that drives the census wrappers as the stubs
would) covers:
- install/uninstall swaps all 5 call sites and 3 vtable slots, **all-or-nothing** (a changed call target or
  vtable entry installs nothing), and restores every site on disarm;
- the emitted stub bytes (r13/rbp capture + `jmp [rip]`) resolve to the right saved slots and wrapper;
- per-sector bucketing with 1/2/3/4 ms runs → `sector_sum=10`, `sector_max=4`, `speedup=2.5`, `passes=1`,
  `ext_wakes=4`; cross-sector wake and keyless (serial) context classification and hazard count;
  event own/foreign/cue/director split.

The stage-1 suite (`test_aidriver_move.c`) uses a fake engine whose helpers reproduce the reverse-engineered
behaviour (RequestWake slot rule, ClearWake nulling of both buffers, ClearTimedWake(IfDue), RemoveContext,
the timed tree with its node pool, interrupt runner and handler selection) and a walkpar-semantics event
buffer (scoped buffering, a cancel kills buffered scheduled posts of any task, immediate posts survive,
replay of listed tasks then a sweep). It checks, per branch, the helper-call order against the original's
(cleanup, interrupt restart, attention restart, completion with run_script / run_order_script / restore,
the six global-interrupt cases), the pass structure (timed harvest order and node pool, 0x20 diagnostic text,
0x21 without a flip, wake carried to the next frame), normal == rounds when buckets do not interact (run
multiset, per-sector order, state hash), each compare counter on a constructed case (cross-bucket ClearWake,
wake absorption), and the walk review's post/cancel cases (post earlier in bucket B / cancel later in bucket
A and the reverse; an order inversion is flagged; replaying tasks against execution order breaks the result,
a negative control). **All pass.**

`tests/test_fixed_tick_probe.py` (real LuaJIT): **23 pass** (6 for the AI driver) — AIPROF and AIDRIVE lines,
actual mode logged, loud `FEATURE aidriver DISABLED` on refusal / driver refusal / mid-run fallback, no AI
hooks unless `X4LAN_AIDRIVER` is set.

`python dev/check_hook_signatures.py ..\x4-re\bin\X4.exe`: PASS, executable + 63 hook signatures (25 sites +
38 stage-1 helpers), no files modified. Note: `build.py --test` runs the suites in order and the simprof
suite's 3 %-overhead timing assertion fails on a saturated PC (7 X4 instances); simprof is untouched by this
branch, and the aidriver suites were run directly with the same compiler flags.

## 7. Native steps (stage 0 done for two loads; stage 1 next)

Stage 0: done on lanes for idle and attnload (section 3a); combat (scenario v3 `battle-400`) queued.
Stage 1, in order (lane runs with the `probe-s1b` overlay = d922398, release x4lane, `.local/evidence-103-withheld`):
1. **done** (`s1-normal-smoke`, 1a6e2c0): `X4LAN_AIDRIVER='normal'`, idle, 60 s: the driver took every frame
   (`driverFrames` = frames, `preflightRefusals=0`), no crash or disarm, and no new engine script errors vs
   the census-idle log (first 61 s of game time: 715 vs 639 log entries, 0 vs 1 `aicontext` script messages,
   no new message kinds); this was the only check of the helper calling conventions.
2. `alternate` (census and normal windows interleaved on one world; `aiprof_compare.py <log> --modes
   census,normal`), idle then battle-400: structural means and per-run ratios (runs, passes, interrupt runs,
   attention restarts, removals, caller wakes, global handlers, wakes, events) agree within noise; zero
   `bad_caller`.
3. `compare`, idle and attnload: fraction of passes with no observed event/wake hazard and the counters; reorder only on
   this branch (no walkpar), deferral in the integrated build (walkpar `WP_REPLAY`, see Q9).
4. Before ours becomes authoritative outside tests (method step 2): capture the original's per-context
   decision trace natively (more call-site hooks inside the pinned Move body, census-only) and replay it
   offline against `ad_process_context`; zero differences required.

## 8. Open items / coordination (`rewrite-coord/questions.md`)

### Native compare-idle result, 26 October 03:35

The existing chain's `s1-compare-idle` finished at 03:34:59, lane harness pass (requested observation
window completed), **not semantic equivalence**. After 30 warmup windows: 453 windows, 16,030 driver
frames, 101,778 passes; 355 passes had event/wake hazards (mostly wake absorption), and 18,415 passes
(18.09%) reordered RNG-drawing contexts. Wake order was restored in 2,513 passes, with zero skips;
zero preflight refusals, bad callers or loop logs. No event buffer was bound (`evbuf=0`). Evidence:
`.local/evidence-091-withheld`; timing is untrustworthy. The existing chain next runs battle-400.

Every one of the 481 non-initial hash samples was `d852dcb87b6f12a3`: exactly this implementation's hash
of three zero vector lengths. This strongly indicates that the post-phase context vector at
director+0x148 and the two wake vectors supplied no contexts; direct count instrumentation was not
in that overlay. Its earlier label "AI state hash" overstated coverage; equality of these hashes
proves nothing about active script state. The RemoveContext helper
uses +0x148, but complete live-context enumeration remains unresolved; no new offset is guessed.
New diagnostics report `hashValid`, `hashContexts`, `hashWakeContexts`, and `hashEmptyFrames`, and emit
`hash=unavailable` when coverage is empty. Synthetic tests distinguish empty/populated contexts,
changed masks and queued contexts; 24 real LuaJIT tests verify log transport. The diagnostics build
and the joint synthetic buffer test pass. They are not present in this already-frozen native overlay.

These observed dependencies block promotion of rounds to a behaviour-identical replacement. Normal
serial mode remains the comparison baseline; no per-entity RNG policy change was made.

### Native battle-400 continuation, 26 October 03:48

`s1-alt-b400` completed 03:42:43 and `s1-compare-b400` 03:47:25, both lane collection passes, graceful
close, frozen probe-s1b, timings untrustworthy. Evidence: corresponding `.local/evidence-075-withheld` folders.
The original chain has finished; none of these runs contains the new Codex diagnostics.

Alternating census/normal after 30 warmup windows: 88/87 windows, 1,341 normal-driver frames, zero
refusals/bad callers/loop logs. Most structural means and all normalized ratios are within 15%, but
foreign-context event delivery differs by -56% (0.926 versus 0.408 per frame). Three census windows
contribute 64% of its summed window means, so bursty temporal sampling is a possible explanation,
not an established cause. This difference remains unresolved; a clean normal-mode equivalence verdict
is not justified. Measured phase share averages 12.0% original / 12.2% normal in this contended run.

Compare after 30 warmup windows: 177 windows, 3,199 driver frames, 24,456 passes. Event/wake hazards
occurred in 567 passes (2.32%, chiefly wake absorption); 7,386 passes (30.20%) inverted RNG-drawing
context order. Wake order restored in 3,667 passes (14.99%), zero skips/refusals/bad callers/loop logs.
The buffer was absent. All 205 non-initial hashes again equal the empty-vector digest. These are
measured semantic hazards, not a successful rounds replacement.

The combat owner's separate `scale-battle-800` profile reports inclusive AIDirector samples at 25.68%
of MoveWorker (400: 13.17%). It has no AIPROF markers or bucket census, and inclusive sample share is
not the same metric as phase/slot-loop share. It is a candidate for a future direct 800-ship AI census
to revisit the optimization gate, not grounds to promote threaded execution now.

### Native hash coverage diagnostic, 26 October 03:51

`codex-hash-coverage` completed 03:50:42 on released lane b2, collection pass, graceful close and
restoration. This was the sole additional Codex native run; it began after the existing chain finished.
Frozen overlay source `26472be` (code `09c504e`), manifest `.local/evidence-070-withheld`;
evidence `.local/evidence-078-withheld`. The run used compare mode without walkpar, had no
timing claim, and left no AI-driver lane or waiter running.

Direct coverage counts confirm the gap: 2,685 driver frames, all with zero hashed contexts and zero
hashed wake-vector contexts; 0/63 final-window samples nonempty. Every reported hash was unavailable.
The observed AI runs and event/wake activity continued normally, so this is not an idle/no-script
control. Zero preflight refusals, bad callers or loop warnings. This verifies the new coverage
diagnostics and invalidates the old constant hash as an AI-equivalence indicator. It does not repair
live-context enumeration. The same run reports 23 event/wake-dirty passes and 2,979 RNG-order-inverted
passes out of 12,329, plus nine passes too large for the null scan; these remain semantic limits.

Next work: identify a complete context source or capture original per-context decisions, investigate
the bursty foreign-event A/B difference, and run a direct 800-ship census if prioritization needs it.
Any native joint walk integration must use the walk owner's later ABI fixes, not the earlier synthetic
test snapshot. No per-entity RNG policy, gameplay authority or thread rewrite was approved here.

### Codex continuation, 26 October 03:13

`s1-alt-idle` completed natively at 03:12:02 using the already-frozen `probe-s1b` overlay
(DLL SHA-256 `e19aebed712c1f92a52374bc692b26182fdaa64eb5ca4a9eaa24e30e56cee64b`).
After 30 warmup windows, the same-world alternating run contains 287 original/census and 286 normal
windows, 16,966 normal-driver frames, zero preflight refusals, loop warnings or bad callers. Relative
normal/original differences: runs -0.1%, passes +0.6%, timed wakes -0.2%; all reported structural and
population-normalized measures are within the prespecified 15% comparison band. This establishes
idle structural consistency, not per-context decision equivalence or a performance improvement.
The lane explicitly reports timing untrustworthy. The existing chain continues with compare-idle
and battle-400; its immutable overlay is separate from the changes below.

Compare diagnostics now account for a previously unobserved hazard: an immediate event buffered until
the pass barrier can change what a later context in the **same** bucket sees. Sorting replay by original
keys cannot repair that synchronous dependency. `cmpDeferredImmediate` counts such posts conservatively
and marks the pass dirty. A legacy buffer lacking event-detail readout marks affected passes unchecked
and dirty. Immediate posts are excluded from post/cancel conflict checks when their flag is available.
The output says "no observed event/wake hazard", with RNG inversions reported separately, since shared-state writes, duplicate-wake provenance and
other uninstrumented channels still prevent an equivalence claim.

Q9/Q20 adapter preparation: when `WP_HAS_AI_REPLAY` is present, bind only in `WP_REPLAY`/`WP_AI_REPLAY`,
refreshing the binding at each AI phase so independently disabling the walk buffer cannot leave a stale
"evbuf=1" claim. The append-only `buffer_event` readout is optional for older integrations.
Synthetic regressions include a negative control where a later same-sector context sees an immediate
effect in normal mode but misses it in ordered rounds, despite equal final event-delivery counts.
Both C aidriver suites and 24 real LuaJIT probe tests pass; the fixed-tick DLL builds with warnings as
errors. The new integrated walk adapter still requires validation against the walk owner's Q20 header
and a later native test; these diagnostics do not change the unresolved RNG policy.

03:20 update: the actual ai-driver adapter and walk commit `a60b058` buffer/header pass a joint
synthetic test (`.local/evidence-065-withheld`, snapshot `.local/evidence-001-withheld`). Both inverted
post/cancel cases, original-order three-source posting, the same-sector immediate-event hazard,
and binding gates for off/census/replay/AI-only replay pass with zero dropped/degraded events.
The readout returns zero for consumed operations, which are skipped (not counted as unknown).
The snapshot compiles with warnings as errors except unused functions from the deliberately
unattached native walk lifecycle. This tests the real buffer/adapter together on a fake engine;
it neither installs native detours nor validates an integrated game build.


- **Q4/Q7 (walk)**: answered — one buffer, `walkpar_api()` scopes + ordered replay; bound under
  `#ifdef X4LAN_WALKPAR_H`. **Q9 → walk** (open): an AI-only replay mode (WP_REPLAY also defers the walk) and
  `get_mode()`; the immediate flag in `buffer_op`; confirm `wp_cancel` kills scheduled posts of any task (the
  code) rather than only its own (the comment).
- **Q5 → ecs-core**: answered — barrier order = original sequential order (pass, wake index, emission), not
  bucket order; implemented behind the optional walkpar pair (Q20 → walk: `set_order(key)`, key-aware cancel,
  `replay_ordered()`, plus the imm flag / event pointer in the readout), bucket order until walk has it.
- **Original-order stamp (Q14)**: `ad.run_pass` / `ad.run_pos` valid from run_begin to run_end (census: read from
  Move's frame, rbp-0xa8 / rbp-0x80 / rbp-0x88 inside the pinned body; driver: set by our loop).
- **Q1 (ai-orders)**: answered — `ad.observer` callbacks (run_begin/run_end/event_begin/event_end) on my sites;
  census script ranking published for their order choice.
- Fleet-root keying (commander chain) is not resolved; the census reports fleets == sectors.
- Lane-check lesson: anchor `--expect`/`--until` on real marker lines; MD error-context dumps repeat the
  marker text (`* Expression: '... COMBATSCN engaged ...'`).
- B6 property-lookup (partition report §10, "not located") is **located**: `Expression_PropertyLookup` 0xcec1a0
  / `EvaluateComponentProperty` 0xcedeb0 under the evaluator 0xc97cf0 — relevant to foreign-blackboard access
  inside expressions. Noted for ai-orders and a future finding row.

## 9. Stage 2 (worker threads) — design, gated by the census

**Gate.** The partition report drops stage 2 when the AI phase is under 20 % of the frame, the largest bucket
holds over 60 %, or over 10 % of runs would migrate. Measured (section 3a): AI phase **≈ 9 %** of the slot loop
at idle and with 8 high-attention sectors and **10.3 %** in a 400-ship battle; largest sector 20-30 %; runs
touching a foreign sector or cue 5-8.5 %. Amdahl bound: stage 2 can save at most share × (1 − 1/S); with the
ideal per-sector S = 3.5-5.2 that is ≤ 7 % of the frame, and less in practice (a barrier per pass at 6-8 passes per frame, the serial bucket, the
barrier replay, migrated runs). The combat agent's battle profile (87 ships) shows AIDirector::Move at 10.9 %
of MoveWorker samples, also under the bar. **Recommendation: do not build stage 2 at these loads** (the
battle-400 census confirms it). Re-evaluate only for loads well beyond a 400-ship battle and after ai-orders' native ports of the top
scripts (move.generic, trade.find.free, fight.attack.object.fighter ≈ 55 % of AI run time), which remove the
interpreter cost outright and run per sector in x4mp-sim, where parallelism comes by construction.

**If a load passes the gate**, stage 2 is stage 1's pass-synchronous rounds with the buckets of a pass on N
workers (largest first by the previous pass's time) and a barrier per pass. The pass structure stays the
original's. What it needs on top of stage 1, with the partition report's blocker numbers:
1. Per-thread state (B7, B8): `g_CurrentExecState` 0x38f4c60 (5 functions) and the director's current entity
   dir+0x238 (read by AbortScriptsOnEntity) in TLS; TLS game time / role flag copied to the workers.
2. Wakes (B4): a RequestWake 0x106f910 detour routes worker wakes into per-bucket outboxes merged at the
   barrier in the original order (the waker's (pass, wake index), then emission). The inline copies need no routing: our driver calls RequestWake itself; the one in
   AICallStackContext::HandleScriptEvent 0x1062f70 runs during event delivery, which is deferred to the
   barrier on the main thread; RefreshScripts runs at frame end. ClearWake of a context of another bucket
   writes another worker's read-vector slot: migrate (it is the `cmp_null_lost` interaction anyway).
3. Events (B3): the walk buffer becomes per worker (thread-local op buffers, merged by the original-order key
   (pass, wake index, emission) per ecs-core's Q5 answer, which is thread-count invariant; one op-record type,
   Q8; key support requested from walk, Q20); event values deep-copied at post time (B2 residual). Wakes (item
   2) merge by the same key.
4. Script values (B1, B2): the ~14 unlocked live-object lists (104 writer functions) need locked link/unlink
   or serialised value creation; non-atomic refcounts need bucket exclusivity: fleet-root keying plus
   migration of any run that reads or writes a foreign blackboard or `global.$` — detection in the expression
   property lookup 0xcec1a0/0xcedeb0 (B6) and slot-3 guards for hazardous action classes (A6; ai-orders owns
   those slot-3 entries, so the guard would chain on their table).
5. Randomness: the interpreter's chance= stream is per thread (TLS+0x318), so N threads draw differently from
   1; thread-count invariance needs per-entity streams (contrib/ferrite-rng-streams).
6. Fail closed per pass: a hazard detected on a worker rewinds the action (pc − 1, yield) and migrates the
   context to the serial tail; any detour verification failure runs the pass on the main thread.
Cost: high (items 3-4 dominate). Expected gain at measured loads: ≤ 7 % of the frame. Stage 1 stays the
deliverable: the semantic harness, the order-equivalence instrument, and the driver ai-orders' Rust
behaviours plug into when they take over contexts.
