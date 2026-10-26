# Internal architecture and protocol reference

This document contains protocol history as well as still-used interfaces. For
current acceptance and the 7.4 combat/starting-world paths, begin with
[the handoff](../CONTINUE-HERE.txt) and [current source map](../onboarding/03-CODE-AND-STATUS.txt),
then inspect the relevant source and dated report. Historical FIRE2 descriptions
alone do not describe the complete current shot-capture/health/visual stack.

The new scene transport and engine adapter are original code. Public Test 01 is
withheld until the broader [shared-universe requirements](WORLD-SYNC-REQUIREMENTS.txt)
are met. The follow-up synchronizes NPC scene visibility/motion and adds ordinary
co-op cargo transactions plus shared display identity. General NPC interactions
and the full economy remain incomplete.

## W01 NPC sector scenes

`UniverseSession` extends `CoopSession`, enabled only by `world_sync` with a
`world1-` build label. The native adapter also requires isolated profile 45841.
Source defaults remain disabled. Internal packages explicitly select replica mode.

The host enumerates all faction fleets incrementally, filtering for the requested
sector and excluding player-owned ships, live player occupants and co-op fixtures.
Each native component key receives a monotonically allocated session ID; exact
64-bit keys never pass through Lua floating-point numbers. A changed macro gets
a new ID. Component-ID reuse with the same macro has not been proved impossible;
engine generation/deletion hooks remain necessary for full lifetime authority.

`UINTEREST` selects the guest sector. The host captures complete sorted records:
network ID, kind, macro, owner, position/orientation, reserved ware/amount fields.
`UBEGIN`, indexed `UOBJECT` and `UCOMMIT` form an Adler-checked baseline. `UACK`
selectively acknowledges a window of 12 packets, retrying after 250 ms. A partial
baseline cannot replace committed state. Empty committed baselines explicitly
remove a sector view. Corruption restarts assembly; duplicate commits do not
repeat engine effects. The host lease is the session epoch. A fresh host requires
a fresh relay and guest world. Relays cache at most eight sectors; each baseline
is limited to 4096 objects. An overflowing enumeration never publishes a prefix.

`UMOVE` carries host epoch, object ID, sequence, sector, source time, pose and
linear/angular velocity for known committed IDs. Units are seconds, metres and
radians. Legacy `UPOS` remains readable.

