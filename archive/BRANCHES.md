# X4 Multiplayer - branch archive

Archive of <https://github.com/Lith7ium/X4-Multiplayer> - the working mod repository.
Taken 2026-10-27 15:18 with `main` at `bbaa7de2`.

The upstream repository states that only the owner and invited collaborators can push
to it. These branches are the project's entire development history, and nothing on
GitHub guarantees a branch stays where it is - a force-push or a delete on the original
would erase it. This archive keeps a copy.

## Restoring

```sh
git clone x4-multiplayer-all-refs.bundle restored
cd restored
git branch -a                 # every branch is present
git checkout contrib/ferrite-rust-relay
```

A copy of every branch is also pushed under `archive/<name>` in
<https://github.com/ComfyChloe/X4-Multiplayer>, so the branches survive even if the
originals are deleted upstream.

## The branches worth knowing about

- **`contrib/ferrite-linux-server`** - The server work, for Linux.
- **`contrib/ferrite-rewrite-base`** - The base the rewrite branches build on.
- **`main`** - The integration branch. What the mod actually ships.
- **`src-layout-main`** - Sources moved to src/ and tools to tools/.
- **`contrib/astra-7-5-delivery`** - The 7.5.0 delivery the source snapshot was cut from.
- **`contrib/ferrite-rust-core`** - A Rust core that binds X4's own FFI at runtime, pinned to a specific game build. The game-side half, in Rust.
- **`contrib/ferrite-rust-relay`** - An earlier Rust port of the relay and the native module, with a differential parity harness.
- **`contrib/kimi-dedicated-server`** - An earlier dedicated-server experiment.

## Every branch

`behind` and `ahead` are commit counts against `main`: how much of `main` the branch is
missing, and how many commits it has of its own. A branch that is 0 ahead of main has
nothing of its own.

