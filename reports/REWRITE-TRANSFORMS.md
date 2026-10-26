# Transforms and the component view cache (`Component::BuildCache`)

Owner: transforms agent, branch `contrib/ferrite-rw-transforms` (base `contrib/ferrite-rewrite-base`).
Target: X4 9.00, Steam build 23660954 (X4.exe SHA-256 `19750a65...6891`). All addresses are Windows
RVAs (VA = 0x140000000 + RVA). Labels: `x4-re/labels/rw-transforms.tsv` (merged, 0 conflicts).
Stages: **1 understand** done; **2 capture/replay** done offline against the original machine code
(oracle, section 4) and NATIVELY (lane run xform-002, section 5); **3 shadow** native (1,500 frames;
rerun with the corrected rule in xform-003); **4 switch** (Rust build in place of `BuildCache`)
proven offline (section 6), native A/B run xform-003 queued. Native results are functional only
(other X4 instances on the PC).

## 1. Where the time goes (profile attribution)

Evidence: `X4-Multiplayer-fixed-tick/.local/evidence-238-withheld`
(native run by the lead, 25 Oct; 1 kHz MoveWorker samples, so samples per frame = ms per frame).
Tool: `python native/engine/xform_profile.py <log> --phase name=first-last` (window end times).

| ms per simulation frame | start 10-30 s | baseline 40-81 s | 8 sectors 110-262 s | after 270-281 s |
|---|---|---|---|---|
| BuildCache 0x3d4a80 inclusive | 6.03 | 3.85 | 3.68 | 2.13 |
| BuildCache self (the walk itself) | 5.52 | 3.33 | 1.70 | 1.17 |
| PropagateCacheFlags 0x3d46b0 inclusive | 0.50 | 0.50 | 1.79 | 0.80 |
| of which GetLocalTransform 0x7461c0 | 0.16 | 0.15 | 0.77 | 0.28 |
| of which attachment poses vfunc622 0x745920 | 0.17 | 0.16 | 0.27 | 0.18 |
| frame (all MoveWorker samples) | 20.2 | 17.3 | 29.7 | 19.3 |

- **100% of BuildCache samples are under `U::UniverseFrameUpdate` in slot 3** (call at 0x9fe549, after
  physics). None are on the camera/scene/UI path. The earlier reading "mostly a per-render-frame
  camera cost" does not hold: per frame the build costs about the same with 8 sectors (3.7 ms) as at
  baseline (3.8 ms); its *share* falls because the rest of the frame grows.
- The *walk* (self time) decays steadily from game start (5.5 to 1.2 ms per frame across the run,
  independent of the load steps) while the *refresh work* grows with the active sectors (0.5 to
  1.8 ms). Self time by recursion depth: at the start 3.7 of 5.5 ms are at depth 5 (objects directly
  in zones, galaxy = 1); with 8 sectors the refresh at depths 4-6 dominates. Interpretation (to be
  confirmed by the capture): the walk is dominated by one-shot specifications from spawning and
  zone changes, which open whole zones for two frames (section 3).
