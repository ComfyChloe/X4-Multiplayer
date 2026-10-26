# Rewrite: per-sector ECS core, scheduler, shadow framework, mirror capture (ecs-core)

Owner: ecs-core agent, branch `contrib/ferrite-rw-ecs-core` (from `contrib/ferrite-rewrite-base` e63d9cf).
Own files: `rust/x4mp-sim/`, `native/engine/mirror*`, `native/engine/test_mirror.c`, `dev/mirror_probe.lua`,
`tests/test_mirror_probe.py`, this report. Interface: `rewrite-coord/interfaces.md` (section x4mp-sim).
Program and method: `reports/REWRITE-PROGRAM.md`.

**Status (2026-10-26):** the crate and tools are implemented and tested offline; the mirror probe has run
natively on lanes (section 6). Native results are marked **NATIVE**; everything else is synthetic.

## 1. What the core is

Each sector is its own world. Replacement systems run per world on worker threads; cross-sector effects
are messages and structural commands applied at a frame barrier in the original sequential order.

| piece | file | summary |
|---|---|---|
| ids | `src/id.rs` | `UniverseId` (engine layout: slot+1 in bits 0..24, generation 25..40), `EntityId` (ours, stable across migration), `SectorIx` (tree order), `OrderKey`, `IdMap` |
| components | `src/comp.rs` | engine field types: `Mat4f` rows of f32 (row 0 position, rows 1..3 basis), velocity 2x4 f32, hull f64, class id, attention i32, tree, flags, controller, owner/cover factions, order state, per-entity RNG state |
| storage | `src/world.rs`, `src/column.rs` | dense SoA columns per world, rows sorted by tree-order key; spawn/despawn/migrate are staged and committed by a stable merge; per-sector typed resources |
| frame | `src/universe.rs`, `src/sched.rs` | persistent worker pool (pinned jobs run on the calling thread), `System::run_sector` per world, ordered barrier, `System::barrier` serial hook, phase snapshot of published columns |
| messages | `src/bus.rs` | system-addressed envelopes; targets sector, entity (follows a same-barrier migration) or the barrier hook; order keys for non-tree original orders |
| randomness | `src/rng.rs` | the engine generator with the exact `Random::*State` semantics; per-entity streams |
| shadow | `src/shadow.rs` | exactness policy, comparator, replay, online shadow statistics, feature switch |
| capture | `src/capture/` | format v1 writer/reader, mirror loader, capture replay harness, native cross-check |
| tools | `src/bin/` | `x4mp-capdump` (info, frame, diff, check), `x4mp-sim-bench` |
| native | `native/engine/mirror.h`, `mirror_layout.py` | the capture probe in the fixed-tick DLL |

### Determinism model

- A job mutates only its own world's values. Structure changes and every cross-sector effect go into its
  outbox. Other sectors are read only through the snapshot taken at phase start.
- The barrier drains all outboxes in the total order **(order key, SectorIx, emission sequence)**. Keys are
  0 unless a job sets one, so the default is tree order of sectors, then emission order inside the sector:
  exactly the order a sequential walk would produce. `Outbox::set_order` lets a system reproduce another
  original order (the AI director's wake order: key = pass << 32 | wake index; see Q5).
- Commands are applied first, worlds committed, then messages routed (an entity-targeted message follows
  a migration of the same barrier; a missing target is dropped and counted).
- Messages are delivered to the addressed system's next run (by default the sender itself, next frame;
  `send_to` reaches a later system of the same frame). A first version delivered to "the next phase"; the
  thread-invariance test with two systems showed that this silently dropped messages, so it was changed.
- Multi-pass systems (`System::max_passes`, requested by ai-orders for the AIDirector pass loop, up to
  0x21 passes): `step` repeats the system, each pass a full phase with its barrier, until no message for it
  waits and no job or hook called `request_pass`; its own messages are then handled in the next pass of the
  same frame. The pass count is an OR over worlds of deterministic state, so it is thread independent
  (tests/passes.rs); a bound hit with work left is reported (`more`) and the rest arrives next frame.
- Randomness comes from per-entity streams in a column; nothing depends on which thread ran a job.
  Scheduling uses measured job time only to order work (largest first), never for results.

## 2. Findings from the engine (understand step; Windows build 23660954)

Read by this agent with a capstone-based helper over `x4-re/bin/X4.exe` (scripts in
`x4-re/scripts/rw-ecs-core/`, private dumps in `x4-re/work/rw-ecs-core/out/`). Descriptions only.

