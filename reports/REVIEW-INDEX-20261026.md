# Draft review index ? 26 October 2026

Snapshot: 03:20 EDT (07:20 UTC). These are source checkpoints for review, not a release or an integrated build. The owner requested publication so the other developers can inspect the work. Task branches are separate experiments; their presence on GitHub does not approve native behavior or authorize installing them. No branch was merged into main as part of publication.

## Start here

- Current deployed N-player source: [contrib/ferrite-np8 at 8ed861e](https://github.com/Lith7ium/X4-Multiplayer/tree/8ed861e99297467047bd71cd213d95532cc9c094).
- Shared rewrite baseline: [contrib/ferrite-rewrite-base at 1b7e2d3](https://github.com/Lith7ium/X4-Multiplayer/tree/1b7e2d3b122f998744d7be93d4b13fc403bfa123). Individual task branches contain additional changes and must be reviewed independently.
- This index lives on `contrib/codex-review-index-20261026`, based on the rewrite baseline. It adds documentation only. Historical top-level handoffs in that baseline may describe older delivery states; use this dated index and each task's tracked report to navigate the published checkpoints.
- Agree task ownership and interfaces with the project owner/lead before editing shared protocol or native hooks. Give each contributor an isolated checkout/worktree. Native test capacity and timing windows require coordination.

## Linux server and joining

The owner's Linux VPS endpoint is **76.13.109.115**, UDP **45841** for the relay and TCP **45844** for authenticated world transfer. A read-only check at **03:11?03:12 EDT on 26 October** observed the server service active, both ports listening and dedicated state ready with the authority connected. This is a dated observation, not a promise of continuing availability.

Deployed build: **7.6.0**, source **8ed861e**, package pair **internal-dist-124d714d7a862245**, world label **world1-7.6.0-np1-p8-9.00-steam23660954**. It supports eight slots including the dedicated authority. Native Linux **X4 9.00 / Steam build 23660954** is required. World DLCs, all version900:

- `ego_dlc_boron`
- `ego_dlc_mini_01`
- `ego_dlc_mini_02`
- `ego_dlc_pirate`
- `ego_dlc_split`
- `ego_dlc_terran`
- `ego_dlc_timelines`

Joining needs a **matched client package, privately assigned slot and private invitation**. Coordinate with the project owner/lead in [the existing Slack development thread](https://example.invalid/private-coordination-withheld), channel `#private-coordination-channel`. Astra currently has **player4 / guest3 / profile45843 reserved**; do not reuse that slot. Additional developers must request their own slot/package. Invitations, session tokens and configured client files do not belong in Git or public channel messages.

For Astra's assigned Linux package, `X4-LAN-7.6.0-Linux-Client3.tar.xz` has SHA-256 `40205027bf76b09786ced301bf74e00475997a3edf7b995952d1f5d8ef128c61`. The package and invitation were delivered privately. If a connector cannot download its 21,556,276 bytes because of a 10MB limit, request a private alternate transfer from the lead; the invitation alone is insufficient.

Close X4 before installation, preserve existing settings/Workshop and installations, extract the complete package into a fresh folder and place the invitation alongside it. Run `bash 01-INSTALL.sh` (the dedicated package selects the client role), then its `04-TEST-CONNECTION` and `05-START-X4` Linux starters. Do not run host-world preparation or choose an unrelated save. The starter downloads/verifies the shared world over TCP45844. Read the deployed branch's `DEDICATED-SERVER.md` for current details. A UDP-only check does not prove world transfer.

Report connection diagnostics such as `bubble slot=p4 relay=...` with sanitized error lines. Ships3+ still have limits: packaged projectile visuals are absent; pilot health/hit claims, trading and dock attachments remain limited to ships1/2. Visual presence alone does not prove shared combat or economy. The walking-avatar work listed below is **not included** in the currently deployed package.

## Rewrite checkpoints and evidence limits

Reported checks below belong to their particular task revisions and scopes. They were not rerun merely to publish branches. Original-code oracle runs require the developer's own pinned executable; no Egosoft executable or decompiler dump is distributed here.

- **Walk:** original stage1 replay failed semantically because production callbacks were missing and events were dropped, even though a harness passed. The continuation wires native callbacks and adds original-order keys/AI-only replay plus sticky abort reporting. C/Lua regression checks passed. New native wiring/replay acceptance remains pending. Event deferral can change immediate-handler semantics; no parallel walk or general equivalence claim.
- **AI driver:** reported idle normal-mode smoke/A-B evidence covers 16,966 frames and structural means within15%, not equivalent gameplay. Delayed immediate events are now flagged as unresolved ordering hazards.24 LuaJIT tests and both C suites passed at the checkpoint. Original-order Q20 integration and native battle comparisons remain pending. AIDirector's bound is64 passes (`0x40`), not33. Per-ship RNG policy is not approved; retain serial exact verification.
- **AI orders:** M3 `move.generic` is an offline replay-stage port;44 crate tests including9 new replay/regression tests and clippy passed. Earlier Patrol native wake-time evidence does not validate the new full move.generic semantics. Full query/value capture and native shadow acceptance remain open. Empty captures/brackets must not count as evidence.
- **ECS/mirror:** round-robin subset capture and explicit mixed-age state handling were added. C golden, Rust and Lua checks passed; truncated initial full captures now refuse rather than initialize an invalid subset stream. Full native validation previously matched1920/1920 local and1707/1707 sector comparisons, but full walking cost roughly111ms per captured frame. That is not a viable every-frame live mirror. Incremental/subset state is diagnostic and mixed-age, not a synchronized universe proof; native round-robin acceptance is pending.
- **Economy:**31 Rust tests plus clippy/fmt passed after comparison/parser and RNG endpoint fixes. Existing native captures still fail: lab4 has172 mismatches in1722 transitions; stress3 has1 in896. These failures are preserved. New lab5/trade1 evidence is queued. Physical-storage grouping/rounding and price targets need investigation; recorded efficiency inputs do not independently validate their calculation.
- **Combat:**39 focused Python/LuaJIT checks passed. Final-window failure emission, capture validity, RNG coverage and timing eligibility were tightened. Prior IK-joint shadow/switch evidence is narrower than full combat acceptance. Existing battle and battle200 runs were contended and are **not trustworthy timing**;400/800 and a fresh census remain pending. Fresh-bullet counters are timestamp proxies, not proof of unique first updates/creation order.
- **Transforms:** direct-memory walk and persistent pool replace the earlier expensive copy experiment. Reported original-code oracle seed9 covers400 scenarios,750 exact captures,1/8thread and4thread A/B configurations;8 FFI and5 Python acceptance tests passed. Native fast-path and speedup acceptance are pending. Independent review raised a duplicate-open-visit proof gap; follow-up guard work is ongoing. Do not infer that old copied-path native results validate this new fast path.
- **Physics:** replay validation now rejects empty/malformed records and captures mutable model outputs. Offline C/Rust regressions passed. Native census saw no forced models; steering dominated worker time, so it cannot validate the forced kernel. A new mass-traffic capture is queued, forced-model native coverage and full shadow/switch acceptance remain pending. Oracle coverage must name the pinned executable rather than silently omit it.
- **Frame diet:** unsafe `ui_elements` skipping stops the UI/onUpdate producer and is refused. Watchdog recovery does not turn a failed run into acceptance; historical bisect001 remains STABILITY FAIL. Lifecycle C,27 LuaJIT/analyzer and ABI checks passed. Safe `server+rng_split` native stability is queued; no new timing or production-package claim.
- **Native lanes:** releasee04b02a includes hold and quiet-window/reservation fixes. Deliberate-stall v6 acceptance correctly reports failure/exit3, terminates its owned process and restores the lane. This proves hang detection/cleanup, not gameplay. Coordinate the existing queue; do not duplicate queued tests. Windows cap:6 total X4 processes and at least14GB free before a launch; respect reserved slots and quiet windows.
- **Player factions:** strict correlated two-client evidence validation and stale-intent fencing are source checkpoints. Synthetic/Lua/relay checks passed; the inherited two-client acceptance is queued and prior s001 refused before launching. Native two-client acceptance, dedicated-human recognition, ordinary cargo/magnet/radar regressions and persistence remain open. Per-player money ledger is a proposal only.

## Other completed source checkpoints

- **Walking avatars:** WALK2 per-player forwarding, station/shared-ship avatars and elevator replay are implemented on `contrib/ferrite-walk-avatar`;717 suite tests with LuaJIT/no skips, Rust tests/clippy and relay parity were reported. Native avatar visibility/animation, station context identity, transporter behavior and moving-ship passengers remain unverified. Multi-crew/EVA are design notes. See `WALKING.md` and `distribution/HUMAN-WALK-TEST.txt` on that branch.
- **N-player/Linux delivery:** `np8`, `nplayer`, `linux-client`, `linux-server` and `linux-preload` expose the current implementation and its predecessors. Use matched packages and the deployed build pin, not an arbitrary branch tip.
- **Dynamic loading/save trim:** `contrib/ferrite-save-trim` publishes save analysis/trimming source. The runtime streaming experiment's harness/results remain local and its final report is unfinished. A completed managers spawn result does not settle station-module completeness or the missing minimal run.
- **Profiling/architecture/authority experiments:** additional branches below preserve the load/attention/simulation profiler, fixed timestep, RNG/timing, puppet client, protocol and shared-attention work. Each remains bounded by its own report and native evidence; none establishes complete shared-universe authority.

## Published task snapshot manifest

All pins below were pushed without force to same-named task branches. Later branch tips may advance; use the commit link when reviewing this exact snapshot. Credentials, packages, raw logs, local environments and game binaries were excluded. The small `mirror_golden.x4cap` fixture is generated from a synthetic component tree; `patrol_zone.x4aitrace` is hand-built synthetic input, not a player log.

- [contrib/ferrite-architecture-design](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-architecture-design) ? [7698736](https://github.com/Lith7ium/X4-Multiplayer/commit/76987362ea0fece8d5f5c615a5e99048c32b45fb).
- [contrib/ferrite-attention-load-test](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-attention-load-test) ? [5a5ce0d](https://github.com/Lith7ium/X4-Multiplayer/commit/5a5ce0d9d10bd521eeb7707444e68b2987da6921).
- [contrib/ferrite-attention-parity](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-attention-parity) ? [4a4d989](https://github.com/Lith7ium/X4-Multiplayer/commit/4a4d98965008eaececa038d7a3f108ce28fc34dc).
- [contrib/ferrite-bucket-resync](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-bucket-resync) ? [995c134](https://github.com/Lith7ium/X4-Multiplayer/commit/995c134d0dfd6c6ab1280f96fc256e73f01a2bbc).
- [contrib/ferrite-build-intents](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-build-intents) ? [acd0a20](https://github.com/Lith7ium/X4-Multiplayer/commit/acd0a20e605079d1c853db552e8eaad20733d4ae).
- [contrib/ferrite-dev-defaults-rules](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-dev-defaults-rules) ? [cffbce0](https://github.com/Lith7ium/X4-Multiplayer/commit/cffbce065474925f0fafffafe69b8b6c7777cf7f).
- [contrib/ferrite-divergence-probe](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-divergence-probe) ? [7c6fc41](https://github.com/Lith7ium/X4-Multiplayer/commit/7c6fc419c282179bc553e77c4aa48ec19715a520).
- [contrib/ferrite-features-line](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-features-line) ? [7726eac](https://github.com/Lith7ium/X4-Multiplayer/commit/7726eac178f094f81dced2e330ff57c24e796ad0).
- [contrib/ferrite-fixed-tick-stage3](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-fixed-tick-stage3) ? [af37f72](https://github.com/Lith7ium/X4-Multiplayer/commit/af37f725c2113be5a89d5d0603d106c7995d9839).
- [contrib/ferrite-fork-probe](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-fork-probe) ? [a5d7bbc](https://github.com/Lith7ium/X4-Multiplayer/commit/a5d7bbcc1095bf9aa9c55996b7421788cd121ac9).
- [contrib/ferrite-hash-performance](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-hash-performance) ? [9f28ae9](https://github.com/Lith7ium/X4-Multiplayer/commit/9f28ae9ab1ec2ada8bfa6c3c600aa7c20632f16f).
- [contrib/ferrite-integration-1](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-integration-1) ? [d2d3872](https://github.com/Lith7ium/X4-Multiplayer/commit/d2d3872943d585b99f9eafeeee270c53a7429ae7).
- [contrib/ferrite-linux-client](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-linux-client) ? [3e66966](https://github.com/Lith7ium/X4-Multiplayer/commit/3e669663ce699dc6c7dbb4be0ca5cef939d8a92b).
- [contrib/ferrite-linux-preload](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-linux-preload) ? [d9cc663](https://github.com/Lith7ium/X4-Multiplayer/commit/d9cc663f5affac8cf6d46447710910a5942a88e6).
- [contrib/ferrite-linux-server](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-linux-server) ? [8543b6e](https://github.com/Lith7ium/X4-Multiplayer/commit/8543b6ee467c8d583ff91b6222e17cd94ad7a33f).
- [contrib/ferrite-load-profiler](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-load-profiler) ? [f7b47f2](https://github.com/Lith7ium/X4-Multiplayer/commit/f7b47f27bc80264b0f2af950f33d6f169d321445).
- [contrib/ferrite-measurement](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-measurement) ? [d3d7ee9](https://github.com/Lith7ium/X4-Multiplayer/commit/d3d7ee9a96e6467d9b6c468e38b591c259762026).
- [contrib/ferrite-npc-combat-intent](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-npc-combat-intent) ? [0fdbf8e](https://github.com/Lith7ium/X4-Multiplayer/commit/0fdbf8e7377d6316e000153a6c5fe8d62607231a).
- [contrib/ferrite-npc-flicker](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-npc-flicker) ? [b5a8620](https://github.com/Lith7ium/X4-Multiplayer/commit/b5a8620fa0a42374062d3ac3bbdac3a6c9da395e).
- [contrib/ferrite-npc-target-diag](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-npc-target-diag) ? [8c22d59](https://github.com/Lith7ium/X4-Multiplayer/commit/8c22d59dafbc000e0918dd13cebc250ebc608f51).
- [contrib/ferrite-nplayer](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-nplayer) ? [b9f03b4](https://github.com/Lith7ium/X4-Multiplayer/commit/b9f03b4e699fe5d5659db9847d93ed416af9e374).
- [contrib/ferrite-overhaul](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-overhaul) ? [ab6c3e6](https://github.com/Lith7ium/X4-Multiplayer/commit/ab6c3e6d976d4f3ef00e105aca097288f78bf84f).
- [contrib/ferrite-player-context](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-player-context) ? [240b798](https://github.com/Lith7ium/X4-Multiplayer/commit/240b798f82b27634db6a34017ebf2d60ab9957a9).
- [contrib/ferrite-player-factions](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-player-factions) ? [016d592](https://github.com/Lith7ium/X4-Multiplayer/commit/016d5926f3a1c90684591d7278a3f43810bcff00).
- [contrib/ferrite-proto-v2](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-proto-v2) ? [f95fd59](https://github.com/Lith7ium/X4-Multiplayer/commit/f95fd597ecc0c975a448f8e3cf9fe8b10cbb24f9).
- [contrib/ferrite-puppet-client](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-puppet-client) ? [275db62](https://github.com/Lith7ium/X4-Multiplayer/commit/275db625c8b3b687ef2b67ff21d9403a22d1928e).
- [contrib/ferrite-rewrite-base](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rewrite-base) ? [1b7e2d3](https://github.com/Lith7ium/X4-Multiplayer/commit/1b7e2d3b122f998744d7be93d4b13fc403bfa123).
- [contrib/ferrite-rng-streams](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rng-streams) ? [47885db](https://github.com/Lith7ium/X4-Multiplayer/commit/47885db0f2f59ad53061b38e08e796617e091661).
- [contrib/ferrite-rust-core](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rust-core) ? [361a217](https://github.com/Lith7ium/X4-Multiplayer/commit/361a217dd261027274584a9b5af24d2ab4dec14e).
- [contrib/ferrite-rw-ai-driver](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-ai-driver) ? [9b6a028](https://github.com/Lith7ium/X4-Multiplayer/commit/9b6a028cc5d82b726b2cbd81fa6ed362ddb23ae8).
- [contrib/ferrite-rw-ai-orders](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-ai-orders) ? [d068cf1](https://github.com/Lith7ium/X4-Multiplayer/commit/d068cf1e234f07dd95948e351c1f0e49c48ee46e).
- [contrib/ferrite-rw-combat](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-combat) ? [8340fe4](https://github.com/Lith7ium/X4-Multiplayer/commit/8340fe43f6f46b150918ced3b9671733cf40e54e).
- [contrib/ferrite-rw-economy](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-economy) ? [60db4b5](https://github.com/Lith7ium/X4-Multiplayer/commit/60db4b5cd6729704a735df0531b38bdcb697d68a).
- [contrib/ferrite-rw-ecs-core](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-ecs-core) ? [62ae50c](https://github.com/Lith7ium/X4-Multiplayer/commit/62ae50c847f2752f85d7d0cb4f5b53856eed4485).
- [contrib/ferrite-rw-frame-diet](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-frame-diet) ? [9da12cf](https://github.com/Lith7ium/X4-Multiplayer/commit/9da12cf27d501599cf577d1d63f11b9afc7f4a17).
- [contrib/ferrite-rw-lane](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-lane) ? [d7f6dec](https://github.com/Lith7ium/X4-Multiplayer/commit/d7f6decf2c889ac80881b4754e29047a310bda27).
- [contrib/ferrite-rw-physics](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-physics) ? [96dcfe8](https://github.com/Lith7ium/X4-Multiplayer/commit/96dcfe8a690cb3a7439956eeff8a8c1ed81f934b).
- [contrib/ferrite-rw-transforms](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-transforms) ? [7fc0263](https://github.com/Lith7ium/X4-Multiplayer/commit/7fc0263b0f6d1ed8c719a9cdb7377b5debdf3257).
- [contrib/ferrite-rw-walk](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-rw-walk) ? [a60b058](https://github.com/Lith7ium/X4-Multiplayer/commit/a60b0584bc3473daad9c76ebf09dfb07d5d715b7).
- [contrib/ferrite-save-trim](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-save-trim) ? [bf61baa](https://github.com/Lith7ium/X4-Multiplayer/commit/bf61baa6f5f7bbd824c0a131562fcc4bf20069e3).
- [contrib/ferrite-shared-attention](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-shared-attention) ? [35b6882](https://github.com/Lith7ium/X4-Multiplayer/commit/35b6882fb3c70efeca75f5ee2f9e355e14ba408f).
- [contrib/ferrite-sim-decouple](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-sim-decouple) ? [37d6218](https://github.com/Lith7ium/X4-Multiplayer/commit/37d6218f5e4e808a6b7c28419b2d3f155c2071bd).
- [contrib/ferrite-simprof](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-simprof) ? [d00b8a1](https://github.com/Lith7ium/X4-Multiplayer/commit/d00b8a12721ae915596ffc896a218e8f9fc8dca2).
- [contrib/ferrite-thread-timing](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-thread-timing) ? [2ee6f37](https://github.com/Lith7ium/X4-Multiplayer/commit/2ee6f37872ee3c94f0cc17807542ca602769a3d8).
- [contrib/ferrite-walk-avatar](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/ferrite-walk-avatar) ? [7944822](https://github.com/Lith7ium/X4-Multiplayer/commit/7944822c76b7d3907c51ee15936f35c291034c1a).
- [contrib/kimi-fixed-sim-scheduler](https://github.com/Lith7ium/X4-Multiplayer/tree/contrib/kimi-fixed-sim-scheduler) ? [6e0e38f](https://github.com/Lith7ium/X4-Multiplayer/commit/6e0e38f607fe56afc4a4ffde1e72faea2b43c754).

Already visible source branches also include `contrib/ferrite-np8`, `contrib/ferrite-engine-hooks`, `contrib/ferrite-proxy-motion`, `contrib/ferrite-rust-relay` and `contrib/ferrite-player-scope`. Old merge-only aliases, the superseded initial faction-AI ownership branch and historical stage2 alias were not separately published; their meaningful work is reachable through the listed successors. No divergent remote history was overwritten.

## Review and integration

This index is a navigation aid, not blanket approval. Scoped independent review found and prompted the truncated-baseline fix; transform duplication and new capture/ordering boundaries are still being checked. Keep semantic failures visible and inspect each branch's report/source before accepting it. Integration requires separately reviewed changes, appropriate automated checks and coordinated native evidence. Do not merge all task heads or rebuild the deployed test pair as a side effect of reading this index.
