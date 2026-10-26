# Component-walk parallelism and deferred events — stages 0 and 1

X4 9.00, Steam build 23660954 (X4.exe SHA-256 19750A65…6891). Agent: walk,
branch `contrib/ferrite-rw-walk` (base `contrib/ferrite-rewrite-base` e63d9cf).
Design: `x4-re/coord/reports/parallel-walk-design.md` (author: the "Second sandbox" session, the reviewer).
Terminology (user, 26 Oct): the dedicated X4 is the **server**; players are **clients**. New material uses
server/client, not host/guest.

This report covers what was built offline (stage 0 census engine + stage 1 deferred-replay engine, with
tests) and specifies precisely the in-process patch the native lane must install. **No native run has been
done** (rewrite-coord/quiet.lock held for the user's live two-client test; native-lane.md is not READY). No
engine bytes were modified; no X4 was launched.

## 1. What was built (offline)

Own files:
- `native/engine/walkpar.h` — the engine: per-frame census counters, the stage-1 post/cancel buffer (with
  the source UniverseID stored per buffered op), tree-order replay, a normal-vs-replay listener comparison,
  and a shared scope/ordered-replay API for the ai-driver AI phase. Pure data+logic; it never patches the
  process. Included by `fixedtick.c` after `walkprof.h`.
- `native/engine/test_walkpar.c` — synthetic tests in the style of `test_fixedtick.c` (fake events/components
  with vtables); built and run by `build.py --fixedtick --test`.
- Additive edits to shared files (minimal, per the rules):
  - `signatures.py`: `WALKPAR_SITES` (byte-pinned detour targets + the galaxy call site) and
    `x4_walkpar_checks[]` / `WP_GALAXY_CALL_RVA` emitted into the C header.
  - `fixedtick.c`: `#include "walkpar.h"`; `walkpar_arm()` (verify only) in `start()`; `walkpar_disarm()` in
    `disarm_because()`; a WALKPAR/WALKCENSUS block in `stats()`.
  - `build.py`: add `walkpar` to the `--fixedtick --test` targets.
  - `dev/fixed_tick_probe.lua`: `WALKCENSUS` / `WALKREPLAY` log lines and a loud `FEATURE walkpar DISABLED`.
- RE: `x4-re/labels/rw-walk.tsv` (4 labels for the hot functions below), `x4-re/scripts/rw-walk/rwdis.py`
  (private capstone helper; analysis only).

The engine is driven by entry points an in-process layer calls once the lane exists; `walkpar_arm()` only
**verifies** the build-pinned bytes and reports readiness. Any verification miss fails closed (pass-through,
no replay). The native detours are registered in `rewrite-coord/hooks.tsv` and **not armed**.

## 2. Stage 0 — hazard census (measurement only)

Driven per frame by: the galaxy-walk wrapper (`wp_walk_begin/end`), the Sector::Move bracket
(`wp_sector_enter/exit`, chained from walkprof's existing sector wrapper — do not add a third replace on
0x2b1ff58), a PostEvent entry detour (`wp_post`), a DeliverEvent wrapper (`wp_deliver_enter/exit/listener`),
and counting detours on the creation/global paths (`wp_note_*`). Per frame and per sector it records:

- **Time split:** inclusive Sector::Move ms (sum and max across sectors → ideal speedup = sum/max), and
  inclusive DeliverEvent time while inside a sector (outermost only). This is the deferrable-handler vs
  parallelisable-compute split that decides the stage-2 gain (design §6.1 decision rule).
- **Event traffic:** posts by type (histogram keyed on the event's vtable-slot-1 type id) and immediate vs
  scheduled; cancels; SendEventImmediate; non-empty FlushCommandQueue; DeliverEvent calls and dispatched
  listeners (with the listener's vtable as an RTTI-class stand-in).
- **Cross-sector flags:** per post/listener/ConnectTo, whether the source / listener / new parent resolves
  to a sector other than the current one (`wp_sector_resolver` walks +0x70 to the U::Sector id).
- **Creation / global paths:** ComponentFactory::Create, SpawnEffect, CreateTempZone, AssignNewID, the hit
  path (PostAttackedEvents), OnLastRelease during the walk, Cue_RunActions, ExecState_Run, AIDirector_RequestWake.

### 2a. Attribution of the unknown hot functions in the 8-sector walk profile

From the folded 8-sector profile (`…/evidence/simcost/simprof-8sectors.folded`). Static RE only; labels in
`x4-re/labels/rw-walk.tsv`, evidence in `x4-re/work/rw-walk` analysis (not shared). Event type ids below are
read from each event's vtable slot 1 (`mov eax,<id>; ret`).

| RVA | share | attribution (medium confidence) |
|---|---|---|
| **0x748120** Positional::Move | 4.7% self | the base per-object Move: movement-controller update (>4), then child enumeration via 0x314fa0, then animation 0x1165660, then crowd (dtCrowd::update). Self time = controller update + child iteration dispatch. |
| **0x1165660** | 4.0% incl | `maybe_Positional::UpdateAnimationTimelines` — called only by Positional::Move; drives per-object animation timelines (calls the sampler 0x1165ee0 and channel appliers 0x759320/0x759b20/0x759f00). Client-side character/skeletal animation; independent per object. |
| **0x1165ee0** | 2.3% self | `maybe_AnimationTimeline::SampleCached` — leaf; memoised keyframe sampler (compares the request to a cached stamp +0x68 within an epsilon, returns the cached value +0x60 on a hit). ~10 timeline callers. |
| **0x314fa0** | 1.7% self | `maybe_Component::ChildBlockIterBegin` — leaf; initialises iteration over a Component child-block vector (count +0x20, begin/end +0x8/+0x10, 0x20 stride). ~1700 callers (Positional::Move, BuildCache, ConnectTo…). The per-node child-enumeration primitive of the whole walk. |
| **0x81c7a0** | 1.2% self | `maybe_Weapon::PostFireStateEvent` — reached from Weapon::vfunc674 / Turret::vfunc683 / TurretAction; clears a flag +0x354, allocates a 0x18-byte event and PostEvents it (a weapon firing-state self-event). Combat subtree. |

**Takeaway:** a large slice of the sector-scaling cost (0x1165660 + 0x1165ee0 + part of Positional::Move
self = ~7–9% inclusive) is **character/skeletal animation timeline evaluation** — per-object, independent,
and client-only. On a dedicated server it overlaps the frame-diet agent's slot-8 (animation) removal and is
also the cleanest parallel-friendly compute. 0x314fa0 is pure child-iteration overhead spread across the
walk; 0x81c7a0 is a weapon event post (feeds the stage-1 deferral). This sharpens the design's split: the
parallelisable compute is dominated by per-object animation/transform math, while the serial-at-replay part
is event delivery + MD/AI, as the design predicted.

## 3. Stage 1 — single-threaded deferred replay

`wp_walk_begin` turns the phase on; `wp_post` buffers each post as `{src UniverseID, event*, time, imm, type,
task, seq}` and returns 1 so the detour skips the original; `wp_cancel` marks matching buffered posts of the
task dead and appends a CANCEL op; at `wp_walk_end` the phase is turned off and `wp_replay` delivers the
buffer in post order (= tree order) through the original PostEvent/CancelPendingEvents **before 0x1092e80**,
re-resolving each source by UniverseID (ComponentRegistry::Find) and freeing the event via its vtable slot 0
if the source is gone. Fail-closed: a full buffer passes the post through inline and flags the frame
`degraded`; any signature miss leaves the engine OFF.

**AddTempZoneEvent (type 0xa) is never buffered** — `wp_post` returns 0 for it so it is delivered inline and
later FindZoneAt calls in the same walk see the new zone (design §1.3). Event type ids confirmed from the
build: AddTempZone 0xa, Attacked 0x1e, ChangedAttention 0x46, TempZoneAdded 0x1db, Warp 0x21f.

### Normal-vs-replay comparison

`wp_deliver_listener` records a per-delivery tuple `(listener id, src id, type, payload)` keyed by the
**listener's sector**. A normal run (no buffering, deliveries inline during the walk) fills log A; a replay
run (buffer + replay) fills log B. `wp_compare` checks, **per listener id**, that the ordered subsequence of
events it sees is identical (same-listener order is the invariant; cross-listener/cross-sector global
interleaving is allowed to differ, which is the expected stage-1 shift). MD cue activations are compared as
an order-independent multiset (count + XOR of cue ids), because MD is universe-level. `wp_compare` returns
the first differing event type. Offline the test runs both A and B over an identical synthetic scene and
asserts equivalence; natively this is two identical runs (mode A, then mode B) diffed offline (a
`WALKCENSUS`/record log per run), per the design's A-vs-B procedure.

## 4. The four reviewer points

1. **AddTempZoneEvent stays synchronous, inline, under the creation lock.** Yes. `wp_post` returns 0 for type
   0xa (never buffered), so the detour calls the original PostEvent inline; the design's CL around
   GetOrCreateZoneAt is the native layer's responsibility at H8–H11. Tested: `wp_post(e_tz)` returns 0,
   `posts_temp_zone_inline` increments, the temp-zone listener (the sector itself) is delivered during the
   walk in both the normal and replay runs.
2. **The source UniverseID is stored per buffered post.** Yes. Each op stores `src_id = *(uint64_t*)(src+8)`
   (the same field PostEventDeferred stores). At replay the source is re-resolved by id via
   ComponentRegistry::Find; a gone source drops+frees the event. Tested (incl. the gone-source path).
3. **Replay happens in tree order before 0x1092e80.** Yes. The buffer appends in walk order (tree order);
   `wp_replay` runs at `wp_walk_end`, which the native galaxy-call wrapper at 0x9fdf95 invokes right after the
   walk returns to 0x9fdf9b — before the 0x1092e80 call at 0x9fe07b. (Fallback: wrap the 0x9fe07b call;
   that reorders the 0x470/0x478/0x8e8450 step, noted as unknown-impact in the design.)
4. **A normal-vs-replay listener comparison exists.** Yes — §3 and `wp_compare`, with the offline A-vs-B test
   proving per-listener equivalence and the difference path reporting the offending event type. Cue
   activations compared as a multiset.

## 5. Hook sites (registered in rewrite-coord/hooks.tsv; byte-pinned in signatures.py)

| site | kind | signature (first N bytes SHA-256, pinned) |
|---|---|---|
| 0x9fdf95 | galaxy walk call site `call [rax+0x1510]` (6 bytes) → Dispatch via a pointer cell | galaxy_walk_call |
| 0x99a3f0 | PostEvent entry detour | post_event (32) |
| 0x99a870 | CancelPendingEvents entry detour | cancel_pending_events (32) |
| 0x99c470 | DeliverEvent wrapper (census + compare log) | deliver_event (32) |
| 0x99abe0 | FlushCommandQueue (ESL on workers, stage 2) | flush_command_queue_wp (32) |
| 0x2b1ff58 | Sector::Move vtable slot — **chain only** (owners: attention-hooks + walkprof); feed `wp_sector_enter/exit` | (walkprof's existing walk_sector wrapper) |

`dev/check_hook_signatures.py ../x4-re/bin/X4.exe` → 21 signatures PASS (the 5 new ones above included), no
files modified.

## 6. Precise native-patch spec (for the lead / native lane; NOT armed here)

The engine is complete; the remaining work is the in-process installation, which is a build-pinned code
patch that must be byte-verified and restored on disarm and fail closed (ENGINE-INSTRUMENTATION.txt). I did
not arm it because native testing is gated (quiet.lock; lane not READY) and a wrong entry detour crashes the
process; per the task, I specify it precisely.

**Galaxy-walk wrapper (0x9fdf95).** Replace the 6-byte `call qword ptr [rax+0x1510]` (bytes `ff 90 10 15 00
00`) with `call qword ptr [rip+disp]` (`ff 15` + rel32), where the pointer cell holds `&Dispatch`. rcx already
= galaxy, rax = galaxy vtable at the patch point, so `Dispatch(void* galaxy)` is `__fastcall`. The cell must
sit within ±2 GB of 0x9fdf95: allocate a code cave in the image (an existing padding/alignment gap after a
function) or a VirtualAlloc reservation near the image base; store the 8-byte cell there. `Dispatch` =
`wp_walk_begin(); ((fn)(*(void***)galaxy)[674])(galaxy); wp_walk_end();` — reading galaxy slot 674 composes
with walkprof's timing wrapper if present. Verify `galaxy_walk_call` before patching; restore the 6 bytes on
disarm.

**Entry detours (PostEvent 0x99a3f0, CancelPendingEvents 0x99a870, DeliverEvent 0x99c470).** A function-entry
hot-patch to a 14-byte absolute jump to the detour, with a trampoline that re-executes the overwritten
prologue then jumps back. Prologue notes (build-pinned, verified against the pinned exe):
- PostEvent first 14 bytes = 3 whole instructions: `48 85 d2` (test rdx,rdx), `0f84 1d030000` (je 0x99a716),
  `48 89 5c 24 08` (mov [rsp+8],rbx). The `je` is a rip-relative jcc, so its rel32 **must be relocated** in
  the trampoline (new rel32 = old target − trampoline instruction end). None of the other prologues in this
  set has a branch in the first 14 bytes (Cancel: movsd/push/push/sub; Deliver: mov/push×…; all
  position-independent).
- The detour body calls the engine: PostEvent → `if (wp_post(rcx,rdx,xmm2,r9b)) return; else <trampoline>`;
  Cancel → `if (wp_cancel(rcx,edx,xmm?)) return; else <trampoline>`; Deliver → `wp_deliver_enter(src,ev);
  <trampoline>; wp_deliver_exit(src,ev);` (and, for the listener histogram/compare log, read the event's
  listeners via GetListenersForType and call `wp_deliver_listener` per listener, or add a finer hook).
  ABI: PostEvent(rcx=src, rdx=event, xmm2=double time, r9b=imm); Cancel(rcx=src, edx=type, xmm2=minTime).
- Verify `post_event`/`cancel_pending_events`/`deliver_event` before install; restore the saved prologue on
  disarm; fail closed (any miss → detours not installed, `wp_mode` stays OFF). Watchdog: a per-frame check
  that the detours still hold their bytes.

After install, call `walkpar_set_mode(WP_CENSUS)` (stage 0) or `WP_REPLAY` (stage 1). The ai-driver binds its
AI-phase buffer to the same `walkpar_api()` (scope_begin/end, replay_tasks, buffer_op) — see questions Q4/Q7.

If you (lead) prefer, I can write the trampoline installer as its own reviewed patch once the lane is READY
and I can byte-verify + lifecycle-test it natively; I did not want to ship an unvalidated in-process patcher.

## 7. Tests (offline only; no native result claimed)

- `python native/engine/build.py --zig <zig> --fixedtick --test` — DLL builds with `-Wall -Wextra -Werror`;
  `test_walkpar.exe` PASS (6 groups): stage-0 census counts (posts/imm/sched, deliveries, cross-sector
  listener flag, sector sum/max and ideal speedup); stage-1 normal-vs-replay equivalence per listener;
  AddTempZone stays synchronous + source id stored/re-resolved; cancel-against-buffer, gone-source
  drop+free, overflow fail-closed; difference detection reports the event type; cue multiset; shared
  scope + ordered replay (ai-driver surface); foreign-thread pass-through. `test_fixedtick.exe` PASS
  (unchanged). `test_simprof.exe` is unrelated (includes only simprof.c) and is flaky under heavy
  concurrent CPU load (it suspends a busy worker; documented as noisy) — it passes on isolated re-runs.
- `python dev/check_hook_signatures.py ../x4-re/bin/X4.exe` — 21 signatures PASS, no files modified.
- `python native/engine/check_lua_abi.py …` — PASS (module loads; argument/coroutine/non-X4 rejection).
- `… -m unittest tests.test_fixed_tick_probe` — 19 PASS (2 new: WALKCENSUS line, WALKREPLAY + loud refusal).

## 8. Native steps needed (schedule when native-lane.md is READY and quiet.lock is gone)

In lane B1/B2 (profiles 45850/45851), new disposable world, through `dev/x4lane.py`:
1. Install the entry detours + galaxy-call wrapper per §6 (build-verified, fail closed), then
   `walkpar_set_mode(census)`.
2. **Stage 0 census** under the three loads (design §6.1): single client on a highway; four clients in four
   sectors; a 100-ship battle + mining fleet. 10 min each. Capture the WALKCENSUS/WALKREPLAY lines. Apply the
   decision rule (drop stage 2 if Σsector − deliver − CL-candidate < 25% of frame, or the largest sector > 60%,
   or cross-sector non-event writes > 0 outside the listed paths).
3. **Stage 1 A-vs-B**: same save, fixed dt (use the fixed-tick probe's fixed60), 600 frames then a 10-min
   soak. Run normal (mode A) and replay (mode B); diff the per-listener logs offline; report any difference
   with its event type; check the soak invariants (object/temp-zone/orphan counts, MD cue counts, AI wake
   counts, "Too many recursive cue actions" / "abandoned cue" / "Failed to assign an ID" log counts).

## 9. Open questions

- The DeliverEvent per-listener histogram wants each dispatched listener's RTTI; the cheapest native hook is
  to read GetListenersForType at the Deliver enter (walk src+0x28) rather than a per-listener callback —
  confirm with the first census run.
- SendEventImmediate (H5) de-dup answered from the buffer is not yet implemented (pass-through today); add
  only if a census shows re-post duplication. Coordinated with ai-driver (Q4).
- The galaxy-call pointer-cell/code-cave location (an in-image gap within ±2 GB) needs picking against the
  live image; specified, not chosen.
- ecs-core's barrier/message-bus interface (interfaces.md): the walkpar op record is proposed as the shared
  per-task op buffer for stage 2 (Q8).