| Branch | Last commit | Author | behind | ahead | Subject |
|---|---|---|---:|---:|---|
| `contrib/astra-client3-seed-stage` | 2026-10-26 | X4 Contributor 1 | 6 | 51 | Record bounded native Linux p4 join and cleanup evidence |
| `contrib/astra-ferrite-coordination` | 2026-10-26 | X4 Contributor 1 | 6 | 10 | docs: checkpoint Linux thread-role mapping handoff |
| `contrib/astra-thread-role-map` | 2026-10-26 | X4 Contributor 1 | 6 | 1 | docs: map Linux simulation thread roles and job pool indices |
| `contrib/codex-review-index-20261026` | 2026-10-26 | silver2127 | 6 | 62 | Publish final native evidence boundaries and measured transform slowdown |
| `contrib/ferrite-architecture-design` | 2026-10-26 | silver2127 | 6 | 10 | Design: call the processes server and clients, not host and guest |
| `contrib/ferrite-linux-client` | 2026-10-26 | silver2127 | 6 | 45 | Launcher: role prompt stays usable without a readable package.json |
| `contrib/ferrite-linux-server` | 2026-10-26 | silver2127 | 16 | 21 | Stop the shield bar flashing from health authority churn |
| `contrib/ferrite-np8` | 2026-10-26 | silver2127 | 6 | 52 | Merge PR #62: stage saves for reserved dedicated client profiles |
| `contrib/ferrite-nplayer` | 2026-10-26 | silver2127 | 6 | 43 | Motion: drain the relay queue, adaptive delay, no fence on refresh gaps |
| `contrib/ferrite-overhaul` | 2026-10-26 | silver2127 | 6 | 8 | Overhaul: chain the sector walk wrapper on the attention hooks, cover ship sizes |
| `contrib/ferrite-player-factions` | 2026-10-26 | silver2127 | 6 | 24 | docs(player-factions): record native ordering compatibility |
| `contrib/ferrite-rewrite-base` | 2026-10-26 | silver2127 | 6 | 58 | Merge branch 'contrib/ferrite-rw-ecs-core' into contrib/ferrite-rewrite-base |
| `contrib/ferrite-rw-ai-driver` | 2026-10-26 | silver2127 | 6 | 51 | AI driver: validate hash coverage diagnostics in a native lane |
| `contrib/ferrite-rw-ai-orders` | 2026-10-26 | silver2127 | 6 | 77 | Record wider native movement capture evidence |
| `contrib/ferrite-rw-combat` | 2026-10-26 | silver2127 | 6 | 79 | Record corrected native combat census and measurement limits |
| `contrib/ferrite-rw-economy` | 2026-10-26 | silver2127 | 6 | 84 | Verify native reference prices and model automatic offer price separation |
| `contrib/ferrite-rw-ecs-core` | 2026-10-26 | silver2127 | 6 | 61 | Record native round-robin capture and exact subset-reader validation |
| `contrib/ferrite-rw-frame-diet` | 2026-10-26 | silver2127 | 6 | 57 | Record bounded quiet-window frame diet timing |
| `contrib/ferrite-rw-lane` | 2026-10-26 | silver2127 | 6 | 53 | Record native lane stall detection and cleanup acceptance |
| `contrib/ferrite-rw-physics` | 2026-10-26 | silver2127 | 6 | 47 | Record native forced-flight target-transition replay coverage |
| `contrib/ferrite-rw-transforms` | 2026-10-26 | silver2127 | 6 | 36 | Measure isolated transform baseline and reject settled-world speedup |
| `contrib/ferrite-rw-walk` | 2026-10-26 | silver2127 | 6 | 41 | Record bounded native replay smoke and retained failure limits |
| `contrib/ferrite-save-trim` | 2026-10-26 | silver2127 | 16 | 18 | Record correlated native station module persistence evidence |
| `contrib/ferrite-sim-decouple` | 2026-10-26 | silver2127 | 16 | 22 | Add opt-in render-prep skip with RNG isolation; document why the UI scene skip is unsafe |
| `contrib/ferrite-walk-avatar` | 2026-10-26 | silver2127 | 6 | 45 | Docs: on-foot avatars, interactions, multi-crew and EVA design; walk test checklist |
| `dev` | 2026-10-26 | silver2127 | 3 | 109 | NPC replicas: near replicas uncapped, a per-frame time budget for the rest |
| `main` | 2026-10-26 | silver2127 | 0 | 0 | Merge pull request #2 from Lith7ium/src-layout-main |
| `src-layout-main` | 2026-10-26 | silver2127 | 1 | 0 | Move sources to src/ and tools to tools/ |
| `tidy-root` | 2026-10-26 | silver2127 | 3 | 0 | Move root documentation into docs/ |
| `contrib/astra-7-4-integration` | 2026-10-25 | X4 Contributor 1 | 24 | 0 | Prepare pinned contributor onboarding and refresh project entry points |
| `contrib/astra-7-5-delivery` | 2026-10-25 | X4 Contributor 1 | 12 | 1 | docs: record forthcoming dedicated server task under Ferrite |
| `contrib/astra-faction-missions` | 2026-10-25 | X4 Contributor 1 | 20 | 0 | Record faction mission draft and native validation checkpoint |
| `contrib/astra-linux-hook-map` | 2026-10-25 | X4 Contributor 1 | 6 | 2 | Identify Anark onUpdate anchors and retain frame candidate limits |
| `contrib/astra-log-preservation` | 2026-10-25 | X4 Contributor 1 | 6 | 3 | Clarify log preservation relative to interrupted capture recovery |
| `contrib/astra-npc-combat` | 2026-10-25 | X4 Contributor 1 | 22 | 0 | Record NPC combat draft and validated source checkpoint |
| `contrib/cobalt-h4-identity` | 2026-10-25 | JennyTheBelgian | 6 | 1 | docs: H4 identity/authority inventory and interface proposal |
| `contrib/cobalt-playtest-recorder` | 2026-10-25 | JennyTheBelgian | 6 | 4 | recorder: never split oversized lines; explicit write-error omissions (Astra review 2) |
| `contrib/codex-engine-patches` | 2026-10-25 | silver2127 | 6 | 2 | Make Windows simulation networking default and bundle its driver |
| `contrib/ferrite-attention-load-test` | 2026-10-25 | silver2127 | 14 | 10 | Add an attention load test harness for per-sector simulation cost |
| `contrib/ferrite-attention-parity` | 2026-10-25 | silver2127 | 14 | 9 | Add a STATEHASH attention bucket and the attention-parity test plan |
| `contrib/ferrite-bucket-resync` | 2026-10-25 | silver2127 | 14 | 3 | Add bounded hash mismatch resync coordination |
| `contrib/ferrite-build-intents` | 2026-10-25 | silver2127 | 6 | 13 | Define the md.xsd build check only when X4_MD_XSD is set |
| `contrib/ferrite-dev-defaults-rules` | 2026-10-25 | silver2127 | 6 | 1 | Make features and hooks on by default during development |
| `contrib/ferrite-divergence-probe` | 2026-10-25 | X4 Contributor 3 | 14 | 1 | Add opt-in log-only divergence probe and offline comparator |
| `contrib/ferrite-engine-hooks` | 2026-10-25 | silver2127 | 14 | 7 | Merge bucket resync with attention and hashing controls |
| `contrib/ferrite-features-line` | 2026-10-25 | silver2127 | 6 | 12 | Log FEATURES lines and FEATURE DISABLED reasons for every feature |
| `contrib/ferrite-fixed-tick-stage3` | 2026-10-25 | silver2127 | 6 | 6 | Fixed-tick Stage 3: re-present the last frame while a simulation frame is late |
| `contrib/ferrite-fork-probe` | 2026-10-25 | silver2127 | 16 | 17 | Log the VBI table global and mover thread while the agent waits |
| `contrib/ferrite-hash-performance` | 2026-10-25 | silver2127 | 14 | 3 | Bound hashing cost and move hashing into Rust |
| `contrib/ferrite-integration-1` | 2026-10-25 | silver2127 | 6 | 11 | Record 7.6.0 automated test run and release checks |
| `contrib/ferrite-linux-preload` | 2026-10-25 | silver2127 | 16 | 6 | Add opt-in build-pinned Linux engine agent (LD_PRELOAD) and supervisor switch |
| `contrib/ferrite-load-profiler` | 2026-10-25 | silver2127 | 16 | 18 | Add load profiling: stage times, per-thread activity and a perf symbolizer |
| `contrib/ferrite-main-sync-7-5` | 2026-10-25 | silver2127 | 9 | 0 | Merge main (PR #47 instrumentation policy) into 7.5.0 for main sync |
| `contrib/ferrite-measurement` | 2026-10-25 | silver2127 | 6 | 2 | Enable frame profile and net probe by default in internal packages |
| `contrib/ferrite-npc-combat-intent` | 2026-10-25 | silver2127 | 6 | 12 | Replicate NPC combat intent; guest replicas aim and fire as puppets |
| `contrib/ferrite-npc-flicker` | 2026-10-25 | silver2127 | 6 | 1 | NPC replicas: fix guest flicker at highway speed and add NPC_DIAG counters |
| `contrib/ferrite-npc-target-diag` | 2026-10-25 | silver2127 | 6 | 2 | Enable the NPC targeting diagnostic by default in internal packages |
| `contrib/ferrite-player-context` | 2026-10-25 | silver2127 | 16 | 12 | Read the dedicated control record through x4lan_net; move stale logs aside |
| `contrib/ferrite-player-scope` | 2026-10-25 | silver2127 | 6 | 1 | Add player-scope inventory and inert guest halt/plunder slice |
| `contrib/ferrite-proto-v2` | 2026-10-25 | silver2127 | 16 | 12 | Add protocol v2 crate (x4mp-proto) and an opt-in N-player relay mode |
| `contrib/ferrite-proxy-motion` | 2026-10-25 | silver2127 | 6 | 1 | Remote player motion: NMODE motion modes, pose-only replicas at speed, stable render ... |
| `contrib/ferrite-puppet-client` | 2026-10-25 | silver2127 | 6 | 12 | Add puppet client mode: guest switches off X4's own NPC creation |
| `contrib/ferrite-rng-streams` | 2026-10-25 | silver2127 | 14 | 9 | Compare relocated vtable sites at the preferred image base |
| `contrib/ferrite-rust-core` | 2026-10-25 | silver2127 | 16 | 12 | Add x4mp-core: build-pinned FFI bindings, frame driver and MD queue |
| `contrib/ferrite-rust-relay` | 2026-10-25 | X4 Contributor 3 | 16 | 4 | Document the Rust port: parity results, findings and limits |
| `contrib/ferrite-shared-attention` | 2026-10-25 | silver2127 | 14 | 3 | Add shared remote-player attention hooks |
| `contrib/ferrite-simprof` | 2026-10-25 | silver2127 | 6 | 7 | Add simprof: sampling profiler of the MoveWorker in fixed-tick profile mode |
| `contrib/ferrite-simtick-net` | 2026-10-25 | silver2127 | 6 | 1 | Add inert frame_probe diagnostic for onUpdate rate vs game time |
| `contrib/ferrite-thread-timing` | 2026-10-25 | silver2127 | 14 | 9 | Compare relocated vtable sites at the preferred image base |
| `contrib/kimi-dedicated-server` | 2026-10-25 | silver2127 | 16 | 5 | Add Rust dedicated Linux server prototype |
| `contrib/kimi-fixed-sim-scheduler` | 2026-10-25 | silver2127 | 6 | 2 | Make Windows simulation networking default and bundle its driver |
| `contrib/silver-engine-instrumentation-policy` | 2026-10-25 | silver2127 | 156 | 0 | Update ENGINE-INSTRUMENTATION.txt |
| `contrib/astra-shot-hardening` | 2026-10-24 | X4 Contributor 1 | 123 | 0 | Fence inactive shot applications and preserve admitted hit counts |
| `contrib/claude-monitor-combat` | 2026-10-24 | JennyTheBelgian | 35 | 0 | Monitor: sanitized read-only combat diagnostics for human combat checks |
| `contrib/claude-projectile-protocol` | 2026-10-24 | JennyTheBelgian | 61 | 0 | PR41 review: range-check serializer numbers before isfinite |
| `contrib/claude-projectile-relay` | 2026-10-24 | JennyTheBelgian | 53 | 0 | PR42 review: retire on NAV2 sector change, admit 54/s six-gun plasma |
| `contrib/claude-shot-capture` | 2026-10-24 | JennyTheBelgian | 77 | 0 | SHOT capture: symmetric HEALTH closure fences and native player.controlled witness |
| `contrib/claude-shot-claims` | 2026-10-24 | JennyTheBelgian | 124 | 0 | SHOT design: consolidate into current decisions and blockers |
| `contrib/claude-victim-health` | 2026-10-24 | JennyTheBelgian | 126 | 0 | Design rev 2: consume-and-restore with native identity and runtime weapon channels |
| `contrib/claude-visual-windows-readiness` | 2026-10-24 | JennyTheBelgian | 71 | 0 | PR40 review: native-shaped IDs, actual-profile guard, own FFI declarations |
| `contrib/astra-7-3-0-delivery` | 2026-10-23 | X4 Contributor 1 | 146 | 0 | Keep agent handoff compact and load historical context on demand |
| `contrib/astra-7-3-1-delivery` | 2026-10-23 | X4 Contributor 1 | 137 | 0 | Keep human result notes brief and avoid duplicate transcription |
| `contrib/astra-source-publication` | 2026-10-23 | X4 Contributor 1 | 146 | 2 | Record approved public source mirror and anonymous access verification |
| `contrib/astra-station-bootstrap` | 2026-10-23 | X4 Contributor 1 | 158 | 15 | Prepare shared generation recipes, station matching and durable host checkpoints |
| `contrib/claude-damage-7-3` | 2026-10-23 | JennyTheBelgian | 146 | 3 | HIT2: claim only the LAN targets; kill at the claimed floor (PR #34 review) |
| `contrib/astra-7-1-2-delivery` | 2026-10-22 | X4 Contributor 1 | 156 | 0 | Record exact check runtime and include top-level human test guide |
| `contrib/astra-7-2-0-delivery` | 2026-10-22 | X4 Contributor 1 | 149 | 0 | Withdraw unsafe mod isolation helper and add assisted Workshop recovery |
| `contrib/astra-attachment-native-adapter` | 2026-10-22 | X4 Contributor 1 | 158 | 84 | Keep native attachment startup available after an invalid request |
| `contrib/astra-attachment-session` | 2026-10-22 | X4 Contributor 1 | 158 | 110 | Retain full guest drift evidence and add explicit native velocity hold comparison |
| `contrib/astra-seed-dock-admission` | 2026-10-22 | X4 Contributor 1 | 158 | 89 | Document retained pause after failed seed admission |
| `contrib/claude-attachment-relay` | 2026-10-22 | JennyTheBelgian | 158 | 60 | UATTACH relay: host heartbeats keep the downlink pacing (match PR29 7576bcb) |
| `contrib/claude-attachment-state` | 2026-10-22 | JennyTheBelgian | 158 | 35 | UATTACH v1 (Lua): reuse identity-map validation while the map is unchanged |
| `contrib/astra-attachment-authority` | 2026-10-21 | X4 Contributor 1 | 158 | 42 | Record native frozen dock import success and vanilla mission authority audit |
| `contrib/astra-attachment-integration` | 2026-10-21 | X4 Contributor 1 | 158 | 75 | Record startup-ready seed and acknowledged controller native pass with shader diagnostic |
| `contrib/astra-attachment-relay-review` | 2026-10-21 | X4 Contributor 1 | 158 | 56 | Keep attachment delivery fair under refresh and cover the admitted view within TTL |
| `contrib/astra-entity-lifecycle` | 2026-10-21 | X4 Contributor 1 | 158 | 31 | Record serialized native reconstruction pass and its bounds |
| `contrib/astra-native-seed-transfer` | 2026-10-21 | X4 Contributor 1 | 158 | 14 | Record independent Windows joint-run evidence audit and limits |
| `contrib/astra-native-world-authority` | 2026-10-21 | X4 Contributor 1 | 158 | 27 | Separate transient peaks from final native position errors |
| `contrib/claude-damage-probe` | 2026-10-21 | JennyTheBelgian | 162 | 16 | Native damage harness: pin the game folder before Install (PR #16 review) |
| `contrib/claude-native-seed-windows` | 2026-10-21 | JennyTheBelgian | 158 | 14 | Windows seed harness: bundle only run-time evidence (PR #22 review) |
| `contrib/astra-7-1-1-delivery` | 2026-10-20 | X4 Contributor 1 | 167 | 0 | Record matched 7.1.1 delivery and packaged native startup |
| `contrib/astra-7-1-handoff` | 2026-10-20 | X4 Contributor 1 | 198 | 0 | Clarify numeric evidence in the 7.1 handoff |
| `contrib/astra-angular-observation` | 2026-10-20 | X4 Contributor 1 | 163 | 0 | Record native inter-frame rotation evidence and remaining limits |
| `contrib/astra-native-velocity` | 2026-10-20 | X4 Contributor 1 | 173 | 0 | Record accepted lifecycle scope and failed projectile orientation control |
| `contrib/astra-projectile-research` | 2026-10-20 | X4 Contributor 1 | 189 | 0 | Record independent native firing-orientation and velocity evidence |
| `contrib/astra-shared-world-bootstrap` | 2026-10-20 | X4 Contributor 1 | 159 | 0 | Reject unresolved native HUD evidence and update coordination cadence |
| `contrib/astra-stationary-pose` | 2026-10-20 | X4 Contributor 1 | 182 | 0 | Record native remote-pose correction without fixture orientation assistance |
| `contrib/astra-velocity-frame` | 2026-10-20 | X4 Contributor 1 | 179 | 0 | Record six-orientation native velocity measurements |
| `contrib/astra-playtest7` | 2026-10-19 | X4 Contributor 1 | 201 | 0 | Prepare internal 7.1 candidate with measured native evidence and limits |
| `contrib/claude-combat-sync` | 2026-10-19 | JennyTheBelgian | 236 | 0 | Require the exact configured weapon count for the shared ship (PR #6 native evidence) |
| `contrib/claude-fire2` | 2026-10-19 | JennyTheBelgian | 233 | 0 | FIRE2: enforce the resume boundary in receive, playback and later baselines (PR #7 re... |
| `contrib/claude-windows-launcher-fixes` | 2026-10-19 | JennyTheBelgian | 242 | 0 | Own only per-attempt state temp files during install (PR #3 review) |

112 branches.
