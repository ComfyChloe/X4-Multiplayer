# Engine rewrite: physics and flight jobs (Jolt, pathing batch jobs)

Owner: physics agent, branch `contrib/ferrite-rw-physics` (base `contrib/ferrite-rewrite-base` e63d9cf).
Build: X4 9.00, Steam build 23660954 (X4.exe SHA-256 19750a65...6891). Addresses are Windows RVAs.
Static analysis on a private Ghidra copy (`x4-re/work/rw-physics/gh`, read-only) plus capstone; labels in
`x4-re/labels/rw-physics.tsv`. No decompiled code is reproduced here; behaviour is described.
Every result below says whether it is static, synthetic (offline) or native (X4 in a test lane).

## Summary

| question | answer | evidence |
|---|---|---|
| Jolt version, flags | **Jolt 5.5.0**, single precision, not cross-platform deterministic; Linux feature bits = FP exceptions only; both builds use plain SSE2 Jolt code (no SSE4.1/AVX/FMA) | static: Linux `VerifyJoltVersionIDInternal` compares with 0x400050500; instruction scan of all Jolt functions |
| settings | Jolt defaults, except `mAllowSleeping=false`, `mTimeBeforeSleep=1e4`, gravity 0; **`mDeterministicSimulation=true`**; collision steps 1 (`*(0x6cf1510)`) | static: `Scene::Init` 0xc6890, `PhysicsGlobals` ctor 0xb7d80 |
| job system | `XPhys::XJobSystem` (vtable 0x2a2aba0, derives from `JPH::JobSystem` directly). `GetMaxConcurrency` = JobThreadManager worker count (logical processors - 2, clamped 4..32: **30 on this PC**). `QueueJob` 0xc5a80 forwards a job to the JobThreadManager **only if it still has dependencies**, i.e. never for a ready job. Every Jolt job therefore runs on the MoveWorker inside `WaitForJobs` (0xc5dc0): first ready job in barrier insertion order, under the barrier's critical section, `Sleep(0)` after each job. Same logic in the Linux build (QueueJob 0xeede20). | static; profile: 95.6% of the MoveWorker's `WaitForJobs` samples execute Jolt jobs, 4.4% are `Sleep(0)` |
| scenes | `Main` (0x38f0748) and `Cutscene` (0x38f0740) are stepped back to back by `UniverseFrameUpdate` (0x9fe459, 0x9fe48b) with the MoveWorker's game time; `Navigation` (0x38f0738) is query-only (pathing obstacle queries) and never stepped | static: `CreateGlobalScenes` 0x83f7d0 |
| how much of the step is parallel | **none**: the step decomposes into ~68-450 jobs per frame (synthetic, at 14-30 reported workers) that all run serially on the MoveWorker | static + profile |
| cost (8-sector harness, native profile 25 Oct) | `Scene::Step` 0.70 ms/frame at baseline, 0.81 ms/frame at 8 sectors (2.9-4.1% of MoveWorker samples); of that the Jolt update proper is ~1/3 (0.23-0.29 ms), the rest is X4's serial per-body synchronisation around it | native profile (existing evidence, re-attributed) |
| pathing batch | one JobThreadManager job per batch (two batch vectors), agents assigned round robin; per batch a **wall-clock budget** (+0x218) after which the remaining agents get the cheap update and the cursor rotates. Under load the MoveWorker **does not wait** at the join (0.6% of the join's samples wait); the join cost is serial `ApplyQueuedOrders`, 2/3 of it one O(agents) scan (0xe955b0) | static + native profile |
| more threads, same result? | **Jolt state: yes** (synthetic: bit-identical `SaveState` every frame, 4 scenarios, X4-style serial executor at 4/6/14/22/30/32 reported workers, Jolt's thread pool at 1-31 threads, the parallel pool at 1-8 threads). **X4's listener state: yes as well** - the global callback order changes with the decomposition and with threads, but the calls of one body pair (and so of one contact-map key) keep their order in every execution. The listener still has to run serialised on the MoveWorker (no lock, TLS game time, allocations). | offline harness `native/engine/physics_determinism.cpp` (MSVC build), section 3 |
| improvement | **joltwait** (native, exact): X4's job order without the per-job `Sleep(0)`; -0.128 ms/frame paired in a loaded battle run, worst frame 12.2 -> 5.0 ms; under CPU contention the Sleep(0) is 8.7 % of MoveWorker samples. Parallel pool: bit-identical offline, native integration deferred (Jolt compute ~0.7 ms/frame in the 87-ship battle) | sections 5-6 |

## 1. How X4 runs Jolt

### 1.1 Version and build configuration (static)
- The Linux build exports `JPH::VerifyJoltVersionIDInternal(unsigned long)` (linux 0x1b0aab0); it compares
  with **0x400050500** = features 0x4 << 24 | 5.5.0. In Jolt 5.5.0's `Core.h` feature bit 3 (value 4) is
  `JPH_FLOATING_POINT_EXCEPTIONS_ENABLED`: single precision, **no `JPH_CROSS_PLATFORM_DETERMINISTIC`**, no
  profiler, no debug renderer, 16-bit object layers, no asserts. Upstream tag v5.5.0 = commit 23dadd0e (28 Dec 2025).
- Windows: the version check is folded away by LTCG. The Jolt job object is 0x58 bytes (no profiler name/colour
  fields), as on Linux. An instruction scan over all Jolt functions of both binaries finds **no VEX, no FMA,
  no `dpps`** (Jolt uses `dpps` whenever `JPH_USE_SSE4_1` is set): Jolt is compiled for baseline SSE2 on both
  platforms. On x86 SSE2 the cross-platform flag would only remove FMA, so it changes nothing here; Windows (MSVC)
  and Linux (GCC) are still different binaries and need not agree bit for bit.

### 1.2 Scenes and the step (static)
- `CreateGlobalScenes` 0x83f7d0 creates three `XPhys::Scene`s via `Scene::Create` 0xba090 (65536 bodies,
  body pairs and contact constraints each): `Main` 0x38f0748, `Cutscene` 0x38f0740, `Navigation` 0x38f0738.
  Main and Cutscene are stepped in `UniverseFrameUpdate` after the pathing join, MD, AI and the timer lists.
  Navigation is used under a lock by pathing code (0xebfc60) and maintained by `Zone::Move`; it is never stepped.
- The scene's system object (0x2ab0 bytes): `JPH::PhysicsSystem` at +0 (ctor 0xb41b0; `PhysicsSettings` +0x260,
  gravity +0x500), `XTempAllocator` +0x528, `XContactListener` +0x560 (vtable 0x2a2ab48), `XJobSystem` +0x648,
  last step time +0x678, body-id vector +0x680 (count) / +0x690 (data).
- `XPhys::Scene::Step` 0xba600 (scene, game time):
  1. deferred releases (0xb2b90) under a try-lock;
  2. **pre-sync, all bodies** (`b4540` collects every live body id): component velocity at the TLS game time
     (0x3dc040) -> `BodyInterface::SetLinearAndAngularVelocity`; compound part shapes refreshed (0xbff80 ->
     `NotifyShapeChanged`);
  3. `PhysicsSystem::Update(dt = time - last, collisionSteps = *(*0x6cf1510) (= 1), XTempAllocator, XJobSystem)`;
  4. post: per-record touch map (+0x570) events (0x87aeb0); **collision events**: the listener's contact map
     (+0x5b0) is walked in key order and a `U::CollisionEvent` (vtable 0x2ba5e98) is posted *immediately* to both
     components (speeds computed with `rsqrtss`/`rcpss` approximations); listener reset (0xb52e0); force
     effects (scene +0x30); **velocity feedback, all bodies** (0xbfb00: when the step changed a body's velocity
     by more than 1e-4 - collision response - the change is handed to the component's movement controller, and
     force-effect sums are applied); ship-ship push records (+0x5f0, expiring after 5 or 30 s) applied through
     0x835b80 and `SetLocalTransform`.
- Settings (`Scene::Init` 0xc6890): Jolt 5.5.0 defaults (16384 pairs in flight, Baumgarte 0.2, 10 velocity /
  2 position iterations, ...) except **`mTimeBeforeSleep = 10000`, `mAllowSleeping = false`**; gravity zero.
  `mDeterministicSimulation` keeps its default **true** (Jolt sorts contacts/constraints so results do not depend
  on the order work was done in).
- Collision steps: `PhysicsGlobals` (0x100 bytes at `*(0x6cf1510)`, ctor 0xb7d80) starts with 1; only creation
  and shutdown write it (fixed60catchup sets it per frame).

### 1.3 The job system (static, both builds)
`XPhys::XJobSystem` implements `JPH::JobSystem` itself (no `JobSystemWithBarrier`):

| slot | function | behaviour |
|---|---|---|
| 1 GetMaxConcurrency | 0xc5940 | JobThreadManager+0x528 = worker count |
| 2 CreateJob | 0xc5960 | 0x58-byte Jolt job from SMem; with 0 dependencies it calls QueueJob at once |
| 3/4 Create/DestroyBarrier | 0xc5a30/0xc5a60 | pooled `XBarrier`s (vtable 0x2a2ab78: deque of jobs + CRITICAL_SECTION) |
| 5 WaitForJobs | 0xc5a70 -> 0xc5dc0 | loop: lock; drop finished jobs at the front; run the first job whose dependency count is 0 (inlined `Job::Execute`); unlock; `Sleep(0)` |
| 6 QueueJob | 0xc5a80 | `if (job == null || job->numDependencies == 0) return;` else JobThreadManager::QueueJob(thunk 0xce1d0 = Execute, category 1) |

A job reaches QueueJob only when it becomes ready (dependency count 0: at creation or via `RemoveDependency`),
so **no Jolt job ever reaches a worker**; the condition looks inverted, but it is the shipped behaviour on
Windows and Linux (linux QueueJob 0xeede20 tests the same field, +0x34 there). The worker count still matters:
Jolt sizes its parallel phases (find-collisions jobs, solver jobs, pair queues of 16384/N) by
`GetMaxConcurrency()`, so the same frame is decomposed differently on a 16-thread and a 32-thread PC.

JobThreadManager (static): worker count = sum of logical processors over `RelationProcessorCore` entries of
`GetLogicalProcessorInformation`, minus 2, clamped to 4..32 (0x14bcf80; the earlier "physical cores - 2" note
in time.md undercounts SMT). Workers (`JobThread::Run` 0x14bbff0) wait on one semaphore, pop a slot index from a
deque under a CS, publish the running job's category at runnable+0x18 (runnables at manager+0x28+i*0x20) and
clear it afterwards. That field is what the probe's sampler reads.

### 1.4 X4 callbacks that run inside the step (static)
- `XContactListener` (embedded at system+0x560): validate returns "accept all" (0xb3d60); added/persisted ->
  0xb75a0. It updates per-record touch counters, the contact map at +0x50 (key = both components and parts;
  each manifold adds 1 to a count and its average point to vector sums), may set `ContactSettings::mIsSensor`
  (pair check 0xb74f0) - **the only influence on the Jolt result, and it depends only on the pair** - and for
  ship-ship contacts creates push records with `GetTransformRelativeTo` at the calling thread's TLS game time.
  It takes **no lock**.
- `XCollisionGroupFilter::CanCollide` 0xb71d0 -> 0xb9020: a reader-counted lookup (designed for concurrent
  readers). Uncontended, each call enters a CS and ends with `SetEvent`: ~1.6 ms/s of syscalls at 8 sectors.
- X4 wrapper shapes collide through 0xc6210 (shape filter, then the inner pair's dispatch entry): no X4 state.

### 1.5 Where the time goes (native profile 25 Oct, re-attributed)
Source: `X4-Multiplayer-fixed-tick/.local/evidence-238-withheld`
(1 kHz MoveWorker sampler). Windows grouped by attention-load step (`x4-re/scripts/rw-physics/physprof.py`).

| | baseline (5 windows, 59 fps) | 8 sectors (17 windows, 35.6 fps) |
|---|---|---|
| `Scene::Step` inclusive | 41.0 ms/s = 0.70 ms/frame | 29.0 ms/s = 0.81 ms/frame |
| of it `WaitForJobs` (Jolt jobs) | 13.3 ms/s | 9.3 ms/s = 0.26 ms/frame (FindCollisions 63%) |
| of it waiting | 1.8% | 1.4% (Sleep(0) only) |
| pathing join `WaitAndSwapBuffers` | 4.8 ms/s, 43% waiting | 10.3 ms/s = 0.29 ms/frame, **0.6% waiting** |
| of the join: removed-reference scan 0xe955b0 | | 6.9 ms/s (66.6%) |

The rest of `Scene::Step` at 8 sectors is X4 code: shape refresh 0xbff80 (5.1 ms/s), velocity read 0x3dc040
(~3.4), deferred releases 0xb2b90 (2.4), velocity feedback 0xbfb00 (~3.5), collision event posting (~0.7) and
`SetLinearAndAngularVelocity` (~1.6). Jolt + X4 physics + pathing join together are ~1.1 ms of a ~28 ms frame.

## 2. The pathing batch (static + native profile)
- Pathing thread (`PathingManager::Run` 0xeabf00): woken at MoveWorker frame start, copies the game time into its
  TLS, sets TLS+0x314 = 1 (it reads the same view cache as the MoveWorker), records `dt` (+0x1f8), time (+0x200)
  and the pass start QPC (+0x210), runs `RunPass` 0xeae3c0 and releases the done handle (+0x10).
- `RunPass`: `AssignAgentToBatch` 0xeaf2b0 distributes agents round robin over two batch vectors (flight model
  type 3 -> +0x198, others -> +0x180; 0x20-byte batches); then one `JobThreadManager::QueueJob` per batch
  (thunk 0xf29cd0, job 0xeaeb60, category 1), outstanding counter +0x1b0, completion event +0x1c8.
- `UpdateBatch` 0xeaecc0 (worker): registers as a reader of the published view cache, updates its agents in
  cursor order with 0xe8f250 (the flight/steering update), checks the **wall-clock budget** (+0x218 seconds since
  +0x210; for flagged batches only after 100 agents) and gives every remaining agent the cheap update once over
  budget; then rotates the cursor so those agents go first next frame. Batch (0x20 bytes): +8 agent count,
  +0x10 list head, +0x18 cursor node (MSVC list nodes: next +0, prev +8, agent +0x10).
- **Windows budget quirk** (static + this PC): the check multiplies the QPC tick delta by the constant 1e-9
  (0x31c05a8), but QPC runs at 10 MHz, so with pm+0x218 = 0.001 the **effective Windows budget is 100 ms per
  batch** and the cheap update practically never happens (passes take 1-2 ms). The Linux build (UpdateBatch
  0x13e32f0) computes nanoseconds from `clock_gettime`: a real 1 ms budget. A Linux server therefore degrades
  pathing under load, Windows does not. Each batch splices its private command list
  into the global deferred queue under 0x398a558 **in completion order**.
- Join (`WaitAndSwapBuffers` 0xeac170, MoveWorker, after the walk, MD and AI): waits for the done handle, applies
  queued orders (0xeace40) and flips the double buffer. In `ApplyQueuedOrders` a non-empty removed-component set
  (+0xf0, count +0x118) triggers a scan of **every agent** (0xe955b0: three references resolved to their ship
  ancestor through the component registry and the view cache, cleared if removed).
- Determinism: per-agent updates read the double-buffered state, so they do not depend on each other within a
  pass, but the budget decision (wall clock) and the splice order (completion order) already make the pass
  timing-dependent today.

## 3. Thread invariance (synthetic, offline)
`native/engine/physics_determinism.cpp`, built by `native/engine/physics_build.py` with Jolt 5.5.0 (pinned
commit, fetched into `.local/evidence-259-withheld` when absent) for baseline x86-64 with X4's settings. Scenes: 400
mixed ships (boxes, spheres, capsules, hulls, mutable compounds of hulls, offset-COM hulls) plus static stations
(static compounds) and asteroids (meshes), velocities partly overridden every frame like X4's pre-sync; a
4-collision-step catch-up variant; a packed 300-body cluster (large islands); 200 bodies with LinearCast CCD.
Every frame the complete `PhysicsSystem::SaveState(All)` (bodies, velocities, contact cache, global state) is
hashed and compared with the reference (X4-style serial executor reporting 30 workers = this PC).

Result `.local/evidence-220-withheld` (26 Oct, clang build): **VERDICT IDENTICAL**. 1700 frames x 4 scenarios
x 12 configurations (X4-style serial at 4/6/14/22/30/32, `JobSystemSingleThreaded`, thread pool with 1/2/4/8/15/31
threads, two runs each): state bit-identical every frame; the *set* of contact callbacks identical every frame;
the *order* of callbacks identical only for the same decomposition run serially. In the packed scenario
(1218 callbacks/frame) the order differs between every reported worker count (pair queues of 16384/N fill up
and pairs are processed inline), and with real threads it differs on every frame.

Synthetic step times (this PC, clang build, not X4's MSVC code; per Update): swarm 300-370 us serial, 132-180 us
at 2-4 threads, 250 us at 31 threads; packed 3.5-3.9 ms serial, 1.6-1.8 ms at 4-8 threads. Jolt at this scale
gains ~2x from 2-4 threads and loses most of it beyond 8.

MSVC build (`physics_build.py --msvc`, VS 2022 cl 19.44, the compiler family X4 uses; adds the C serial barrier
of physics_jobs.h, the parallel pool of physics_pool.h and an X4-style listener that keeps, per body pair, a
count and the sum of the averaged manifold points in callback order like 0xb75a0). Result
`.local/evidence-221-withheld` (26 Oct): **VERDICT IDENTICAL** over 144 configuration
runs: state, callback set and the listener aggregate identical every frame for every decomposition and thread
count, both with the listener locked and called directly from the workers and with the buffered replay.

Why the per-key order survives: every key of X4's contact map (both components and parts) belongs to one body
pair, and all calls of one body pair are ordered by happens-before in every execution - one `ProcessBodyPair`
per pair and collision step, then the CCD job for that pair (CCD-vs-CCD pairs are cast only from the lower body
id), then the single removed-callbacks job, steps in sequence. Only the order *across* pairs varies, and X4
walks its maps in key order. (The stage 1 version of this report wrongly concluded that X4's listener state
depends on the decomposition.)

Consequences for X4:
- Neither the Jolt state nor X4's listener state depends on the worker count or the execution order.
- Running Jolt jobs on workers still requires serialising the listener (no locks; tree-node allocations) and
  calling it on the MoveWorker (it reads the TLS game time and view-cache selection through
  `GetTransformRelativeTo`). The buffer in physics_pool.h does that: calls are recorded on any thread with their
  manifold and replayed after the step, grouped by body pair, in reservation order within a pair (which is the
  serial per-pair order). The only listener output Jolt itself consumes, `mIsSensor`, depends on the pair only
  (0xb74f0) and would be decided on the worker and verified at replay.

## 4. Native probe (physics_probe.h, data-only)
Included by `fixedtick.c` (like walkprof.h) in every mode with the slot 0/15 brackets.
- Wraps five .rdata vtable entries after verifying each holds the expected function (and 22 pinned code sites,
  `signatures.py` `PHYSICS_SITES`): XJobSystem CreateJob/WaitForJobs (jobs, time in Jolt per scene),
  XContactListener added/persisted/removed (callbacks, distinct body pairs, pairs with >= 2 manifolds).
  Counting only on the probe's MoveWorker; restored by compare-exchange on disarm.
- Frame end (slot 15): bodies synchronised per scene (+0x680), pathing batch counts, budget and dt.
- Sampler thread (2 kHz, `ReadProcessMemory` only): busy JobThreadManager workers by category, queue depth,
  busy workers while the MoveWorker is inside a Jolt step, pathing pass (batch phase) duration, and the slack
  between the end of the batch phase and the MoveWorker's join (+0x88).
- Log: `[X4LAN] PHYSPROF ...` once per second (dev/fixed_tick_probe.lua); refusal `FEATURE physprof DISABLED`.
- Mode `owner:physics_mode('joltwait'|'original')`; dev/fixed_tick_probe.lua requests joltwait by default
  (lead approval 26 Oct; `X4LAN_PHYSICS_MODE='original'` keeps X4's barrier, `'ab'` alternates): the call only
  queues the request; the owner thread switches at its next frame end, when no Jolt update is in flight
  (X4 loads the UI script twice at game start and the first instance calls from another thread). A refusal or
  anomaly is latched (`joltwait_reason`, `FEATURE joltwait DISABLED`); `modeFrames` per window tells pure
  windows from mixed ones.
  A disarm on another thread (Lua teardown) may race a Jolt update holding a serial barrier, so it leaves
  joltwait installed (entries 3/4 and the WaitForJobs router, pinned module) and the next install adopts it;
  owner-thread disarms happen between frames and restore everything.
- Tests: `native/engine/test_physics_probe.c` (synthetic image: verified swap or nothing, owner-thread counting,
  scene attribution, window folding, sampler occupancy/pass/join, loading skip, restore, foreign entries left
  alone), `tests/test_physics_probe_lua.py` (LuaJIT logging).

## 5. Native results (stage 2, lanes; timing not trustworthy)
`rewrite-coord/results/2026-10-26-physics-stage2-native.md`. Idle world (physprof-001, 5,844 frames) and the
combat battle scenario (battle-ab-002, 87 ships, 1,614 frames in the window): 94 / 114 jobs per frame, 685 /
900 bodies in Main (Cutscene empty), ~8.4 / ~9.7 contact callbacks, 6.6 / 8.2 body pairs, 1.44 / 0.75 pairs
with >= 2 manifolds per step; workers stay idle while the MoveWorker is in Jolt (0.00-0.016 busy); the pathing
pass ends 8-27 ms before the MoveWorker joins.

## 6. joltwait and the parallel pool (stage 3)
`rewrite-coord/results/2026-10-26-physics-stage3-joltwait.md`.
- joltwait swaps XJobSystem slots 3/4 for the serial barrier of physics_jobs.h: same selection rule (first
  ready job in insertion order), no critical section, no `Sleep(0)`. Native A/B in the battle: 0.852 -> 0.726
  ms/frame (paired +0.128 ms, 8 of 8 segment pairs), worst frame 12.2 -> 5.0 ms, 81,989 jobs, 0 anomalies.
  In the contended combat profile `XBarrier::Wait` is 10.1 % of MoveWorker samples, 86 % of it `Sleep(0)`.
- Parallel pool: offline bit-identical (section 3); native integration deferred - Jolt's own compute is
  ~0.7 ms/frame (1.4 % of samples) in the battle, so 2x saves <= 0.35 ms/frame, and X4's shape, filter and
  temp-allocator code would run concurrently for the first time.
- Scene::Step besides Jolt (battle profile): `Body_UpdateShape` 0xbff80 0.68 %, velocity feedback 0xbfb00
  0.67 %, pre-sync 0x3dc040 0.37 %, `SetLinearAndAngularVelocity` 0.22 % of MoveWorker samples.

## 7. The flight/steering update 0xe8f250 (stage 4)
Static; key facts re-verified by hand, details in the stage-4 results file.
- Signature `void(Agent*, CmdList*, float dt, double t, bool cheap)`; only call site 0xeaed6f in UpdateBatch
  (`e8 dc 04 fe ff`). Double-buffered pathing slots: slot(i) = agent+0xad0+i*0x1a0, read index 0x38f4ee4,
  write index 0x2f4b6f4.
- Mostly plumbing (orders, formation flags, reference checks, zone/gate WarpEvents, boost/travel checks; events
  appended to the batch-private command list). The flight math is the agent's flight control model,
  `model->Update(slot(W), dt, t, cheap, P(owner))` through vtable slot +0x70 (call `ff d3` at 0xe901d5), followed
  by a pure post-model filter (rate limiter, forward-speed ratio, angular clamp).
- Six model classes (vtable / Update): Forced 0x2c4f7d0 / 0xeb04c0 (0x1ce bytes, pure), Highway 0x2c4f6f0 /
  0xeb0b70, Linear 0x2c4f610 / 0xeb27c0, MassTraffic 0x2c4f8d0 / 0xeb3d90 (uses `rsqrtss`), Spline 0x2c4f480 /
  0xeb7230, Steering 0x2c4e9c8 / 0xec7200 (large; draws from the worker's TLS RNG: `Random::FloatState(TLS+0x318)`
  at 0xec80b0, so its result depends on which worker ran the batch).
- Thread context: the job path never sets the TLS game time (+0x300) or the view-cache selector (+0x314) on the
  workers; e8f250 stamps some events with the TLS time.
- Data-only interception point: the six vtable slots (+0x70). physics_flight.h wraps them for the census
  (`FLIGHTPROF`) and the capture (`FLIGHTCAP`).

Rust candidate (offline, original machine code via the oracle; rust/x4mp-physics):
- `flight::forced_update` = ForcedFlightControlModel::Update: velocity from the pose step, the relative
  orientation (target x current^T in the original lane order), Euler terms from atan2f/asinf with the clamp,
  pose copy. Bit-identical to the original on 483,322 cases (realistic, near-identity, arbitrary finite bits).
- `flight::x4_asinf` = X4's statically linked asinf (0x1b43ce0), ported: identical on **every** f32 in [-1, 1]
  (2,130,706,434 inputs). The platform asinf differs on ~0.9 % of inputs, a double asin rounded to float on
  ~6.5 %; the platform atan2f equals X4's on Windows (2 M inputs). With the port, the forced update runs
  without any X4 code and stays bit-identical (300,000 cases).
- Capture/replay: physics_flight.h records the model bytes, the agent block 0x340..0x39f and the slot before and
  after each Update of one class into `x4mp-flightcap-<model>.log` (X4PFCAP1); `x4mp-flight-replay` replays it
  against the reimplementation (`REPLAYCAP` line). Offline round trip: 2,000 records from the original replay
  with 0 mismatches, a flipped bit is caught.

## 8. Status and next stages
- Stage 1 (static + synthetic): done (6a0a9a8). Stage 2 (native probe runs): done. Stage 3: joltwait done
  (quiet-window timing requested, Q17); parallel pool offline only.
- Stage 4/5: flight/steering update 0xe8f250 assessment and a capture/replay of it.
