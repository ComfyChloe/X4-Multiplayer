# Target architecture proposal: dedicated authority, N players, Rust core

Status: **proposal for review**, 26 October 2026. Author: Ferrite (project lead).
Nothing here is implemented unless a section names an existing branch or file.
Measured facts and assumptions are labelled; open questions are listed at the end.

## 1. Why change

The current mod grew one protocol per feature (`NAV2`, `UMOVE`, `FIRE2`, `HEALTH`,
`SHOT`, `CARGOREQ`, `QUEST`, mission records, …), each with its own timing, rollback
and authority rules, around a fixed host/guest pair. Every new symptom (highway
flicker, NPCs not engaging the guest, lost NPC fire) gets its own fix. This proposal
replaces that with a few general mechanisms that every feature reuses, sized for
several players from the start.

## 2. Principles

1. **One authority.** The dedicated Linux X4 on the server owns the universe: NPCs,
   economy, stations, missions, money. There is no privileged "host player".
2. **N players from the start.** Player IDs, not slots `p1/p2/p3`. Two players is a
   configuration limit, not a code assumption.
3. **Own ship stays local.** Each client simulates its own ship; no input latency.
4. **Hooks in the engine, logic in Rust.** Per-frame work runs in Rust in a VBI
   callback; MD/Lua shrink to thin, event-driven glue (section 9).
5. **Clients stay as vanilla as possible.** Engine patches that change game rules
   (e.g. extra player factions) run on the server only.
6. **Determinism is an optimisation, not a foundation.** Seeded RNG, attention parity
   and fixed tick reduce corrections; correctness never depends on them.

## 3. Authority rules

