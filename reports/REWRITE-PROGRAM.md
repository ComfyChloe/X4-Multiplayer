# Engine rewrite program: per-sector, multithreaded, behaviour-identical

Status: started 26 October 2026 on the project owner's request ("broad rewrite", "same behavior,
nigh identical output, but optimized", "each sector more or less its own mini version", "one agent
per system, test in parallel"). Base branch: `contrib/ferrite-rewrite-base`. Lead and integrator: the
overhaul session (Ferrite coordination). Local coordination folder (not in git):
`C:\Users\developer\Documents\X4Mods\rewrite-coord\` (README.md there is the live protocol).

## Terminology

User decision (26 Oct 2026, design doc commit 7698736): the dedicated X4 is the **server** (not a
player); players connect as **clients** (player 1 ... N, all equal). No "host"/"guest" in new code,
docs or logs; old identifiers, profiles and paths stay until that code is touched anyway.

## Goal

Progressively replace X4's single-threaded simulation, one system at a time, with our own code
(Rust where it is new logic, C only for build-pinned engine glue). Each sector becomes its own
small world run as a job on a worker thread; cross-sector effects go through ordered messages
applied at a frame barrier. X4 itself keeps running everything else (rendering, UI, input, content, and every system not
yet replaced). The replaced systems must keep the game's behaviour: same decisions, same state,
output identical where the data is discrete and bit-identical or within a documented bound where it
is floating point.

## Method: differential testing against the original engine

Every replacement goes through the same four steps. A step may not be skipped.

1. **Understand**: static RE (Linux build has symbols; match to Windows RVAs) plus the game's own
   script/XML content. Record behaviour, data layout and call sites in a report; never paste
   decompiled code into shared files.
2. **Capture and replay**: record the system's inputs and outputs from a native run (lane B) into a
   file; replay them offline against our implementation; diff.
3. **Shadow**: run the original and ours side by side in the game for every frame/call, compare,
   count mismatches, log the first differing fields. The original's result stays authoritative.
4. **Switch**: ours becomes authoritative behind a feature switch (on by default in dev, loud
   `FEATURE <name> DISABLED reason=...` on any refusal or mismatch), still fail-closed to the
   original on a build/signature mismatch.

Exactness policy:
- Integer and discrete state (IDs, counts, orders, flags, inventories, event order, RNG state): exact.
- Floating point: keep the original operation order and precision (no fast-math, no reassociation,
  no FMA unless the original uses it). Target bit-identical per entity. Where a deliberate
  vectorisation changes rounding, document the bound and prove the bound in tests.
- Parallelism must not change results: per-entity work is order-independent by construction; all
  cross-entity / cross-sector effects are buffered and applied at the barrier in the original
  sequential order; randomness uses per-entity streams (see contrib/ferrite-rng-streams). Every
  parallel system has a test that 1 thread and N threads give identical output.

## Measured priorities (Windows, 8-sector attention harness, 25 Oct 2026)

Evidence: `X4-Multiplayer-fixed-tick/.local/evidence-237-withheld`
(`walkprof-8sectors-x4lan-debug.txt`, `simprof-8sectors-x4lan-debug.txt`, `simprof-8sectors-report.txt`,
`simprof-8sectors.folded` = full folded stacks with RVAs; symbolize with `dev/simprof_report.py`).

| measure | baseline (0 proxies) | 8 high-attention sectors |
|---|---|---|
| sim fps | 60 | 41 |
| slot-loop sim ms (median) | 11.2 | 24.5 |
| component walk ms (share) | 1.9 (17%) | 8.8 (36%) |
| per-sector sum / slowest sector ms | 1.54 / 1.52 | 8.18 / 2.04 |
| ideal per-sector walk speedup | 1.0x | 4.0x |

Share of MoveWorker samples over the whole run (inclusive): Sector::Move walk 22.4%; UI controller
(slot 6) 23.3%; Component::BuildCache 16.1% (mostly camera/scene path, shrinks under load);
GameRender (slot 10) 10.1%; AIDirector 8.0% (ExecState_Run 8.4%, Expression_Evaluate 4.1%); event
processing 5.1% (DeliverEvent 4.7%); Weapon Move 4.6% (0 at baseline); Jolt step 3.3%; MD scheduler
1.6%; pathing join 0.9%; Ship::Move 0.5%. Unknown hot functions inside the walk: 0x1165660 (4.0%
incl), 0x1165ee0 (2.3%), 0x314fa0 (1.7%), 0x81c7a0 (1.2%), 0x1399c40 (7.5%, UI path), 0x138f2c0.

## Systems and owners

| system | agent | branch | own files | first milestone |
|---|---|---|---|---|
| native test lanes B1/B2 | lane | contrib/ferrite-rw-lane | dev/x4lane.py, dev/lane/, reports/NATIVE-LANES.md | unattended new-world runs on profiles 45850/45851 |
| per-sector ECS core, scheduler, barrier, shadow framework, mirror capture | ecs-core | contrib/ferrite-rw-ecs-core | rust/x4mp-sim/, native/engine/mirror*, reports/REWRITE-ECS-CORE.md | crate + thread-invariance tests + interfaces.md |
| transforms / BuildCache | transforms | contrib/ferrite-rw-transforms | rust/x4mp-transform/, native/engine/xform*, reports/REWRITE-TRANSFORMS.md | RE + capture/replay bit-identical |
| server frame diet (UI/render/camera work on a dedicated server) | frame-diet | contrib/ferrite-rw-frame-diet | native/engine/framediet*, reports/REWRITE-FRAME-DIET.md | equivalence-proven skip of client-only work |
| combat: weapons, turrets, projectiles, hits, damage, targeting | combat | contrib/ferrite-rw-combat | rust/x4mp-combat/, native/engine/combat*, dev/combat_scenario/, reports/REWRITE-COMBAT.md | combat load scenario + RE + capture |
| physics and flight jobs (Jolt, pathing batch jobs) | physics | contrib/ferrite-rw-physics | native/engine/physics*, reports/REWRITE-PHYSICS.md | thread use measured; determinism-preserving speedup or no-go |
| economy: production, storage, trade offers, prices | economy | contrib/ferrite-rw-economy | rust/x4mp-economy/, reports/REWRITE-ECONOMY.md | cost census + data model + capture |
| AI phase census and per-sector AI driver (engine side) | ai-driver | contrib/ferrite-rw-ai-driver | native/engine/aidriver*, reports/REWRITE-AI-DRIVER.md | stage 0 census, stage 1 single-threaded rounds |
| native AI order behaviours (Rust, per sector) | ai-orders | contrib/ferrite-rw-ai-orders | rust/x4mp-ai/, reports/REWRITE-AI-ORDERS.md | top orders by census, first behaviour in shadow |
| component walk parallelism + deferred events (stages 0/1 of x4-re/coord/reports/parallel-walk-design.md; reviewer: the "Second sandbox" session, which wrote the design) | walk | contrib/ferrite-rw-walk | native/engine/walkpar*, reports/REWRITE-WALK.md | stage 0 census, stage 1 single-threaded deferred replay with normal-vs-replay comparison |
| MD | stays serial on X4 (x4-re/coord/reports/sector-partition-scripts.md) | | | |
| faction AI (SupCom-style strategic AIs running NPC factions instead of jobs spawning; a deliberate gameplay change, switchable feature, not an equivalence rewrite) | design workflow, then faction-ai agent | contrib/ferrite-rw-faction-ai | reports/FACTION-AI-DESIGN.md, later rust/x4mp-faction/ | design with verified feasibility claims |

Background reports to read before touching a system: `x4-re/coord/reports/parallel-sim.md`,
`parallel-walk-design.md`, `sector-partition-scripts.md`, `simtick.md`, `entity.md`, `script.md`,
`rng.md`, `time.md`, `motion.md`, and `X4-Multiplayer-design/preparation/TARGET-ARCHITECTURE-PROPOSAL.md`.

## Rules

- Engine instrumentation follows `ENGINE-INSTRUMENTATION.txt`: runtime only, exact build verified
  (X4.exe build 23660954, SHA-256 pinned in native/engine/signatures.py), fail closed on any
  mismatch, never modify or redistribute X4/Egosoft files. Prefer data-only hooks (vtable entry
  compare-exchange from the verified original, VBI slots) over code patches; any code patch is
  build-pinned, byte-verified before and after, and restored on disarm. Keep x4lan_net hook-free.
- Register every hook site in `rewrite-coord/hooks.tsv` before using it. One owner per site.
- The lead's timing runs use lane A (profile 45852). Native tests only in your lane (profiles 45850/45851, game views under `X4Mods\lanes\`), through
  `dev/x4lane.py` once the lane agent publishes it. Never use profiles 45841-45844 or another
  session's game view. Never load normal saves. Respect `rewrite-coord/quiet.lock`.
- Distinguish native results from mocks/synthetic tests in every report; record the evidence path.
- Prefer new files over edits to shared ones. Shared files (`rust/Cargo.toml` members,
  `native/engine/build.py`, `native/engine/signatures.py`, `native/engine/fixedtick.c`,
  `extension/x4_lan_lab/ui.xml`) get minimal additive edits only.
- Commit to your own branch in your own worktree with clear messages ending in
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push, do not merge other
  branches; the lead integrates. Never commit logs, saves, credentials or build outputs (`.local/`).
- Original code only; Neresco projects are references, never copied.