1. **Tree order.** `Component::Move` (0x3e4f40) iterates the child container at +0xa8, then the one at
   +0xa0, calling slot 674 (Move) on each child. A container (begin iterator 0x314fa0) holds a live count
   at +0x20 (0 means empty), a vector of 0x20-byte blocks at +0x08..+0x10, each block a vector of
   component pointers at +0x00..+0x08; iteration goes block by block, entry by entry, skipping null
   entries. This order is what the mirror records and what `SectorIx` means. The class overrides
   use the same order: Galaxy, Cluster (after its own physics step, 0x3b8280), Sector (0x784230) and
   Zone (0x834950) reach their children through `Positional::Move` (0x748120), which visits +0xa8 then
   +0xa0 with the same iterator; only class-specific early returns (e.g. low-attention sectors) skip
   subtrees.
2. **Class ids.** Slot 571 (GetClassID) of all 120 component classes has one of four shapes; most return
   one id when Component+0x68 (mode) is 0 and another (usually 0x49 object) otherwise, e.g. ship_s 0x5b
   or 0x49; accessory 0 or 0x4d; detector 0x25 or 0x24. The table with both ids per vtable is in
   `mirror_layout.py`; the names are the engine's table at 0x22c6790.
3. **Where a local pose lives.** `Positional::GetLocalTransform` (0x7461c0) asks the movement controller
   at +0x200; without a controller `Component::GetLocalTransform` (0x3db640) yields identity (position
   row 0, basis rows from the identity constants). `NavigationController` (vtable 0x2c51158): with the
   state relative to the parent and no reference component, `GetTransformImpl` (0xea5a90) returns the
   current pathing slot's mat4 (slots via controller+0x30, stride 0x1a0, index `g_PathingStateBufferIndex`
   0x38f4ee4; velocity at slot +0x40/+0x50, reference id at +0xb8); otherwise it uses the cached matrix
   at +0x60 or composes with the reference component. `StaticMovementController` (0x2a34b38) stores its
   mat4 at +0x10 (new label 0x10ba20). Slot 676 of zones, sectors, ships, docking bays is a no-op.
4. **Composition.** A transform relative to an ancestor (`GetTransformRelativeTo` 0x3db8a0) applies each
   component's local transform from the object upwards to the accumulated matrix. One step computes per
   lane: row 0 `((y*t2 + x*t1) + z*t3) + t0` (lane 3 uses w for x, y, z), rows 1..3 `(y*t2 + x*t1) + z*t3`,
   all in single precision (SSE mulps/addps, no fused multiply-add). `capture::check::apply_local`
   reproduces this order; a unit test shows that a different association gives a different f32 result.
5. **Exports used for the cross-check.** `GetPositionalOffset(id, 0)` returns row 0 of the local transform
   (no ancestor); `GetObjectPositionInSector(id)` returns row 0 of the transform composed up to the first
   sector ancestor, both at the calling thread's game time (TLS+0x300).
6. **Other fields.** hull f64 at +0x280 (GetHullPercent), true/cover owner at +0x348/+0x350, faction id as
   an MSVC string at faction+0x10 (size +0x20, capacity +0x28), order vector +0x4b0..+0x4b8 and current
   index +0x4e0, attention +0x1ec, registered byte +0xd1, death state +0xd3, galaxy at
   `*g_Universe(0x3d198f8) + 0x228`, session seed 0x3d19860, TLS game time at TLS block +0x300.
7. **Random library** (0x14ddb30..0x14ddfe0): `U64/I64/U32/I32State` return `state % n` after one step and
   do not step for an invalid n; `FloatState(max)` steps only for `max >= 0` (not NaN) and returns
   `((float)low32 * 2^-32) * max` in f32, which can equal `max` by rounding. `rng::EngineLcg` matches this.

## 3. Engine-side capture (mirror probe)

Placement: inside the fixed-tick probe's VBI slot-5 callback on the MoveWorker. Slot 3 has finished the
universe update (walk, pathing join and slot flip, physics, BuildCache), slot 4 is input; nothing changes
the universe before the UI in slot 6. Hooking `Sector::Move` was not needed (it is shared and would see
mid-walk state).

Data: one record per world root (galaxy, each cluster, each sector) with the core columns of
`capture::core_schema()` (221 bytes per row). Recorded classes: everything with an Object or Space base
(ships, stations, gates, missiles, asteroids, zones, sectors, ...) plus classes a visitor asks for; the
walk always descends (docked ships live under station modules). Options: record every component, skip the
nav cache, skip sector subtrees below an attention level.

Safety: reads only; no engine function is called and nothing in the engine is written. Arming verifies 18
code ranges (SHA-256), every class vtable's GetClassID entry plus a combined hash of the 120 GetClassID
bodies, and the three controller vtables' GetTransform entries; any mismatch refuses
(`FEATURE mirror DISABLED reason=...`). Every pointer is range-checked against committed readable memory
(VirtualQuery with a per-frame region cache); unknown vtables, malformed containers and depth overflow
skip the subtree and count `walk_errors`. Serialization runs on the MoveWorker; a writer thread computes
the frame checksum and writes; up to 8 frames may queue, then frames are dropped and flagged.
`mirror_layout.py --verify <X4.exe>` re-derives every pinned value from the file (passes); no base
relocation falls into any pinned range; no pinned range overlaps a patch site in hooks.tsv.

