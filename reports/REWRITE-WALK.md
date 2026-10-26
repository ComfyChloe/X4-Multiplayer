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
  a shared scope/ordered-replay API for the ai-driver AI phase, and the detour bodies + native arming.
  Included by `fixedtick.c` after `walkprof.h`.
- `native/engine/wpdetour.h` — the crash-safe in-process layer (§6): build-pinned entry detours and the
  galaxy call-site patch, with a ±2 GB trampoline pool, suspend-others + Rip-clash abort (fail closed),
  rollback and restore.
- `native/engine/test_walkpar.c`, `native/engine/test_wpdetour.c` — synthetic tests in the style of
  `test_fixedtick.c`; built and run by `build.py --fixedtick --test`.
- Additive edits to shared files (minimal, per the rules):
  - `signatures.py`: `WALKPAR_SITES` (byte-pinned detour targets + the galaxy call site) and
    `x4_walkpar_checks[]` / `WP_GALAXY_CALL_RVA` emitted into the C header.
  - `fixedtick.c`: include walkpar.h; `walkcensus`/`walkreplay` start modes; `walkpar_arm` (verify) +
    lazy `walkpar_tick` arming in `move()`; sim-bracket gated by `walkpar_dispatcher_on()`; disarm in
    `disarm_because()`; a WALKCENSUS/WALKREPLAY block in `stats()`.
  - `build.py`: add `walkpar`+`wpdetour` to the `--fixedtick --test` targets.
  - `dev/fixed_tick_probe.lua`: `WALKCENSUS` / `WALKREPLAY` log lines and a loud `FEATURE walkpar DISABLED`.
- RE: `x4-re/labels/rw-walk.tsv` (4 labels for the hot functions below), `x4-re/scripts/rw-walk/rwdis.py`
  (private capstone helper; analysis only).

`walkpar_arm()` verifies the build-pinned bytes; `walkcensus`/`walkreplay` then arm the detours lazily on
the MoveWorker (`walkpar_tick`). Any verification miss or freeze failure fails closed (no patch, `wp_mode`
stays OFF). Census is armable natively now; **replay is gated on the reviewer's go** (set_mode refuses
WP_REPLAY until the dispatcher is installed). Sites registered in `rewrite-coord/hooks.tsv`.

## 2. Stage 0 — hazard census (measurement only)

Driven per frame by: the galaxy-call dispatcher at 0x9fdf95 (`wp_walk_begin/end`, both census and replay),
a PostEvent entry detour (`wp_post`), a DeliverEvent wrapper (`wp_deliver_enter/exit/listener`), and
counting detours on the creation/global paths (`wp_note_*`). **There is no Sector::Move body patch**: the
attention-load hooks (`x4lan_hooks_64.dll`) pin and re-verify a 738-byte range at 0x784230, so patching that
entry trips their integrity check (observed as `HOOKS_DISABLED` in the first attnload run). Per-sector
attribution instead comes from the source/listener sector resolver (`wp_sector_resolver` walks +0x70 to the
U::Sector id): a post's task is the source's sector, cross-sector delivery is listener-sector != source-sector.
Per-sector *timing* comes from walkprof (already chained on the Sector::Move vtable). Per frame it records:

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

`wp_deliver_listener` records a per-delivery tuple `(listener id, src id, type, payload)`. `wp_compare`
checks, **per listener id**, that the ordered subsequence of events each listener sees is identical
(same-listener order is the invariant; cross-listener/cross-sector global interleaving may differ, the
expected stage-1 shift). MD cue activations are compared as an order-independent multiset (count + XOR of
cue ids), because MD is universe-level. `wp_compare` returns the first differing event type, or
`WP_CMP_NODATA` (-2) when either log is empty or overflowed (`WP_CMP_MAX`) — so a run with no per-listener
data (e.g. the native DeliverEvent detour not emitting per-listener records) can never be a false "equivalent".

