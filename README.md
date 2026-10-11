# X4 LAN Lab — internal version 7.0.0 (W01)

For development on either PC or the Fedora laptop, start with
[DEVELOPMENT.txt](DEVELOPMENT.txt) and [CONTINUE-HERE.txt](CONTINUE-HERE.txt).

W01 adds original NPC sector scene replication: dynamic host object identities,
complete acknowledged baselines, movement and guest replica lifecycle. Read
[the focused internal test](distribution/WORLD-SYNC-TEST.txt) before using it.
Public Test 01 remains withheld. Ordinary cargo/magnet pickup, NPC interactions,
station economy and vanilla missions still prevent a fully shared universe.
See [the shared-world requirements](WORLD-SYNC-REQUIREMENTS.txt).

Experimental two-player co-op for X4 Foundations: native Linux host and Windows 11
guest, including ZeroTier. X4 9.00 (611726), Steam build 23660954.

The existing co-op fixture separates ships from player slots, carries sector-aware movement,
arbitrates cockpit handoff, shares one treasury and introduces Cooperative Trial.
Friendly fire, automatic reconnect, emergency ejection and station respawn remain.
The user's real 5.1.0 playtest confirmed ejection and respawn; this release's exact
native and automated evidence is in the release checks. It does not synchronize
the entire universe or all vanilla missions.

## Install and play

Read [INSTALL.html](distribution/INSTALL.html). Replace both full packages and the
relay; version 7 is incompatible with 6.x and older builds. Start two new creative worlds in the
isolated profile `45841`. Disable additional third-party mods for testing and keep
matching DLC. Linux needs Python 3.10+; Windows does not need Python. The OneDrive
profile fix remains included.

Build a fresh matched pair with `python3 distribution/build_packages.py /path/to/new-folder`.
The generated pair has a fresh shared key. Identical version numbers alone do not
make packages from different builds compatible. `VERSION.txt` is canonical; see
[VERSIONING.txt](VERSIONING.txt).

Launchers apply 1920×1080 exclusive fullscreen, Low graphics and a 60 FPS cap only
to the test profile, backing up changed settings. Autosave is disabled. Never load,
quicksave or manually save a normal game in the test. Existing saves and unknown
mod installations are protected from replacement.

## Shared scope

Two stable ships, two targets, two containers and two trading stations start in
Argon Prime. Either player can pilot either ship after the previous pilot leaves
its controls. One pilot per ship; cargo stays with the ship. Movement includes
sector macros and absolute positions, so the old ±50 km cube no longer applies.
A 50,000 km per-axis sanity limit rejects corrupt coordinates; it is not a
sensor-visibility boundary. W01 mirrors host NPC scenes in the guest sector;
NPC interactions and the surrounding economy still run independently. Travel drive/highways and passenger flight need further testing.

Both players belong to the player faction and share 200,000 initial credits.
X4's normal trade menu works at Alpha/Beta for six wares and mixed baskets.
The host game executes all shared trades and mirrors balances and cargo. Pickup
uses `6-COLLECT-CONTAINER` within 250 m of the currently controlled ship's test
container. Arbitrary containers and the normal cargo magnet are not integrated.

Cooperative Trial appears in the mission interface: sell 20 Energy Cells to Beta
together, then destroy both LAN Targets. Both pilots contribute to one mission.
The host awards 25,000 credits once into the shared treasury. This custom mission
is the first quest integration; ordinary plots and mission offers are not shared.

Friendly fire remains enabled. A destroyed occupied ship ejects its occupants;
a suit combat hit triggers station respawn at an accessible non-hostile station
in the current sector. Destroyed ships/cargo are not replaced. Walking figures
remain disabled; spacesuit replicas remain enabled. Suit weapons are not replicated.

A 30-second timeout leads to automatic reconnect using the same lease and request
history. Restarting either game or the relay requires both new worlds. A partial
or uncertain native trade must not be resubmitted as a new transaction.

## Performance and diagnostics

The console reports connections, received updates, game callback timing, relay CPU
and peak memory, ship pilots, world/economy revisions and shared mission progress.
Callback/update rates are not rendering FPS or ping. CPU is a percentage of one
logical core. JSON Lines logs and atomic status snapshots omit session keys.

Unchanged remote poses and guest cargo snapshots are coalesced, with periodic
authoritative reconciliation. This reduces avoidable work, but no general FPS improvement is established.
The version 6 controlled relay benchmark measured 4.27% of one core and 17.6 MiB
peak memory over 20 seconds with two synthetic peers and no send errors. This
measures the relay, including its dashboard output, but excludes X4/mod/driver costs.
The Linux host runs the authoritative game as well as the small Python relay.

## Evidence and development

- [6.0.0 release checks](reports/release-6.0.0-checks.txt).
- [Current automated tests](reports/automated-6.0.0-tests.txt).
- [Earlier native trade tests](TRADE_TESTS.txt).
- [Architecture](ARCHITECTURE.md), [research sources](RESEARCH.md).

`x4lan/` contains relay sessions and the console. `extension/x4_lan_lab/` contains
Lua, MD and AI adapters; `distribution/` contains installers and packaging.
Run `python3 -m unittest discover -s tests -v`. LuaJIT tests need `lupa`; local UDP
tests need socket access. Engine doubles do not establish native compatibility.

Regenerate the base files in order: `python3 dev/build_bubble_md.py`, then
`python3 dev/build_trade_md.py`. Cooperative, life and quest MD scripts are saved
directly. Native libraries are unchanged. No game-memory offsets or copied assets.
English documentation and interfaces are shipped; historical game logs retain the
engine's own wording. Development harnesses must never enter distribution packages.
