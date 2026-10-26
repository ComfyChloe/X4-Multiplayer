# Puppet client (first half of dynamic load)

Status: implemented on `contrib/ferrite-puppet-client` (base `contrib/ferrite-integration-1`), tested
offline only (LuaJIT mocks, game `md.xsd`, audit against extracted vanilla scripts). **Not run in
X4 yet.** All "effect" statements below are expectations until the native steps in the last
section have been run.

## Goal

On a client (guest, not the authority) X4's own world simulation must stop creating and ordering
NPC objects, so the client shows what the server sends. With a trimmed save (proposal
`preparation/TARGET-ARCHITECTURE-PROPOSAL.md` section 7b) the client must not refill job quotas by
spawning its own ships. The server stays authoritative for NPCs, economy, factions and missions.

## Mechanism and why

**Runtime switches from the mod's own MD script, triggered by the guest's UI Lua.** No vanilla MD
script and no library is patched.

| Option | Verdict |
|---|---|
| Library diffs (`jobs.xml` startactive/quotas, `parameters.xml` job factors) | Rejected: libraries apply to every installation of the extension, so the host would lose its jobs too. A second, client-only extension built by the launcher would work but doubles packaging and the install/ownership checks, and job quota factor 0 would also trigger X4's over-quota *cleanup* of existing ships. |
| MD diff patches that add `check_value not puppet` to vanilla cue conditions | Rejected as the primary mechanism: changes the host's vanilla scripts (a diff is applied on both roles even if gated), needs many selectors, and already-running instances (waiting in delays) do not re-check conditions. |
| **Runtime switch** (chosen) | Vanilla already exposes the needed actions: `set_job_active`, `get_suitable_job` (enumerates every job of a faction, including inactive/over-quota with `force`), `event_job_ship_activated`, `set_space_god_allowed`, `set_space_faction_logic_allowed`, `cancel_cue`, and the vanilla flag `md.GenericMissions.Manager.$Disabled` (used by Timelines scenarios). The host never receives the trigger, so it runs unmodified vanilla MD. |

Flow:

1. `ui/puppet_game.lua` decides the role: `puppet_client == true` **and** `slot == 'p2'`. Slot `p1`
   (host/authority) is never a puppet, whatever the flag says (`reason=host_role`). Logs
   `[X4LAN] PUPPET ARMED role=guest` or `[X4LAN] FEATURE puppet_client DISABLED reason=<config|host_role|invalid_value|unknown_role>`.
   If `X4LAN_FEATURES` (branch `contrib/ferrite-features-line`) is loaded, it also calls
   `F.declare('puppet_client', on, reason)`; the file works without it.
2. On the first frame with a valid player the guest sends `X4LAN_Puppet/Activate` once per second
   until MD acknowledges. This does not wait for the relay connection.
3. `md/x4lan_puppet.xml` `Activate` refuses if `md.X4LAN_Bubble.Arena.$host` is true (defence in
   depth), otherwise sets `State.$active` and runs the `Suppress` library (idempotent; repeated on
   every load).
4. `JobSpawnGuard` (active only while `State.$active`) catches any job ship that still activates,
   deactivates its job and removes the ship locally (`destroy_object explosion=false`), logging
   `PUPPET JOB_SPAWN_REMOVED job=… owner=… macro=… sector=…`.

Consequence: suppression starts a few seconds after load (first valid UI frame), not inside X4's
load steps. The `jobs` load step itself runs before any MD/Lua can act; see "Expected load-time
effect".

## Disabled systems

All in `md/x4lan_puppet.xml`, library `Suppress`. The cancel list is audited by
`tests/test_puppet_client.py` against the vanilla scripts.

