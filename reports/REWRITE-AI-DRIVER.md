# Engine rewrite: AI phase census and per-sector AI driver

System owner: agent `ai-driver`, branch `contrib/ferrite-rw-ai-driver`. Part of the engine rewrite program
(`reports/REWRITE-PROGRAM.md`). Build 23660954; Windows RVAs (VA = 0x140000000 + RVA). Terminology: the
dedicated X4 is the **server**, players connect as **clients** (user decision 26 Oct); no host/guest.

This covers the engine-side AI *phase driver*: the loop in `AIDirector::Move` that runs woken AI script
contexts once per simulation frame. The Rust per-sector *order behaviours* are the sibling system
`ai-orders` (`rust/x4mp-ai/`). Static RE background: `x4-re/coord/reports/sector-partition-scripts.md`
(the design this implements), `script.md`, `parallel-walk-design.md`, `parallel-sim.md`.

## 0. Status

- **Stage 0 (measurement-only census): built and tested offline. Not yet run natively** (lanes not ready;
  `quiet.lock` held during the live two-client test). Native numbers below are **profiler-derived estimates**,
  clearly marked; no AI census has run in X4 yet.
- **Stage 1 (single-threaded per-sector rounds driver): designed (section 5), not yet implemented.**

Own files: `native/engine/aidriver.h`, `native/engine/test_aidriver.c`, this report, and the additive AIPROF
logging in `dev/fixed_tick_probe.lua`. Shared-file edits (additive): `native/engine/signatures.py`
(AI sites + hashes), `native/engine/build.py` (`test_aidriver` in the `--test` list),
`tests/test_fixed_tick_probe.py` (3 AIPROF cases).

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

## 5. Stage 1 — single-threaded per-sector rounds driver (design; implements partition report §6)

Replace the `AIDirector::Move` loop (not the engine helpers it calls) with a driver that, each original pass
("round"), groups the pass's woken contexts into **buckets** keyed by the owner entity's sector (fleet root
when it has a commander), runs them bucket-by-bucket, and defers every cross-bucket/global effect to a barrier:
- **own-bucket wake** → the bucket's own queue; **foreign wake** → a per-round outbox merged at the barrier
  (hook `RequestWake` 0x106f910 + the inlined copy in Move, which the driver replaces, and in 0x1062f70).
- **events posted during a run** (MD, foreign AI) → **buffered and replayed at the barrier**, reusing the
  walk agent's PostEvent buffer, not a second one (coordination Q4). MD then runs serially at the barrier.
- the serial bucket (keyless + player + migrated hazardous contexts) runs at the barrier with original
  semantics. Comparison mode: run normal vs rounds over the same contexts, same order-visible outcomes, and
  diff state hashes (the partition report's §6.2 invariants: cue/context counts, wake counts, list integrity).
- Fail closed to the original loop on any mismatch.

The census decides the bucket key (fleet-root vs sector) and whether Stage 2 (threads) is worth it; Stage 1 is
built first regardless, as the semantic harness. Blockers B1 (value live-lists) and B2 (non-atomic refcounts)
are only relevant once threads run; Stage 1 does not touch them.

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

`tests/test_fixed_tick_probe.py` (real LuaJIT): **20 pass** (3 new) — AIPROF line logged, loud
`FEATURE aidriver DISABLED` on refusal, and no AI hooks unless `X4LAN_AIDRIVER` is set.

`python dev/check_hook_signatures.py ..\x4-re\bin\X4.exe`: PASS, executable + 25 hook signatures, no files
modified.

## 7. Native steps needed (scheduled on a lane, not run yet)

Once `rewrite-coord/native-lane.md` starts with READY and `quiet.lock` is gone, via `dev/x4lane.py`
(profiles 45850/45851, lane game views):
1. Load a disposable world; `X4LAN_FIXEDTICK_MODE` unset (or `profile`), `X4LAN_AIDRIVER='census'`. Confirm
   `FEATURE aidriver ENABLED mode=census` and collect ~10 min of `AIPROF` lines under three loads: single
   client on a highway; several clients in several sectors; a large fleet / many trading ships in one sector
   (the heavy-AI case the 8% estimate does not cover).
2. Read off: AI-phase share, per-sector/per-fleet balance and ideal speedup, foreign-wake and foreign-touch
   (hazard) rate, passes per frame, cue-event rate. Record in `rewrite-coord/results/`.
3. Decision: if hazard rate is low and a load shows AI-phase share high with balanced sectors, Stage 2 is
   worth prototyping; otherwise Stage 1 remains the deliverable and the lever is data-level (script intervals).

## 8. Open items / coordination (`rewrite-coord/questions.md`)

- **Q4 → walk**: Stage 1 must reuse the walk agent's PostEvent/Cancel/SendImmediate buffer (scoped begin/end +
  barrier replay) for AI-phase event deferral; I will not build a second buffer. Awaiting the real API shape.
- **Q5 → ecs-core**: barrier bucket order — I propose first-appearance-in-the-wake-vector order (deterministic,
  thread-count invariant, no tree index); confirm vs SectorIx order from `mirror.h`.
- **Q1 (ai-orders → me)**: the slot-565 event hooks and the Move/ExecState_Run brackets can emit the X4AITRACE
  `run`/`ev` capture fields (ctx/entity/script/pc/wake-mask/TLS+0x318/yield) or offer a chain callback on the
  same sites, so ai-orders does not register a second hook there. `aidriver.h` already carries an `ad_observer`
  callback table (run_begin/run_end/event_begin/event_end) for exactly this; I will answer Q1 with its shape.
- B6 property-lookup (partition report §10, "not located") is **located**: `Expression_PropertyLookup` 0xcec1a0
  / `EvaluateComponentProperty` 0xcedeb0 under the evaluator 0xc97cf0 — relevant to foreign-blackboard access
  inside expressions. Noted for ai-orders and a future finding row.