| Decision | Owner |
|---|---|
| NPC AI, NPC target choice, NPC health and death | server |
| Economy, stations, trade, money ledger, missions | server |
| Own ship movement | that player's client |
| Hits **on a player's ship** (incl. NPC fire) | the hit player's client (victim-authoritative; generalises today's `HEALTH`) |
| Hits **by a player on NPCs** | the shooter's client, plausibility-checked by the server (range, rate of fire, target alive) |
| Player actions (cargo, dock, missions, fleet orders, purchases) | client sends an **intent**; server decides; result arrives in the state stream |

No ownership handoff of NPCs between clients: several players can fight the same NPC
without ownership flapping. Rewinding the server to the shooter's view is only needed
if PvP fairness becomes a goal.

## 4. Replication: sector channels and relevance tiers

The server publishes state per **sector channel**. A client subscribes to its current
sector at full detail and to adjacent sectors (one gate/highway away) at a low rate,
so a jump starts from preloaded state. Within a sector, rate follows relevance:

| Tier | Objects | Rate (initial guess, to be measured) |
|---|---|---|
| 1 | other players' ships | 20–30 Hz |
| 2 | your target, anything attacking you, NPCs within weapons range | 20–30 Hz |
| 3 | other real NPCs in your sector | 1–2 Hz + client prediction |
| 4 | other sectors | results on change (ownership, station status, battles) for the map |
| 5 | ambient "mass traffic" | **not replicated**; each client generates its own |

Ambient traffic exception to verify: whether mass-traffic objects can be shot or
otherwise interacted with. If so, an interaction promotes the object or is suppressed.

## 5. Motion

- Snapshots carry the **simulation frame number** (fixed tick, see
  `reports/FIXED-SIM-SCHEDULER.txt`), position in **sector coordinates** (stable across
  X4's zone/temp-zone changes), velocity, angular velocity, and a motion mode
  (normal/boost/travel/highway, as in `NMODE` on `contrib/ferrite-proxy-motion`).
- One **adaptive interpolation buffer** per remote player: delay ≈ send interval +
  2 × p95 jitter, instead of the fixed 150 ms. Cubic (Hermite) interpolation using
  velocity; bounded prediction; corrections are blended over 100–200 ms, never
  snapped below a large threshold.
- Send rate scales with speed, which removes the need for highway-specific code.
- The replica also receives native velocity so engine effects stay natural (today the
  NPC replica renderer applies pose only).

## 6. NPC combat: replicate intent, not shots

The server sends per relevant NPC: current target, firing state per weapon group and
manoeuvre state, alongside motion. Client replicas have no AI of their own but act as
**puppets**: they face the replicated target and fire locally while the server says
"firing". A lost packet delays the state instead of losing fire for the rest of the
fight (the current failure). Hits on players follow section 3.

Known reasons NPCs currently ignore the guest (diagnostic on
`contrib/ferrite-npc-target-diag`, not yet run natively):
1. guest replicas have AI disabled and host fire replication drops out;
2. on the host the guest's surroundings may run at low attention
   (`contrib/ferrite-attention-parity` raises it, untested natively);
3. vanilla scripts special-case the single local player (`player.occupiedship`:
   111 AI-script and 172 MD references; e.g. `aiscripts/lib.target.selection.xml:276`).

## 7. Players, owners and the player faction

X4 has one player faction. Vanilla scripts reference it widely (base game + DLC:
2,609 references in 319 files; ~2,200 `isplayerowned`; story/gamestarts/tutorials
~1,300; orders/combat/trade/mining/construction/missions ~550).

Proposal:
- **Server:** one faction per human player. `faction.player` and `isplayerowned` become
  **context-dependent**: while an AI script or MD cue runs for an object owned by
  player K, `faction.player` resolves to player K's faction. This needs hooks at the
  script keyword resolution, the `isplayerowned` property and the script-context
  start, not edits to thousands of lines. Global cues without an owner fall back to a
  default or take the acting player from the event.
- **Clients:** your own faction is the real `player` (vanilla menus and control); other
  players' assets arrive as their own friendly mod factions (visible, not commandable).
- **Per player:** assets, money (each faction's own account, or a ledger), fleet orders.
  **Shared initially:** reputation, research, blueprints. Story content is off on the
  server at first.
- Open until the read-only RE survey (`x4-re/coord/reports/player-faction.md`)
  reports: where the engine stores and compares the player faction, how many C++ sites
  a script-level context switch would miss, where money/licences/logbook live, and
  whether mods can define factions without faction AI.

### 7a. Player factions in diplomacy

The vanilla diplomacy menu lists every faction from `GetLibrary("factions")` and
removes only the id `player` (`ui/addons/ego_detailmonitor/menu_diplomacy.lua`). The
other players' factions (`player2` …) therefore appear like normal factions once
they are known (`set_faction_known`), with name, colour, icon and relation.

- Relations between players live on the server (relation matrix between player
  factions); each client receives its row. Default: allied.
- War, ceasefire and alliance between players are intents (section 8); both sides see
  the change through the state stream.
- For player factions, X4's AI diplomacy (agents, embassies, diplomatic events) is
  off: `isdiplomacyactive` / `arediplomacyeventsallowed` false.
- Factions cannot be created at runtime (no MD action; the engine imports factions
  only at load). The mod therefore defines a **pool** `player2` … `player16` in
  `libraries/factions.xml`, inactive and unknown until a player is assigned a slot.
  Extension factions are added to existing saves on load, as DLC factions are.
- On assignment the server uses vanilla actions: `set_faction_identity` (at runtime:
  name, short name, prefix, space names, description, icon, ship decal `image`, menu
  `banner`, and `personal="true"` to load logo/banner from the profile folder, which
  allows each player's own logo to be distributed and shown on their ships),
  `set_faction_known`,
  `set_faction_active` and `set_faction_relation`. Colour is not settable at runtime,
  so each pool slot has its own fixed colour. To verify natively: plain-text names
  across save/load (docs recommend `{page,line}`) and personal logos on Linux.
- Clients carry the same pool and map each player to the same slot number.

### 7b. Hot join (decided: fork snapshots)

Players join a running server without pausing the others:

1. The dedicated Linux server takes savegame snapshots from a `fork()`ed child: the
   parent keeps simulating after a short hitch (page-table copy), the child writes the
   save from its copy-on-write memory and calls `_exit`.
2. Snapshots run on a schedule (e.g. every 5 minutes) and on demand; a joining player
   receives the latest one immediately. The same snapshots serve crash recovery.
3. The server rewrites the compressed XML save per client so that the joining
   player's faction becomes `player` (section 7).
4. After loading, the client replays the event log since the snapshot frame and then
   switches to its live sector channels.
5. Blocking risk under investigation (`x4-re/coord/reports/save-fork.md`): in the
   child only the forking thread exists, so the save path must not depend on job
   threads, locks held by other threads, Vulkan, audio, Steam or sockets. If the
   engine serializer cannot run that way, fall back to quiescing before the fork or
   to a custom serializer reading the copied memory.
6. Windows clients load a save produced on Linux; compatibility (XML) to be verified.

## 8. Intents

All player actions use one path: client applies optimistically and sends an intent
(`kind, object, params, player, client seq`); the server validates ownership and
rules, executes with native X4 logic, and the outcome arrives through the state
stream; only a refusal rolls back. This absorbs today's `CARGOREQ` pattern and covers
fleet orders (latency is irrelevant for orders that run for seconds to hours),
docking, missions and purchases.

### 8a. Station and ship construction

Designing stays local; only confirmation crosses the network.

- The vanilla station planner (`menu_station_configuration`, FFI such as
  `UpdateConstructionMapBuildPlot`, `ShuffleMapConstructionPlan`,
  `AddFloatingSequenceToConstructionPlan`) runs unchanged on the client against the
  replicated station state. Saved plans stay per player as XML in the profile
  (`SaveMapConstructionPlan` / `ExportMapConstructionPlan` / `ImportMapConstructionPlan`);
  sharing a plan means sending that XML.
- The planner's confirm is intercepted (Lua menu override): instead of a local
  `AddBuildTask*`, the client sends an intent "expand station S with plan P, build
  plot, budget".
- The server validates ownership of S, blueprints of the sender's faction
  (`GetMissingConstructionPlanBlueprints`, `requireblueprints`), money, the paid build
  plot and plan validity (`GetConstructionPlanInvalidPatches`,
  `CheckConstructionPlanForMacros`), then creates the build natively
  (`AddBuildTask*` / `add_build_to_expand_station`). Construction runs in the server
  simulation; new modules reach clients through station replication.
- Clients never create local build tasks (puppet universe).
- Blueprints: with shared research/blueprints, the server mirrors them to every
  player faction (`add_blueprints` / `LearnBlueprint`).
- Only the owner may modify a station; allied co-building is a later extension.
- Ship construction and loadouts at shipyards follow the same pattern
  (`add_build_to_construct_ship`, `apply_loadout`).

## 9. Runtime split: Rust core, thin MD/Lua

| Layer | Work |
|---|---|
| **Rust**, per-frame VBI callback (Windows DLL / Linux `LD_PRELOAD` agent) | transport, protocol, sector channels, relevance, snapshot buffers, interpolation and prediction, intent validation, money ledger, state hashing; reads and writes engine state through the **exported C functions** of X4's Lua FFI API (about 2,376 shared by Linux and Windows; Linux exports 11,250, Windows 2,378; no velocity/angular-velocity or macro exports, and some exports such as `MakePlayerOwnerOf` are empty stubs; see `rust/x4mp-core/FFI-INVENTORY.md` on `contrib/ferrite-rust-core`) |
| **MD** (thin, event-driven) | only engine actions without a C export (`create_ship`, missions, ownership changes, NPC orders), executed from a queue; no polling cues |
| **Lua** (thin) | UI and event bridging; no per-object work per frame |

Existing pieces: the Rust hooks crate (`contrib/ferrite-engine-hooks`, observer in
slot 12, hashing in Rust), the Rust relay and dedicated supervisor
(`contrib/kimi-dedicated-server`), the Linux agent with the slot-13 frame callback
(`contrib/ferrite-linux-preload`), and `x4lan-net` in Rust.

**Rule change required:** `AGENTS.md` says "Keep the `x4lan_net` transport free of
hooks." Proposed wording: the transport crate stays hook-free and independently
testable; the hook layer links it as a library and drives it from the frame callback.

## 10. Measure before porting

1. Per-frame cost of today's Lua/MD by subsystem (motion, replicas, health, shots,
   universe baselines), with the same QPC budget method as `HASHCOST`.
2. Real Internet RTT and jitter per player through the VPS relay.
3. Server CPU per occupied sector at full detail (MoveWorker is largely single
   threaded; this sets the practical player/sector limit on the VPS).
4. Fixed-tick behaviour on the server (`fixed60`, then Stage 2 catch-up).

Port in order of measured cost; keep each step behind a config switch with the old
path as fallback until native tests pass.

## 11. Migration plan

1. **Foundations:** player IDs instead of slots; frame-stamped messages; RTT/jitter
   telemetry; owner field on every replicated object.
2. **Motion core in Rust:** `NAV2`/`UMOVE` replaced by sector-channel snapshots with the
   adaptive buffer; highways via speed-scaled rate and sector coordinates.
3. **NPC combat intent** replication and victim-side hits on players.
4. **Intent path:** cargo first (replacing `CARGOREQ`), then docking, fleet orders,
   missions.
5. **Owners:** server player factions with the context-dependent `faction.player`
   hooks (after the RE survey); per-player money; player factions in diplomacy.
5a. **Hot join:** fork snapshots, per-client save rewrite, event-log catch-up.
6. **Relevance tiers and ambient-traffic exclusion;** coarse map channel.
7. **Determinism work** (RNG streams, attention parity, thread timing, fixed tick) to
   shrink corrections for predicted NPCs.

Each step needs its own native acceptance test; synthetic peers are not evidence of
native multiplayer behaviour.

## 12. Open questions

- Engine storage/comparison of the player faction; C++ sites missed by a script-level
  context switch (RE survey).
- Can ambient mass traffic be interacted with?
- Which exported FFI functions cover pose/velocity writes for replicas on both
  platforms, and are they safe to call from the MoveWorker frame callback?
- Server CPU headroom for several full-detail sectors.
- Per-player vs shared: reputation, research, blueprints, licences, logbook.
- Story content in multiplayer (off at first).