| What | How | Reason |
|---|---|---|
| Job engine: every active job of every non-player faction | `get_suitable_job faction=… exceedquota force multiple` → `set_job_active activate=false` | Job ships (traders, miners, patrols, fleets, encounters, police) spawn and respawn to fill quotas; on a trimmed save this would refill the dropped ships. |
| Job ships that still appear | `JobSpawnGuard` on `event_job_ship_activated` | Story scripts re-activate jobs (`setup.xml`, `story_paranid.xml`, `job_helper.xml` Fleet_Evolution), shipyards finish queued job ships, spawns between load and activation. |
| God (NPC station creation) | `set_space_god_allowed space=player.galaxy allow=false` | Creates NPC stations. |
| Faction logic permission | `set_space_faction_logic_allowed space=player.galaxy allow=false` | Engine-level permission. Vanilla per-sector `allow="true"` (Heretic's End, story sectors) still overrides. |
| `md.FactionLogic.FactionLogicManagers` | `cancel_cue` | Root of every faction's `Manager` (all DLC factions are added under it by DLC diffs): fleet/job requests, `Econ_Manager` (station build decisions), `Maintain_Stations`, static defence (mines, laser towers), goal selection. |
| `md.FactionGoal_Hold_Space.Start`, `md.FactionGoal_Invade_Space.Start`, `md.FactionGoal_PatrolCoordinationService.Start` + `.CentralInformationCenter`, `md.FactionGoal_Plunder.Start` + `.PlunderNetwork` | `cancel_cue` | Running faction goals: command and request fleets, invasions, patrols, plunder raids. |
| `md.FactionSubgoal_BuildStation.Start`, `_DefendArea.Start`, `_PrepareStagingArea.Start`, `_Recon.Start` | `cancel_cue` | Sub-goals: build defence stations, move fleets, recon. |
| `md.Khaak_Activity.Manager` | `cancel_cue` | Kha'ak nests and raids. |
| `md.Crisis_Manager.Manager` | `cancel_cue` | Crises (Xenon/Kha'ak combos) spawn attackers. |
| `md.Fleet_Reconstitution.Init` | `cancel_cue` | Rebuilds faction fleets. |
| `md.Drain_Stations.Start` | `cancel_cue` | Drains NPC station resources (economy balancing). |
| Generic mission offers (`gm_*`, signal-leak offers, station offers) | `md.GenericMissions.Manager.$Disabled = true` | Vanilla's own switch; local offers would not exist on the server. Server missions still reach the guest through the existing `X4LAN_Missions` mirror (`Mirror` cue; it does not depend on GenericMissions). |

Every cancel logs `PUPPET CUE_CANCELLED cue=… before=<state> after=<state>` so the first native run
shows whether `cancel_cue` on a *complete* root cue really cancels its running sub-cues (expected,
to be verified). A summary row follows:
`PUPPET SUPPRESSED activation=N jobs_off=N jobs_off_total=N cues_cancelled_total=N generic_missions=off god=off faction_logic=off`.

### Kept (UI / player-facing or inert once jobs are off)

- Trade (`trade.xml` menus), upkeep (`upkeep*.xml`, player-fleet maintenance), orders, NPC
  instantiation/conversations (`npc_*.xml`), notifications, terraforming (player projects),
  tutorials, `placedobjects.xml` (new-game only), `inituniverse.xml` and `finalisestations.xml`
  (event-driven on stations the god creates; inert with god off).
- `encounters.xml`, `yaki_supply.xml`, `we_distress_call.xml`: driven by job-ship activation or
  jobs; inert once jobs are off. Not cancelled because ~29 vanilla scripts write into
  `md.Encounters.Manager` variables.
- Mass traffic: engine-driven, per proposal tier 5 every client generates its own.
- The mod's own co-op ships, NPC replicas (spawned by `universe_game.lua`, not job ships), docking
  and mission mirroring: no dependency on any cancelled cue (checked by grep).

### Unsure (not disabled; decide after the first native run)

- `diplomacy.xml` `Start` (agents, diplomatic events change relations; also player-facing UI).
- Story/plot scripts (`story_*`, `setup_dlc_*` world events such as Pirate "The Wave", Boron
  Herald), `x4ep1_*_subscriptions` (guild missions): player-facing, but can spawn NPCs. The
  proposal keeps story off on the server at first.
- `signal_leaks.xml` `Manager` (places signal leaks; offers already stop via the GenericMissions flag).
- `faction_relations.xml` `Init`, `npc_missions.xml` `Manager`.
- `set_space_jobs_allowed` (galaxy): not used. Its documented effect is on job *operating space*;
  it might make existing ships re-plan. Deactivating the jobs is enough to stop spawns.
- Whether `cancel_cue` on an instantiating static cue (`FactionGoal_*.Start`) also ends its
  running instances (expected; the before/after log row is the check).
- Whether `set_job_active false` makes existing job ships leave/despawn (vanilla uses it for
  phase switches, which suggests ships stay).
- DLC-only managers are not in the cancel list (no hard reference to DLC scripts, so a missing DLC
  cannot break the script). DLC factions are still covered by the job sweep and by
  `FactionLogicManagers`.
- NPC AI of ships already in the loaded save keeps running locally (trading, mining, patrolling).
  Their stations' production and trade offers keep being simulated by the local engine. Puppet
  mode stops *new* NPCs; replacing the remaining local NPCs is the second half (server streaming,
  plus the existing current-sector suppression in `universe_game.lua`).

## What clients see

| Visible as before | Empty or stale until the server streams it |
|---|---|
| Own ship, co-op ships, NPC replicas in the current sector | **Mission offers** on the BBS and at stations: only server missions mirrored by `X4LAN_Missions` (already the case for `gm_destroy_rarelyonsight` on the guest). |
| Docking, trading UI, map, station menus | **New NPC ships** anywhere outside the replicated sector: no local spawns; with a trimmed save distant sectors look empty. |
| Missions already shown in the UI (mirrored) | **NPC station construction / expansion, faction fleets, invasions, Kha'ak/Xenon raids, crises**: nothing happens locally. |
| Existing NPC stations and ships from the loaded save | **Trade offers** keep the values of the local engine simulation (diverge from the server; existing behaviour). |

A save written by a puppet client keeps the cancelled managers and inactive jobs. It must never be
used as an authority world. The host logs
`ERROR FEATURE puppet_client DISABLED reason=puppet_state_in_loaded_save` if it detects one.

## Logs and measurement

- `PUPPET v=1 role=… puppet=0|1 npc_ships=N replicas=R local_npc_ships=N-R factions=F
  [baseline=B delta=±D] [md_active=… job_spawns=… removed=… jobs_off=… cues_cancelled=…]` every
  30 s on **both roles** (`npc_ships` = ships of all non-player factions via
  `GetNumAllFactionShips`). On the guest, `baseline` is taken at the first census after activation;
  `delta` is the net local growth since then (spawned minus destroyed). `job_spawns`/`removed` are
  exact counts from `JobSpawnGuard`.
- `LOADTIME v=1 role=… puppet=… source=progressbar.xml bytes=… key=<attribute> stages=<name>:<value>,… sum=…`
  on both roles at the first valid frame, read from `<profile>/progressbar.xml` (profile =
  `GetSaveFolderPath()` without `/save`). X4's UI Lua has no `io`, so the file is read through libc
  (`fopen`/`fread`) via FFI. The schema of `progressbar.xml` is not documented and was not inspected
  (the profile folder is off limits for this task): the parser accepts any element with a stage
  name and a numeric attribute or body. If nothing parses it logs
  `LOADTIME unavailable reason=no_stages head=<first 160 chars>` so one native run shows the real
  format. The file is read again after 120 s and logged with `reread=1` if it changed, which shows
  whether X4 writes it before or after the UI starts (i.e. whether the first row is this load or
  the previous one). Values are logged as recorded; units are not converted.
- Manual fallback: copy `<profile>/progressbar.xml` after each load and compare.

## Expected load-time effect

- **Full save (today's shared start):** no change to X4's own load steps. The `jobs` step (11.2 s
  on the test PC) runs before any mod code; puppet mode only prevents spawning afterwards.
  Expected: after activation `local_npc_ships` stops growing (`delta` ≈ 0 or negative as ships
  die), `job_spawns` stays near 0, frame time on the guest may drop slightly (fewer faction-logic
  cues).
- **Trimmed save:** the reduction comes from the trimmer (fewer objects in `map2`/`jobs`/`scripts3`).
  Puppet mode's part is to keep the client from refilling quotas afterwards. Whether X4 spawns
  quota-filling ships *during* the `jobs` load step for a save with missing job ships is
  unknown; if it does, those spawns happen before activation. `JobSpawnGuard` does not see them,
  but they show as a high `npc_ships` in the first census. Remedy for the trimmer: if the save
  stores per-job active state (the engine has a `JobDB` XML writer, so job state is likely saved;
  to verify), the per-client rewrite can mark jobs inactive in the save itself.

## Native test steps (not yet run)

Preconditions: X4 closed, isolated profile 45841, disposable world, autosave off, packages built by
`distribution/session_launcher.py` (host `puppet_client=false`, guest `puppet_client=true`).

1. **Vanilla reference:** load the normal full shared-start save with the mod's internal package on
   the host only (guest role not involved). Record `LOADTIME` rows (both `reread=0` and `reread=1`)
   and `PUPPET role=host puppet=0` rows over 10 minutes.
2. **Puppet guest, full save:** load the same save as the guest. Check in the debug log:
   - `PUPPET ARMED role=guest`, then `PUPPET ACTIVE …`, 15 `PUPPET CUE_CANCELLED` rows with
     `after=cancelled`, `PUPPET SUPPRESSED … jobs_off=<hundreds>`;
   - host log: `FEATURE puppet_client DISABLED reason=host_role role=host`, no `PUPPET ACTIVE`.
   - Over 10 minutes: `delta` ≤ 0 and `job_spawns`/`removed` small; compare `npc_ships` with the
     host rows.
   - Compare `LOADTIME` stages with step 1 (expected equal for the same save).
3. **Safety:** on the guest, fly the co-op ship, dock at an NPC station, open trade and map, accept a
   mirrored mission; check NPC replicas in the current sector; check no `X4LAN` errors besides
   the expected rows.
4. **Re-load:** load the guest world again; `PUPPET ACTIVE activation=2` (the switch re-runs).
5. **Trimmed save** (when the trimmer is ready): load as puppet guest; compare `LOADTIME` stages
   with step 2 and the first census `npc_ships` with the trimmed object count (detects spawning
   during the load `jobs` step).
6. Decide the "unsure" items from what stays active (story spawns, diplomacy events).

## Files

- `extension/x4_lan_lab/md/x4lan_puppet.xml` – state, activation, suppression, job-spawn guard.
- `extension/x4_lan_lab/ui/puppet_game.lua` – role gating, activation handshake, census, LOADTIME.
- `extension/x4_lan_lab/ui/config.lua` – `puppet_client = false` (inert source).
- `extension/x4_lan_lab/ui.xml` – loads `puppet_game.lua` before `frame_profile_install.lua`.
- `distribution/session_launcher.py` – `puppet_client=(slot == 'p2')` in internal packages.
- `tests/test_puppet_client.py` – role gating, census, LOADTIME parser (LuaJIT); packaging; `md.xsd`
  validation and vanilla cue audit (need `X4LAN_VANILLA_SCRIPTS` or `../x4-re/gamescripts/resolved`;
  the `md.xsd` compile takes about 85 s; both skip loudly when absent).

Reference note: the extracted vanilla tree (`x4-re/gamescripts/resolved`) contains MD, AI scripts
and schemas but no `libraries/jobs.xml`; this design does not need it because jobs are enumerated
at runtime per faction.