**The offline A/B test is SYNTHETIC only** (review #4): it runs normal then replay in one process over
identical scripted input and proves the replay engine preserves each listener's order. It is not evidence
about live data, where A and B come from different frames. **Native comparison method (chosen): the Linux
mid-frame fork** (the fork probe already proved mid-frame fork works) — the parent runs the frame normally
(log A), the forked child runs the same frame's buffered replay (log B), both over a pipe, so A and B are
the *same* frame. The two-instance alternative (two games from one save, fixed tick, seeded RNG) is rejected
for now because it needs frame-diet's camera-RNG split first (render slot 10 draws TLS+0x318) or it diverges
for unrelated reasons. Until the fork harness exists, stage-1 native validation uses the §6.2 soak
invariants (object/temp-zone/orphan counts, MD cue counts, AI wake counts, and the engine log counts of
"Too many recursive cue actions" / "abandoned cue" / "Failed to assign an ID") between a normal and a replay
run, not a per-frame A/B diff.

Cancel semantics (review #2): a buffered cancel kills only this task's **scheduled** (imm==0) posts of the
type (an immediate post was already delivered before the cancel in normal mode, so replay must still deliver
it), and frees the killed event (the engine's CancelPendingEvents deletes queued events). On buffer overflow
the cancel passes through to the original so the global schedule still sees it (review #3).

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
4. **A normal-vs-replay listener comparison exists.** Yes — §3 and `wp_compare`. The offline A-vs-B test is
   SYNTHETIC (one process, identical input); the native comparison is the Linux mid-frame fork (§3), with
   `WP_CMP_NODATA` guarding against a false pass. Cue activations compared as a multiset.

## 5. Hook sites (registered in rewrite-coord/hooks.tsv; byte-pinned in signatures.py)

| site | kind | signature (first N bytes SHA-256, pinned) |
|---|---|---|
| 0x9fdf95 | galaxy walk call site `call [rax+0x1510]` (6 bytes) → Dispatch via a pointer cell | galaxy_walk_call |
| 0x99a3f0 | PostEvent entry detour | post_event (32) |
| 0x99a870 | CancelPendingEvents entry detour | cancel_pending_events (32) |
| 0x99c470 | DeliverEvent wrapper (census + compare log) | deliver_event (32) |
| 0x99abe0 | FlushCommandQueue (ESL on workers, stage 2) | flush_command_queue_wp (32) |
| ~~0x784230~~ | Sector::Move entry detour **WITHDRAWN** — the attnload hooks pin+re-verify a 738-byte range at 0x784230, so patching the body trips HOOKS_DISABLED; sectors attributed via the resolver instead | — |

`dev/check_hook_signatures.py ../x4-re/bin/X4.exe` → 21 signatures PASS (the 5 new ones above included), no
files modified.

## 5a. Prologue cuts and the relocation assertion (review #5)

The trampoline copies each function's first `cut` bytes (whole instructions) and relocates only the one
jcc (PostEvent's `je`). Disassembly of each cut against the pinned build 23660954 (bytes SHA-pinned in
signatures.py); **every cut has no RIP-relative operand and no `call`** (a suspended thread whose return
address fell inside a cut would otherwise return into patched bytes, which the Rip check does not catch):

- `0x99a3f0` PostEvent, cut 14: `48 85 d2` test rdx,rdx; `0f84 1d030000` je 0x99a716 (relocated);
  `48 89 5c 24 08` mov [rsp+8],rbx. (je target 0x99a716 is outside the 14-byte range.)
- `0x99c470` DeliverEvent, cut 16: `48 89 5c 24 20` mov [rsp+0x20],rbx; `55 56 57` push rbp/rsi/rdi;
  `41 54` push r12; `41 55` push r13; `48 83 ec 40` sub rsp,0x40.
- `0x99a870` CancelPendingEvents, cut 17: `f2 0f11 5c 24 20` movsd [rsp+0x20],xmm3; `55 56` push rbp/rsi;
  `48 83 ec 68` sub rsp,0x68; `0f29 74 24 50` movaps [rsp+0x50],xmm6.

None loads the security cookie (no `lea`/`mov` against a RIP-relative `__security_cookie`) within the cut.
`test_wpdetour.c` validates the relocation + a jcc whose target is outside the stolen bytes.

## 6. Native-patch layer — IMPLEMENTED (wpdetour.h); census armable, replay gated on review

`native/engine/wpdetour.h` implements the in-process layer, build-pinned, byte-verified, restored on disarm,
fail-closed. It is validated offline by `test_wpdetour.c` (trampoline + jcc relocation + the call-site
patch + 4-thread concurrency + fail-closed-on-mismatch). **Census (`walkcensus`) is safe to arm natively**;
**REPLAY (`walkreplay`) is held until the reviewer clears it** — `walkpar_set_mode` refuses WP_REPLAY unless
the galaxy-call dispatcher is installed.

**Crash-safe install (both the entry detours and the call-site).** `wp_pool_reserve` reserves a ±2 GB
executable pool *before* the freeze. `wp_detour_install`: re-verify the pinned prologue bytes, build the
relocating trampoline, then `wp_suspend_others` suspends every other thread and **fails closed** — it aborts
(resume all, return 0, caller retries next frame) if any thread cannot be opened/suspended (review #6) or if
any thread's Rip is inside a patch range. Only then the 14-byte `jmp [rip+0]; <abs>` is written and the
prologue tail nop-padded. Full rollback if any site fails; the pool is never freed while armed. Arming is
lazy (`walkpar_tick` each frame on the MoveWorker) so a transient Rip clash just retries.

**Galaxy-walk dispatcher (0x9fdf95, stage 1).** `wp_callsite_install` replaces the 6-byte
`call [rax+0x1510]` with `call [rip+cell]` (the 8-byte cell in the pool holds `&wp_dispatch`). rcx is the
galaxy at the patch point, so `wp_dispatch(galaxy)` = `wp_walk_begin(); ((fn)(*(void***)galaxy)[674])(galaxy);
wp_walk_end();` — slot 674 composes with walkprof's timing wrapper. This call site runs once per frame on the
MoveWorker only, so a clash is effectively impossible. In replay mode the dispatcher brackets the walk (so
replay runs before 0x1092e80); the slot 0/15 sim-bracket is used only for census (`walkpar_dispatcher_on()`
gate in fixedtick.c).

**Detour bodies** (walkpar.h): PostEvent → `if (wp_post(src,event,time,imm)) return; else <tramp>`;
Cancel → `if (wp_cancel(src,type,minTime)) return; else <tramp>`; DeliverEvent → `wp_deliver_enter;
<tramp>; wp_deliver_exit`. ABI: PostEvent(rcx=src, rdx=event, xmm2 time, r9b imm); Cancel(rcx=src, edx=type,
xmm2 minTime). (No Sector::Move detour — see §2.) The DeliverEvent
per-listener records (for the compare log) are **not yet emitted natively** — a finer hook or a read of the
event's listeners at enter is the follow-up; `WP_CMP_NODATA` guards the gap. The ai-driver binds its AI-phase
buffer to the same `walkpar_api()` (scope_begin/end, replay_tasks, buffer_op) — questions Q4/Q7.

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

## 8. Native steps (lanes b1-b4 via dev/x4lane.py; census APPROVED, replay approved for a first short run)

1. **Stage 0 census** (`X4LAN_FIXEDTICK_MODE='walkcensus'`): a probe-only shakeout (baseline world), then the
   attnload 0,8 harness (`make-overlay attnload --steps 0,8 --durations 60,180 --warmup 20`). Capture the
   WALKCENSUS line and post posts/frame, imm share, xsector_post/listener, posts_rootlevel, deliver_ms vs
   sector_ms. Apply the design §6.1 decision rule for stage 2. (Timing is functional-only next to other X4;
   the sector-ms ceiling also comes from walkprof's earlier dedicated run, ideal speedup 3.96 at 8 sectors.)
2. **Stage 1 replay** (`walkreplay`): a few minutes, short. Auto-disarm on a degraded frame (built: wp_walk_end
   → wp_replay_abort → walkpar_tick disarms; lane `--fail-on "degraded=[1-9]"`). Watch replay_dropped and
   gameplay (no stuck orders, no duplicate temp zones). Record the §6.2 soak invariants.
3. **Stage 1 verification — Linux mid-frame fork compare (next build item, condition 4).** A/B equivalence
   stays UNPROVEN until this exists; do not call stage 1 "verified" before it. Plan: on the Linux build, at
   `wp_walk_begin` the probe `fork()`s (the fork-probe worktree proved mid-frame fork is safe); the parent
   runs the frame normally and emits the per-listener log A, the child runs the same frame's buffered replay
   and emits log B, both over a pipe; `wp_compare` (or an offline differ) checks per-listener equivalence on
   the *same* frame. Linux anchors: walk vcall L 0x1193339, PostEvent L 0x16c5fe0, Sector::Move slot 683 at
   L 0x3d1eda8, 0x1092e80 ≈ L 0x1097d70 (design §7). Needs the Linux build / VPS, which belongs to x4mods-61
   — coordinate access through the lead (not requested yet; after the replay run).

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