Known limits: the PlayerController pose and poses with a reference component are recorded raw (pose_ref
set) but not checked; the mass-traffic and region threads may still run during slot 5 (the engine's own
walk has the same exposure in slot 3); the full-tree walk costs one pass over every component per
captured frame (unmeasured natively).

## 4. Shadow framework for system agents

- Policy: integers exact; floats bit-exact unless a documented tolerance is configured per field path
  (`Policy::exact().with("transform", Tol::Ulps(1))`); `-0.0 != 0.0` under Exact.
- `Comparator`: per-field statistics (compared, mismatched, within tolerance, max ULP, max abs), first
  differences with full path, frame, sector, entity and hex values. Paths are interned (one trie node per
  index-free path holding its statistics and its resolved tolerance); the full path with indices is rendered
  only for a recorded difference. Cost about 2.5 ns per compared value (was 36 ns with string-keyed
  statistics); no allocation per frame unless a difference is recorded. A self-diff of the 60-frame native
  capture (60.5M values) takes 1.8 s instead of 4.9 s, most of it reading and mirroring the files.
- `replay` (recorded inputs/outputs), `Shadow` (online, original authoritative), `FeatureSwitch`
  (`FEATURE <name> DISABLED reason=...`, sticky).
- `capture::replay_frames`: mirror engine frame k, run the systems, compare their columns with frame k+1
  (consecutive pairs, engine dt). Tests: an identical system replays exactly; a variant forming the product
  in f64 is caught at frame 1 and stays within 1 ULP (documented-bound example).
- `World/Universe::compare_with` with or without entity-set checks (row-aligned fast path when both worlds
  hold the same uids in the same order); `x4mp-capdump diff` for two captures.

## 5. Tests (offline)