- Camera path (slot 10, frame-diet's area): `Camera::vfunc3` 0x983160 2.1-2.5 ms, `Scene::vfunc915`
  0x779a00 1.8-2.2 ms, `Positional::vfunc693` 0x751770 1.1-1.5 ms per frame, all only under
  `X4::GameRender` (slot 10). `GetLocalTransform` is also called there (0.10-0.13 ms), by AI
  (0.07-0.11), the walk (0.03-0.10) and the UI (0.03). `GetTransformRelativeTo` 0x3db8a0 totals
  0.18-0.31 ms (AI, walk); `GetDistanceSqTo` 0x3e0c40 is negligible (<0.02 ms).

## 2. What BuildCache computes (RE; high confidence where verified by the oracle)

**Frame position and double buffer.** In `UniverseFrameUpdate` 0x9fdef0, after
`XPhys::Scene::Step` x2: `idx` = 0x2f4b71c on the MoveWorker (TLS+0x314 != 0) else 0x2f4b718
(clamped to 1 with "Galaxy::PopulateCache: Invalid cache index value"); time = TLS+0x300;
`PropagateCacheFlags(galaxy, idx, galaxy+0x1e0 | 0x6b)` (0x9fe53c), `BuildCache(galaxy, idx, time)`
(0x9fe549), `FlushOrphanCacheData` 0x8e7c90 (orphans in Universe+0x280 get the same
refresh/build; components in Universe+0x240 have cache idx cleared and copied to the other cache
under CS 0x3d79118 after readers 0x3d79110 drop to 0), then `PublishComponentViewCache` 0xef41e0.
Indices: write side W=0x2f4b71c (MoveWorker) / R=0x2f4b718; read side A=0x38f4ee0 (MoveWorker) /
P=0x38f4eb4 (other threads; 427 reader sites, e.g. NavigationController::vfunc33, formation,
pathing). MoveWorker publish swaps A and W; the main thread at the frame boundary sets P=A, R=W.
So every frame the MoveWorker rebuilds cache W while all threads read the other one; readers on
the MoveWorker see the new cache right after the publish, other threads from the next boundary.

**Component fields** (all verified): +0x70 live parent; +0xa0 attachment container and +0xa8 child
container (bucketed: +0x08/+0x10 bucket vector of 0x20-byte {begin,end,..}, +0x20 count, +0x24
holes); +0xd8 specification list; +0xe0 + idx*0x80 view cache {+0x00 parent (refcounted), +0x08
parent connection, +0x10 child vector, +0x28 pose block (0x70: local pose 4x4 f32, scale 4 f32,
velocity 8 f32), +0x30 attachment poses, +0x38 attention, +0x3c dirty bits}; +0x1e0 own cache
flags; +0x1e4 clear mask (bit set = PCF clears the dirty bit after refreshing; moving components
clear bits 8/0x10 of the mask, so their pose stays dirty); +0x1e8 number of specification entries
in the subtree, own included; +0x1ec attention.

**Cache specifications** (entry 0x20 bytes: kind, flags, truncation class, truncated byte, interval
f64, last f64; list = small vector, 4 inline). Created by `AddCacheSpecification` 0x3d5180 (merges
into an entry of the same kind; kind 7 also writes +0x1e0; a positive interval draws
`Random::FloatState` from the calling thread's TLS stream for the phase, but no shipped call site
passes one), removed by `RemoveCacheSpecification` 0x3d5440; counts propagate up the live parent
chain (0x3d55c0 / 0x3d55e0).

| kind | created by | flags | truncated at | interval |
|---|---|---|---|---|
| 1 | Galaxy ctor 0x5c0bc0 / 0x5c0830 | 0x6b | sector (0x58) | 0 (every frame, permanent) |
| 2 | `Sector::vfunc701` 0x78d870 (sector becomes active; removed when inactive) | 0x4f | zone (0x6d) | 0 |
| 3 | `Zone::vfunc701` 0x843fd0 (zone becomes active) | 0x7f | none | 0 |
| 4 / 5 | `Highway::vfunc701` 0x5ffb60 | 0x7f / 0x4f | none / zone | 0 |
| 7 | ConnectTo 0x3d6e50, AddEntityForPost 0x453af0 (0x19, on the component); reparent `Component::vfunc624` 0x3d7010 (0x2 or 0x4, on the old parent); Gate (0x20); NavigationController::ClearOwner (0x18) | as listed | none | -1 (one-shot, two frames) |
| 8 | UniverseMapBehavior (map UI) | 0x9 / 0x2 | object (0x49) / docking bay (0x28) | -1 |
| 9 | VentureTracker::MoveShipToVenturerCluster | 0x7f | object | variable |

**The walk** (`BuildCache` 0x3d4a80): gate: return unless the stack holds untruncated entries
(entry total 0x6cf1d38 != truncation depth 0x6cf1d30) or the own count +0x1e8 != 0. Push the own
list (stack 0x6cf1d50, max 99, "MAX_CACHE_SPECIFICATIONS"). For every untruncated entry on the
stack: if it has a class and `IsClass(this, class)` (vtable slot 573) is true, truncate it for the
subtree (stack 0x6cf1a10, max 99); OR its flags into `acc` if `time >= last + interval`. For each
child in view-cache child vector `idx`: `f = acc | child+0x1e0`; if f: `PropagateCacheFlags(child,
idx, f)`; recurse. Untruncate. Refresh every attachment (+0xa0) with 0x7f. Pop; for each own entry:
`last = time` when due; interval in [-0.5, 0) expires (counts down the chain, kind 7 clears
+0x1e0, empty list freed); interval < -0.5 counts up by 0.5. An active entry keeps its whole
subtree open even when it is not due (only the flags wait for the timer).

**The refresh** (`PropagateCacheFlags` 0x3d46b0, per component, flags & dirty of cache idx): 0x1
parent pointer (refcount transfer) and registered bit 0x10000; 0x2 child vector from +0xa8
(0x997be0); 0x4 clear only; pose block (allocated lazily; freed when 0x2f4b9bc and dirty 0x200000):
0x8 pose and scale (vfunc 648 `GetLocalTransform(this, block, tls_time, 1, 1)`, 649), clears
0x40000; 0x10 velocity (vfunc 650, gets the block if the pose was refreshed in the same call);
0x40 attention copy; 0x20 (or 0x400000) attachment poses (vfunc 622, offered the other cache when
+0x68 says so). Each dirty bit is cleared only if the clear mask has it.

**Invalidation:** `Positional::vfunc653` (SetLocalTransform) 0x746980 sets dirty 0x8 / 0x40010 in
both caches and the mask per vfunc 644; reparenting 0x3d7010 sets 0x2/0x4 on old and new parents,
moves the subtree count and adds a kind-7 one-shot on the old parent; ConnectTo adds a kind-7
0x19 one-shot on the connected component; attention changes add/remove kinds 2/3/4/5.
**Threading:** MoveWorker only (global stacks, unlocked components); the double buffer makes
readers on other threads safe.

**Pose math** (bit-exact, `x4mp_transform::posrot`): `Math::PosRot` = 4x4 f32 row-major, row 0 =
translation, rows 1-3 = basis. `GetLocalTransform` (non-reset path 0x74626e..0x746320) maps each
row of the caller's matrix through the local `L` = controller transform (+0x200 vfunc +0x30) after
the parent's child frame (vfunc 676): lane i = `((y*R2 + x*R1) + z*R3) + T` for the translation
row (lane 3 broadcasts w), `(y*R2 + x*R1) + z*R3` for basis rows. `GetTransformRelativeTo`
0x3db8a0 composes up to the common ancestor and expresses the result in the target's frame:
subtract the frame translation, then `(x*X + y*Y) + z*Z` with the transposed basis (lane 3 of X,
Y, Z = frame row 3 lane 3) (0x3dbba5..0x3dbc8f). Packed SSE, one rounding per op, no FMA. The
MoveWorker runs with the default MXCSR (only the main thread sets FTZ, in 0x14dd0e0), as Rust does.

## 3. Why it is expensive, and what can be done without changing results

The walk visits, every frame: galaxy, every cluster and sector, every direct child of every sector
(the galaxy entry, truncated at sectors); every direct child of every zone of an active sector; the
whole subtree of every active zone and highway; the path to every specification owner; and for two
frames the whole subtree of every one-shot owner, which opens its parent zone so that *all* the
zone's children are visited (gate check) even when nothing applies to them. Each visit touches
about three cache lines of a 1-4 KB component (counts, cache dirty word, specification pointer) plus
`IsClass` virtual calls for class-filtered entries. The walk is latency bound, not compute bound.
Most refreshes find nothing to do (dirty 0 returns immediately), except moving components, whose
pose and velocity stay dirty and are recomputed through their movement controllers every frame they
are covered (active sectors: 0.77 ms of `GetLocalTransform` with 8 sectors).

Options that keep every result bit-identical:

1. **Per-sector parallel walk with an exact merge** (`x4mp_transform::parallel`, done as a model):
   the inherited entries are read-only (truncation is path-local, so tasks get private copies taken
   where the sequential walk would enter the sector); every write stays inside the sector except
   commutative counters (refcounts; expiry counts on the sector's ancestors), which are deferred.
   Hazards detected and answered with a sequential fallback: a component reachable from two tasks
   or touched by the sequential part later (stale child vectors after a reparent), an expiry count
   outside the task root's chain, a stack overrun. Engine side it additionally needs worker TLS
   (game time +0x300, role +0x314, init flags), deferred releases (a refcount reaching zero runs a
   destructor), and per-getter thread safety: `GetLocalTransform` via controllers reads the pathing
   buffers (stable after the join); the attachment refresh vfunc 622 evaluates animations
   (0x1165ee0, also hot in the walk), unproven, so it may have to stay on the MoveWorker (plan the
   walk in parallel, run those refreshes in walk order).
2. **Prefetch and a compact mirror**: the same visit order with the next children's lines
   prefetched; or a structure-of-arrays mirror of the fields the walk reads. The mirror needs every
   writer of those fields hooked (reparent, specification add/remove, dirty writers), so it comes
   after the shadow stage.
3. **Skipping clean subtrees** is exact only with a correct dirty index: one-shot traversals also
   refresh any dirty component they pass (for example transforms of objects in inactive sectors are
   refreshed only by such incidental passes), so a skip must prove no covered component is dirty.
   Not data-only; later.
4. Not allowed: narrowing the specifications (changes what the caches contain, and the cache is read
   by AI, pathing and formation code).

## 4. Rust reimplementation (`rust/x4mp-transform`) and offline proof

- `posrot`: `PosRot`, `compose_onto`, `express_in_frame`, `transform_relative_to` (engine control
  flow: same node, absent/parent target, ancestor, branch, disjoint; the attachment variant with
  the fifth argument set is not modelled).
- `cache`: the walk and the refresh as an event-logging model (`frame`, `Traversal`), plus
  `add_spec` / `remove_spec` / `recount`; getters are callbacks.
- `parallel`: `frame_parallel(tree, root, idx, time, threads, split_class)`.
- `scenario` / `posecases`: shared binary formats and an engine-like universe generator.

Tests (offline, this PC): 16 unit tests; **thread invariance** (`tests/parallel.rs`): 120 random
universes x 5 frames at 1, 2, 4 and 8 threads give logs and final states identical to the
sequential walk (2,876 sector tasks, 85,168 events, 0 fallbacks); universes with stale child vectors
fall back in 71 frames and stay identical. **Oracle** (`native/engine/xform_oracle.c` +
`xform_oracle.py`, `tests/oracle.rs`): runs the original `BuildCache`, `PropagateCacheFlags`,
`GetLocalTransform` and `GetTransformRelativeTo` machine code from the pinned local copy
`x4-re/bin/X4.exe` (SHA-256, size, PE timestamp and image size verified; sections copied into a
private allocation; no imports, relocations, initialisers or entry point; a private TLS block via
TEB+0x58 entry 0; the scenarios avoid every path into the engine allocator) on synthetic components
whose vtables point at recording callbacks. Results: seed 1: 400 universes, 69,550 components,
274,462 observed engine calls (IsClass and getter calls, in order) and every final component state
(own flags, counts, dirty bits, cached attention and parent, refcounts, every specification entry
bit for bit) identical; 20,000 pose cases (9,942 compose with values from subnormals to 2^60 and
signed zeros, 10,058 relative on trees of up to 8 components) bit-identical. Seed 2: 300
universes, 52,503 components, 214,156 calls, 20,000 poses identical. Negative control: one
flipped bit in an oracle result is detected. Not covered offline: the child refresh (bit 0x2, it
allocates), pose-block release (0x200000), empty-list frees and NaN payloads.

Run: `python native/engine/xform_oracle.py --zig <zig.exe> --x4 <copy of X4.exe> --scenarios 400`.

## 5. Native capture, replay and shadow

**Result (NATIVE, lane b3, run xform-002, 6,011 frames, 350 s):** all 6 captures (8.6-158.8 s,
391,512 -> 48,086 components; capture 0: 140,062 visits, 34,828 open, 7,032 refreshes) replay
identically with the model, the traced ones (0, 2, 4) with identical refresh (component, flags) and
visit sequences. Refcounts differ by 1-2 on 3 components (other threads take and drop references
meanwhile); reported separately. Rule learnt natively: the attachment getter (vfunc 622) runs the
animation timeline code, whose fragment 0x116a295 ORs 0x80000 into BOTH caches' dirty words and
the clear mask of an animated component, so bits 0x80000/0x100000/0x400000 are getter-owned in both
caches wherever the getter ran (first shadow run: 66 of 1,500 frames flagged before this rule).

**Census (examples/census_capture.rs):** the walk is held open by one-shot kind-7 entries whose
owners are never reached: at 8.6 s 251,475 kind-7 entries, 72 owners reached, 251,047 owners
missing from their live parent's cached child vector (never visited, so their countdown never
runs); their +0x1e8 counts hold 29,740 of the 34,828 open gates. They decay with the world
(31,114 at 158.8 s), and the walk with them (140k -> 17k visits). No list holds a kind twice.

Instrument cost (lane only): capture 39-329 ms, shadow 91-1,467 ms per checked frame.

### Probe design

Module `native/engine/xform_probe.c` -> `x4lan_xform_64.dll`, started by a lane overlay
(`ui/xform_probe.lua`), never packaged. Verification before arming: X4.exe file SHA-256, PE
timestamp, image size; SHA-256 of `BuildCache` 0x3d4a80..0x3d517f, of `PropagateCacheFlags`'
first 32 bytes and of 0x9fe520..0x9fe560 around the call site; the call instruction at 0x9fe549
must be `E8` with a displacement to 0x3d4a80. Hook: retarget that call's rel32 to a jump stub
within +-2 GB of the image (14-byte `jmp [rip]`), written while the MoveWorker is in slot 6 (the
Lua callback, so `UniverseFrameUpdate` cannot be executing), byte-verified after writing, restored
on stop/collection/failure; any mismatch leaves everything unpatched and logs
`FEATURE xform DISABLED reason=...`. The stub calls the original `BuildCache` unchanged except on
capture frames (every N seconds, at most M), where it first writes a snapshot of every component
the walk can reach (the same gate logic in C, with child containers where the child bit is dirty,
attachments, `IsClass` for every filter class present, all walk inputs listed in section 2, the
global stack depths, the drop flag, idx and time), then calls the original, then writes the final
state of the same components. Optional trace mode additionally retargets the two refresh calls
inside `BuildCache` (0x3d4d0a, 0x3d4ea6) and the recursion (0x3d4d18) for the duration of the
capture frame only, recording each (component, flags, dirty before/after). Offline replay: the
capture becomes a scenario whose walk starts at `BuildCache(galaxy)`; the model must reproduce the
final state of every captured component (and the call sequence in trace mode). The capture also
gives the per-frame census section 3 needs (visits, open gates, refreshes with work, by
specification kind). Shadow (9547439): every Nth frame the same capture is taken around the
original and checked in process by the model (`x4mp_transform_ffi.dll`, `x4mp_xform_check`).

Hook sites (rewrite-coord/hooks.tsv, owner transforms): 0x9fe549 (call BuildCache in
UniverseFrameUpdate); trace frames only: 0x3d4d0a, 0x3d4ea6, 0x3d4d18.

## 6. Switch: the Rust build in place of `BuildCache`

`x4mp_transform_ffi.dll` `x4mp_xform_build` (ABI 2, `ffi/src/engine.rs`), called by the probe
instead of the original at 0x9fe549:
1. **Load** (read-only): a compact copy of what the walk can reach (the capture core's superset
   rules), sector subtrees on `threads` worker threads, merged by component address;
2. **Plan**: the bit-exact model runs `BuildCache(galaxy)` on the copy: refresh calls in walk
   order and every component's final specification list;
3. **Execute** on the MoveWorker in walk order through the engine's own functions:
   `PropagateCacheFlags` per refresh, and at a list's last end-of-subtree point the timer values
   are written and expired kinds removed with `RemoveCacheSpecification` 0x3d5440 (SHA-pinned;
   it does the count and kind-7 flag updates as the end loop does).
Refusals (nothing executed, the original runs): stack overrun, a list with a repeated kind
(entries are addressed by kind), a list change other than timers and removals, a component
outside the copy. Getter side effects on dirty bits (0x116a295) cannot change the plan: whether a
component is refreshed depends on the specification stack and its own +0x1e0, not on dirty bits.
The walk's shape depends on counts and cached child vectors, which no getter wrote in the traced
native captures (identical refresh and visit sequences); the A/B shadow keeps checking it.

**Offline (oracle, original machine code):** seeds 5, 6 and 7, 400 universes each: 2,453-2,475
frames per seed built by Rust at 1 and at 8 threads, every one shadow-checked by the model with 0
differences, 25-47 frames refused (synthetic repeated kinds; native captures have none), final
state identical to the original run in 400 of 400 universes per seed and thread count; A/B mode
(as in game: pairs of original and Rust frames, 4 threads; seed 7: 982 Rust frames) identical as
well. The oracle needed one fix for this: its TLS swap now replaces only entry 0 of the thread's
TLS array (the Rust DLL has its own implicit TLS slot).

**Native (xform-003, queued):** A/B: plain frames alternate in pairs between the original and the
Rust build (the cache index flips every frame, so both paths build both caches), both timed; every
5th frame a shadow check, alternating the same way, so the model is checked against the original
and the Rust build against the model in the same run; 6 captures of the original for replay.

Expected cost: the copy reads the whole reachable superset (the capture walker took 329 ms at
391k components, single-threaded), far more than the original walk touches (~3 cache lines on
140k visits, 3.7-6 ms). This switch proves the engine-side execution; a faster build needs the
walk itself on engine memory (exact visits, sector tasks in parallel, refreshes in walk order on
the MoveWorker), see section 3 option 1.

## 7. Open questions

- Answered by the census: the start-of-game walk cost comes from one-shot kind-7 entries of
  owners the walk cannot reach (section 5).
- Thread safety of the attachment refresh (vfunc 622, animation evaluation 0x1165ee0) and of the
  movement-controller getters on worker threads.
- `Component::GetLocalTransform` 0x3db640 (components without a controller) and the controller
  `GetTransform` implementations (NavigationController::GetTransformImpl 0xea5a90) are not yet
  modelled; they are the data side of the refresh.
- The fifth-argument variant of `GetTransformRelativeTo` (ancestor list built by 0x469420).
