# Internal W01 architecture — 7.0.0

The new scene transport and engine adapter are original code. Public Test 01 is
withheld until the broader [shared-universe requirements](WORLD-SYNC-REQUIREMENTS.txt)
are met. W01 synchronizes NPC scene visibility and motion, not NPC interactions,
ordinary cargo or the full economy.

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

`UPOS` carries lossy, sequenced motion only for known committed IDs. Host and
relay each send at most 12 such records per tick. The guest interpolates and
updates at most 24 replicas per frame with a fair cursor; stale motion freezes
until a newer baseline or pose arrives. Rates are shared across a sector, so this
is not a promise of 20 updates/s for every ship. Baseline motion recovers after
lost pose packets. Transport receipt and engine application are separate: engine
changes wait for an unpaused frame; a transport acknowledgement alone is not an
engine success report.

Guest replica mode removes independent NPCs only in its current sector and only
following a complete, recent baseline. It creates host macro/owner replicas via
engine APIs, disables their AI and applies host poses. Local player-owned ships,
co-op fixtures and player occupants are protected. A replica occupied by the
player stops the adapter instead of removing the player. This mode is only for
disposable worlds: deleting native NPCs can invalidate local vanilla missions.
The adapter does not yet suppress independent stations, production, missions or
all consequences of the guest simulation. Observe mode receives baselines and
logs counts without changing game objects.

The protocol accepts reserved drop records, but the native drop adapter is not
implemented and rejects them. No ordinary cargo pickup fix is claimed. Next work
must establish cargo transactions, equipment/damage/ownership authority, station
state, native object generation hooks and stronger guest simulation control.

## Existing two-ship cooperative fixture

The native Linux X4 game is the authority for shared damage, cargo, treasury and
mission outcomes. The Python UDP relay validates sessions, routes snapshots,
deduplicates actions and arbitrates which pilot may publish each ship's movement.
It does not simulate the universe. Windows runs its own X4 universe and applies
replicas of the explicitly shared objects.

## Session and stable objects

Two player slots authenticate with a matched package key and build/session IDs.
The handshake's start sector is Argon Prime; later navigation contains the actual
sector macro. Packets use the bounded ASCII version-1 envelope (768 bytes max).
Version 6 adds channels incompatible with a version-5 game adapter. Pair keys and
package metadata prevent accidentally joining another release pair.

`CoopSession` extends the existing economic session. Ship IDs 1 and 2 remain stable
when a player walks, dies, changes sectors or takes the other ship. `CREW` reports
controlled/occupied ship IDs. The relay grants exclusive pilot ownership; an epoch
increases on handoff and rejects delayed updates from the previous owner. Pending
native trades block handoff. Empty ships retain their movement authority until
another pilot takes the controls. Stale movement stops after 0.75 seconds.

`NAV` carries ship ID, authority epoch, sequence, sector macro, absolute pose,
shared-market dock ID and shot counter. The 50 km starting bubble was removed.
Coordinates are bounded at 50,000,000 m per axis as corruption protection. Sector
components are resolved locally through MD; existing ships are warped between
sectors instead of recreated. Interpolation uses a 100 ms buffer. Engine pose
writes are limited to 20 Hz and skipped for unchanged poses. Dock transitions use
native docking actions; a remote undock also leaves the dock's coordinate frame.

The same native player faction owns both ships. Remote player figures on foot are
disabled. Spacesuit replicas use `BODY2`, with independent generation and sector.
Life handling guards lethal hull thresholds, ejects actual occupants, and keeps
native one-point suits protected until station teleportation finishes. Friendly
fire remains active. Suit weapons and walking animations are not replicated.
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

Pickup uses the existing action launcher and maps the authenticated player's
current controlled ship to its matching test container. Arbitrary cargo magnets,
NPC traders, other stations and wider universe economy remain independent.

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