NPC motion is a shared prediction (`motion.lua` `M.drPredict`): pose plus velocity
times elapsed source time, with a constant turn rate when the velocity heading matches
the yaw (turns capped at 90 degrees, then straight). The host reads every published
NPC each motion tick (one fixed 0.05 s server step), takes its velocity from the
displacement over that tick (none across a jump above 100 km/s), and sends a sample
only when the real pose diverges from the prediction of the last sent sample, or as a
keepalive. Relevance sets thresholds and rates: hot (within 6 km of a player ship, or
a client's target) 3 m / 0.02 rad, up to 20 Hz, keepalive 1 s; warm (within 25 km)
25 m / 0.06 rad, up to 5 Hz, keepalive 2 s; cold 150 m / 0.2 rad, up to 2 Hz,
keepalive 4 s. At most 32 samples per tick, most diverged first. A client reports its
selected NPC with `UFOCUS|1|lease|seq|id` (0 = none, refreshed every second); relays
forward each new sequence to the host (`|clientLease` appended on the dedicated relay),
which treats it as hot for 3 s. Relays forward samples a client has not received
first (younger than 0.75 s oldest arrival first, then up to 5 s old for a late joiner
or sector change) and repeat at most two recent ones per tick.

Clients dead-reckon on the host's clock. The dedicated server steps a fixed 0.05 s per
frame at 12-20 fps, so its clock runs at about 0.6-1.1 x wall time; the client estimates
rate and offset from the least-delayed samples (sliding 3 s window) and slews its render
clock at most 0.1 s/s. A new sample's error blends out with a 0.3 s time constant; an
error above max(1 km, 1 s of speed) snaps, at most once per second per replica, logged
as `NPC_SNAP`. Prediction coasts 8 s past the last sample, then holds. Replicas moving
at 150 m/s or more, or moving within 6 km of the player, are written every frame (up to
256); the rest at most 128 per frame no faster than every 15 ms. When the engine pose
slipped from the last written pose by more than max(5 km, 2 s of speed) (zone-frame
re-parenting, as for co-op ships), replicas near the player learn the zone offset per
source zone and compensate later writes; other slips are rewritten, and a slip lasting
1 s respawns the replica at its pose (`NPC_FRAME_SLIP` counts each action). A baseline pose replaces a live track
only after 8 s without motion; replicas pause only when the baseline is 15 s old and
no motion arrived for 5 s. The co-op `NAV2` path still uses the interpolation buffer
(minimum-latency offset, 150 ms delay, 350 ms prediction). Engine changes wait for an
unpaused frame; transport acknowledgement alone is not engine success.

Guest replica mode removes independent NPCs only in its current sector and only
following a complete, recent baseline. It creates host macro/owner replicas via
engine APIs, disables their AI and applies host poses. Local player-owned ships,
co-op fixtures and player occupants are protected. A replica occupied by the
player stops the adapter instead of removing the player. This mode is only for
disposable worlds: deleting native NPCs can invalidate local vanilla missions.
The adapter does not yet suppress independent stations, production, missions or
all consequences of the guest simulation. Observe mode receives baselines and
logs counts without changing game objects.

## Native ordinary cargo

`cargo.py`, `cargo_game.lua` and `x4lan_cargo.xml` implement ordinary drops from
the two shared ships. Host X4 owns inventories and container lifetime. Baseline
drop records contain one ware/quantity and use the common world ID allocator.
The host keeps native dropped objects in the registry until collected/removed.

Guest native drop/collection is observed after the engine prediction. MD rolls
that local inventory change back immediately; Lua submits an intention:

`CARGOREQ|1|lease|requestID|hostEpoch|ship|pilotEpoch|DROP/PICKUP|dropID|ware|amount`

The relay authenticates pilot ownership/epoch and blocks conflicting handoff or
trade. IDs are retained (bounded at 8192, never evicted into reuse). Host execution
is retried every 250 ms using `CARGOEXEC`; both Lua and MD deduplicate before any
mutation. MD checks live ships, ware, cargo, capacity and pickup distance/sector.
`CARGODONE` and `CARGOREPLY` carry outcome plus the committed baseline fence.
An accepted mutation waits for a new baseline; a non-mutating stale epoch uses
the existing revision. A baseline that overtakes completion already satisfies
its fence. Host loss yields UNCERTAIN and never replay into a replacement host.

Native guest containers must be actual full-quantity `drop_cargo` results from
the fixture donor. Generic pickup objects and adding quantity after a one-unit
drop do not produce correct magnet behavior. Donor capacity failure stops the
adapter explicitly. Asynchronous creation callbacks carry a local generation;
late/duplicate callbacks cannot adopt or remove the wrong instance. Pickup
callbacks bind network ID, generation and exact native object. One pending
pickup per instance prevents repeated partial-magnet events from adding another
intention. The host's remaining quantity is then recreated with a new generation.

Host-only `CINV|1|lease|hostEpoch|sequence|ware|count1|count2` updates reconcile
tracked wares and override the older ECON cargo rows. Non-OK outcomes show a
throttled notice; uncertain results require ending the test, not fresh-ID retry.
Controlled native host and guest checks cover drop/magnet, duplicate intentions,
lost completion, capacity and partial quantity. Two-native acceptance remains
separate. Arbitrary NPC loot, multi-ware crates and all cargo categories are open.

## Player factions

`player_factions` (internal packages; inert in source) gives every human an own faction from the
pool `x4lan_player1..16` (`libraries/factions.xml`). Each machine maps its own human to `player`
and every other human K to `x4lan_playerK`. `PFID`/`PFROSTER` carry names, the host authors
relations (`PFREL`, guest intents `PFREQ`) and sector owners (`PFSEC`); the relay
(`src/x4lan/players.py`) only routes and caches. Shared co-op fixtures stay `player`-owned. Details,
limits and native test steps: [PLAYER-FACTIONS.txt](PLAYER-FACTIONS.txt).

## Display identity and shared ship observations

`IDENT|1|lease|hostEpoch|sequence|scope|id|code|UTF8nameHex|known|contact` is
host-authenticated, bounded and sequenced. Scopes separate P (co-op ships 1/2),
N (committed universe IDs) and F (targets 1/2, markets 3/4). Names and displayed
codes come from the host. Native component IDs remain local. A narrow LuaJIT
`ffi.C.GetObjectIDCode` lookup wrapper supplies shared display codes while keeping
all other C functions/native fallback intact; X4 has no `debug` library, so the
wrapper uses ordinary `getmetatable`. Native target-monitor headers verify the
actual display path, not just a mocked getter.

Radar flags describe host-visible ships within either shared ship's radar range,
and expire locally after 2.5 seconds without a fresh observation. This is not
complete galaxy discovery or synchronization of every station/object type.
Full equipment/damage/ownership authority, station state, native generation hooks
and stronger guest simulation control remain separate work.

## Existing two-ship cooperative fixture

The native Linux X4 game is the authority for shared damage, cargo, treasury and
mission outcomes. The Python UDP relay validates sessions, routes snapshots,
deduplicates actions and arbitrates which pilot may publish each ship's movement.
It does not simulate the universe. Windows runs its own X4 universe and applies
replicas of the explicitly shared objects.

## Session and stable objects

Two player slots authenticate with a matched package key and build/session IDs.
Dedicated sessions admit `p1..pN` and one shared ship per client; see
[DEDICATED-SERVER.md](DEDICATED-SERVER.md) for N-player routing, `XSHIP` and the
subsystems that stay on ships 1/2.
The handshake's start sector is Argon Prime; later navigation contains the actual
sector macro. Packets use the bounded ASCII version-1 envelope (768 bytes max).
Version 6 adds channels incompatible with a version-5 game adapter. Pair keys and
package metadata prevent accidentally joining another release pair.

`CoopSession` extends the existing economic session. Ship IDs 1 and 2 remain stable
when a player walks, dies, changes sectors or takes the other ship. `CREW` reports
controlled/occupied ship IDs. The relay grants exclusive pilot ownership; an epoch
increases on handoff and rejects delayed updates from the previous owner. Pending
native trades and cargo transactions block handoff. Empty ships retain their movement authority until
another pilot takes the controls. Stale movement stops after 0.75 seconds.

`NAV2` carries ship ID, authority epoch, sequence, sector macro, absolute pose,
shared-market dock ID, shot counter, source time and linear/angular velocity.
Legacy `NAV` remains readable. The 50 km starting bubble was removed.
Coordinates are bounded at 50,000,000 m per axis as corruption protection. Sector
components are resolved locally through MD; existing ships are warped between
sectors instead of recreated. NAV2 uses the same motion buffer as UMOVE and
applies changed poses no faster than every 15 ms. Dock transitions use
native docking actions; a remote undock also leaves the dock's coordinate frame.

FIRE2 is selected explicitly by the matched package's `fire2=true`. False or
absent means legacy NAV fire only; the protocols do not switch during a session.
If the required native group API is absent in FIRE2 mode, remote fire is disabled
with a diagnostic instead of silently changing protocols. The sender envelope is:

`FIRE2|1|lease|ship|pilotEpoch|sequence|sourceTime|primaryGroup|secondaryGroup|primaryShots|secondaryShots`

The relay validates ownership, epoch, bounded groups (1-4), finite monotonic time
and per-trigger counters. Playback uses the NAV2 source clock; the first counter
sample is a baseline, never historical replay. Pause boundaries freeze per-epoch
source cutoffs before an incoming NAV2 can reset its clock mapping. A new owner
gets a separate mapping; missing mappings wait for the first valid NAV baseline.
Queued pre-resume fire is discarded, including receive-before-frame ordering.

Counter deltas grant finite per-trigger native shot budgets. Native weapon-fired
callbacks consume them and stop the trigger at zero. Negative overrun debt is
retained so split counter deliveries cannot authorize the same salvo twice.
Pending credit is capped at 256 shots and expires after a fixed 350 ms window;
new packets cannot extend that old window. A group change stops the previous
group before activation and resets its credit/debt. Pause, silence, ship death
and authority changes cancel playback. MD group indices are 0-3; the wire/C API
indices are 1-4. Native capture uses the active group's weapon `.list`.

Native default-group calibration measured tap6 ->6, sustained78 ->78 and real
source capture6 ->remote6, including split2+4 and4+2 deliveries. This is trigger
and cadence evidence, not an exact projectile protocol. Custom group membership
and overlapping trigger membership are not replicated. Gimbal aim, projectile
inheritance and general NPC combat authority remain separate work.

The same native player faction owns both ships. Remote player figures on foot are
opt-in `WALK2` avatars, one per remote client, relative to a shared ship index or a
station identity; see [WALKING.md](WALKING.md). Spacesuit replicas use `BODY2`, with independent generation and sector.
Life handling guards lethal hull thresholds, ejects actual occupants, and keeps
native one-point suits protected until station teleportation finishes. Friendly
fire remains active. Suit weapons are not replicated; on-foot animation is the local
engine walk of the avatar, not the remote player's animation.
In co-op mode, only host snapshots confirm shared ship and target destruction.
Guest targets retain a minimum hull guard until that confirmation, so temporary
local damage can be reconciled without losing an otherwise living shared object.

## Economy

Both clients display one 200,000-credit starting treasury. Host X4's player faction
account is authoritative. Proxy ship accounts remain zero. Alpha/Beta have their
own station accounts, offers and stock. Six supported wares and native MapMenu
basket confirmation feed `COOPORDER`, which includes the actual controlled ship
ID and epoch. The relay verifies ownership and pilot presence, then routes
`COOP_EXEC` to host X4.

The host validates docking, stock, capacity, price and funds before native
`execute_trade`. Sales precede purchases. Actual cargo and account changes are
checked. A request ID is marked pending before engine mutation; retries return
the existing result. Partial or uncertain transfers require fresh worlds because
native operations cannot be safely rolled back. Guest economy writes are coalesced
when balances/cargo have not changed, but
periodic reconciliation still corrects local drift. Health refreshes at least
once a second and cargo/balances every two seconds.

The legacy pickup action maps the authenticated player's controlled ship to its
original fixture container. Ordinary freely jettisoned co-op cargo uses the native
magnet transaction path described above. NPC traders, other stations and the
wider universe economy remain independent.

## First shared mission

`X4LAN_Quest` creates Cooperative Trial on both clients. Host X4 counts verified
Energy Cell sales at Beta and destroyed shared targets. The trade AI receives a
mission-eligibility parameter from MD; after verifying the native cargo transfer,
it emits a local Lua event. The host adapter forwards this to the mission's MD
cue. Delivery request IDs are deduplicated. Quest state is published only after
host observation, never inferred from a guest's claimed kill or delivery.

Authenticated `QUEST` snapshots contain monotonic revision, stage, delivery count,
destroyed targets and paid flag. The relay rejects invalid/regressing states and
replays the current snapshot for late/resumed guests. Guest MD only mirrors its
mission interface. Host MD marks the reward paid before adding 25,000 credits
once; the guest receives that money through the ordinary economy channel. This
custom mission does not replicate arbitrary vanilla MD cues or story state.

## Reconnect, diagnostics and test isolation

Both endpoints use a 30-second timeout. Transport interruptions resume the same
lease, sequence and request history. Restarting either game or the relay requires
two fresh test worlds. Ambiguous trades are not replayed into a new host world.
The relay is trusted only on the chosen LAN/ZeroTier network; the mod does not add
its own encrypted transport.

The monitor shows player status, packet rates, game callback intervals, relay
process CPU/peak RSS, ship pilots, world/economy revisions and quest progress.
These distinguish relay workload from gaps in game updates, not rendering FPS or
network ping. Logs omit session keys. Their IP addresses are diagnostic data.

Profile 45841 is required for activation. Launchers protect links and existing
saves, back up config changes, disable autosave and apply Full HD exclusive Low
with a 60 FPS cap. Development harnesses run only in a backed-up test installation
and must not enter either release archive. Local synthetic-peer tests verify
engine behaviour on Linux; real Windows/Linux latency and endurance need playtests.

## Opt-in measurement (FRAMECOST, NETRTT)

Two diagnostic flags, false in the source config and in generated session configs,
supply numbers for the target-architecture "measure before porting" step. With both
false, `measure_clock.lua`, `net_rtt.lua`, `frame_profile.lua` and
`frame_profile_install.lua` return immediately and wrap nothing. Enable them by editing
the package's generated `extension/x4_lan_lab/ui/config.lua` on both players after
01-INSTALL (05-START-X4 copies it into the game); session recovery rewrites it to false.

The clock is QueryPerformanceCounter (Windows) or `clock_gettime(CLOCK_MONOTONIC)`
(Linux) through LuaJIT FFI, falling back to `GetCurRealTime`. `MEASURE_CLOCK` logs the
source, observed resolution and read cost; a clock that never advances is reported as
untimed rather than as zero cost.

`frame_profile=true` wraps the bubble frame root, network client update, each adapter's
`frame`/`receive` entry point, the seed gate, the shared motion buffer and the mod's
MD event callbacks. Times are exclusive (self) wall time, so chained adapters are not
counted twice; inline chain members (cargo and identity in universe, co-op docks in
coop) belong to their chain. Once per second:

`FRAMECOST v=1 window_s frames mod_ms_mean mod_ms_p95 mod_ms_max frame_interval_ms_mean
frame_interval_ms_p95 frame_interval_ms_max profiled_calls overhead_ms_est clock
sections=name:calls:total_ms:max_ms,...` (sorted by total) and
`FRAMECOST_MD v=1 window_s lua_to_md md_to_lua lua_to_md_per_s md_to_lua_per_s
sent=Screen.Control:n,... received=Event.Name:n,...`. MD execution itself cannot be
timed from Lua; its cost appears only in the game's frame interval.

`net_probe=true` sends `RTTPING|1|lease|seq|client_ms|echo_relay_ms|hold_ms` every 0.25 s
(`net_probe_interval`, 0.05-5 s). The relay answers `RTTPONG|1|lease|seq|client_ms|relay_ms`
at transport level; a ping never reaches the session, refreshes a lease or is forwarded.
Older relays drop RTTPING; older clients never send it. The client logs
`NETRTT v=1 side=client` every 5 s (pings, pongs, lost after 2 s, late, loss, RTT
min/median/p95/max, relay-to-client jitter p95, transport packet/byte rates, and
`rx_msgs`/`rx_types=KIND:per_s,...`, received messages by kind with bundles unpacked). The relay
writes `NETRTT v=1 side=relay` per peer to `logs/netprobe-*.log` (created at the first
ping) with RTT from the echoed relay time minus client hold, upstream jitter, sequence-gap
loss and per-peer rates; the status JSON and dashboard show the latest summary. Samples are
read once per UI frame and once per relay poll, so RTT includes up to one frame interval
plus the relay's 5 ms poll sleep. Jitter is p95 of |inter-arrival - source spacing|.

### Datagram bundles and NPC send cadence

A client sends `XBCAP|1|lease|1` with each 1 s HELLO (`net_bundle=false` opts out).
For such a peer, both relays pack each 20 Hz tick's snapshot packets into
`XB|1|lease|<len>:<item>...` datagrams of at most 768 bytes (the native transport
limit). An item is a packet `KIND|1|lease|rest` carried as `KIND|rest`; `client.lua`
restores the envelope and passes every message through the unchanged receive path.
Other packets, and every packet for a peer without `XBCAP`, are sent as before; older
relays ignore `XBCAP`, so old/new peers mix safely. The host sends `NHP` and `IDENT` only
when a value changes, otherwise with a new sequence every 1.5 s (`NHP`) or 1.2 s
(`IDENT`); the relays forward only sequences a client has not yet received, and repeat
at most two unchanged universe motion samples per tick for loss. Universe motion is
sent on divergence (see W01 above).