| suite | result |
|---|---|
| `cargo test -p x4mp-sim` | 68 pass, clippy clean |
| thread invariance | 24 sectors x 120 ships, 90 frames, 2 systems: state hash per frame, message log, barrier log and counters byte-identical for 1, 2, 16 threads (6145 messages, 309 spawns, 581 despawns, 286 migrations) |
| structure property test | 12 seeds x 400 random spawn/despawn/migrate/commit ops against a reference model |
| capture | round trip; every single-byte corruption detected; truncation, version, magic, schema refusal; mirror across frames (handles kept, migrations, despawns); replay determinism |
| native golden | the C test's capture of a fake tree read by Rust field by field (header, dictionary, 2 frames, mirror, round trip, check logic) |
| compare | nested index paths, index-free statistics, changing field order, name collisions (`x.y` vs `x`/`y`), reset, max_diffs, row-aligned vs uid-matched world compare give the same differences |
| `build.py --fixedtick --test` | fixedtick, simprof, mirror pass (simprof's 3 % overhead assertion is load-sensitive) |
| LuaJIT | fixed_tick_probe 17, mirror_probe 5 pass |

Benchmark (`x4mp-sim-bench`, synthetic load): smoke-tested at 2,000 entities only. The 10k-200k x 8-200
sector x 1-32 thread matrix needs the quiet window (it would compete with the live client test).

## 6. Native results and steps

Procedure: lane run with the fixed-tick probe overlay plus a mirror overlay (`dev/mirror_probe.lua` and
`X4LAN_MIRROR_*` settings), wait for `[X4LAN] MIRROR done`, then `x4mp-capdump info <capture>` and
`x4mp-capdump check <capture> <run>/x4lan-debug.txt`. Pass criterion: `MIRROR armed`, no
`FEATURE mirror DISABLED`, `verdict EXACT`. All runs are functional only (other X4 instances running).

| run | probe | frames | result |
|---|---|---|---|
| **NATIVE** mirror-validate-1 (b4) | 57c2ed2 | 10 x 16.5k objects, 282 roots | walk_errors 0; local 320/320 and Object sector 282/282 bit-identical; 913 ms/capture (old region cache) |
| **NATIVE** mirror-validate-2 (b3) | f772266 | 60 | walk_errors 0; Object sector 1732/1732 exact; local 1901/1920: the 19 misses are two superhighways (class overrides GetLocalTransform, see below); 262 ms/capture |
| **NATIVE** mirror-validate-3 (b6) | 247642b (v1.1) | 60 x 16.3-16.7k objects | walk_errors 0; **local 1920/1920** (10 of them superhighways) and **Object sector 1758/1758** bit-identical; 271 ms/capture; 2,570 VirtualQuery/frame |
| **NATIVE** mirror-validate-4 (b1, run mirror-005) | 3179ba6 | 60 x 16.7k objects | walk_errors 0; **EXACT**: local 1920/1920 (37 superhighways), Object sector 1707/1707; **429,325 components visited per frame** for 16,690 recorded rows; walk 272.8 ms, serialize 3.0 ms (of 275.9 ms); region cache never full (2.7k VirtualQuery/frame) |
| **NATIVE, TIMING** mirror-cost-timing (b3, `--timing`, no other X4) | 3179ba6 | 60 x 16.6k objects | walk_errors 0; EXACT: local 1920/1920 (50 superhighways), Object sector 1677/1677; **112.2 ms per captured frame (walk 111.1, serialize 1.2)**, 428,297 components visited: 0.26 us each |
| mirror-ab-1 | 96730d9 | 60 | queued: per-class visit histogram, A/B prefetch (odd frames) |

Findings from the native data: six classes override GetLocalTransform (slot 648): Explosive, Missile,
Bomb (0x567aa0), Highway (0x5f79f0: adds the vector at +0x570 to row 0, then the standard step),
HighwayScene, HighwayBooster. The capture flags them (`class_flags` bit 512) and records the Highway
offset; `check::local_step` models Highway and refuses the others. Out-of-sector navigation slots are
updated sparsely: at attention 2, 460k of 583k consecutive-frame samples keep the identical pose with a
non-zero velocity (`x4mp-capdump kinematics`).

Capture cost: the walk is memory-latency bound (0.26 us per visited component on a quiet PC, 0.64 us under
load; 26 components visited per recorded row; results/2026-10-26-ecs-core-mirror-cost.md). Next: the class histogram decides between pruning subtrees that cannot hold recorded
classes and prefetching; the A/B flag measures prefetching inside one run.

Pending: capture cost (histogram, A/B); benchmarks in the quiet window (x4lane hold);
cross-check with the walk agent's collector order once both run in one lane (Q8).

### Bounded sector capture (26 October, Codex continuation)

The opt-in round-robin capture starts with one full frame, then walks the galaxy/cluster levels
and up to `rr_sectors` cold sector subtrees in rotation. `rr_pairs=1` holds the same selection
for two capture attempts; `hot_attention>0` additionally captures every sector at or above that
attention. Settings: `X4LAN_MIRROR_RR`, `X4LAN_MIRROR_RR_PAIRS`, `X4LAN_MIRROR_HOT` in the existing
mirror Lua driver. Defaults are zero, retaining the full capture path and its golden bytes.
No engine pointer persists between frames. Newly discovered sectors enter the next rotation.
This bounds cold-sector count, not milliseconds: the first full frame, a large selected sector,
or many hot sectors can still be expensive. It is a diagnostic capture, not a live authority.

Subset FRAM records have flag 4. Their root indices are local to that record, not stable ECS
SectorIx values. `mirror()` rejects subset records (and truncated records) before mutation;
full-world replay skips those pairs. Call `mirror_subset()` explicitly after a full mirror to
refresh known roots by UID. Its report identifies `updated_sectors`; omitted roots retain stale
values. Missing entities are removed only from observed roots. Migration into an observed root
preserves handles; migration from an observed root to an omitted destination is indistinguishable
from deletion until the destination is observed, so handle continuity is not guaranteed there.
Do not feed this mixed-age mirror into authoritative simulation or label it a full-world snapshot.
Pair capture does not by itself justify isolated sector replay with cross-sector dependencies.

Offline validation: C synthetic tree covers rotation, wraparound, two-frame holds, additive hot
sectors, new-sector budgeting, root IDs, serialized subset flags and unchanged full-frame golden
bytes. Rust covers explicit subset opt-in, stable root mapping, migration, omission versus empty
roots, initialization/truncation refusals and full-world replay exclusion. LuaJIT covers settings
and subset statistics. Native round-robin correctness/cost acceptance remains pending.

## 7. How to use it (system agents)

```rust
#[derive(Clone, Default, Debug)] struct Heat(f32);
x4mp_sim::impl_fields!(newtype Heat);
impl x4mp_sim::Component for Heat { const NAME: &'static str = "heat"; }

struct Cooling;
impl x4mp_sim::System for Cooling {
    fn name(&self) -> &'static str { "cooling" }
    fn run_sector(&self, ctx: &mut x4mp_sim::SectorCtx<'_>) {
        let dt = ctx.frame.dt as f32;
        for h in ctx.world.col_mut::<Heat>() { h.0 -= h.0 * 0.1 * dt; }
    }
}
// let mut u = x4mp_sim::Universe::new(seed); u.register::<Heat>();
// u.step(&x4mp_sim::Pool::new(0), &[&Cooling], &frame);
```
Capture columns for your own data: reserve an id range in interfaces.md (economy has 0x200..0x23f), add a
visitor in your probe (`mirror_add_visitor`), and replay with `capture::replay_frames`.
