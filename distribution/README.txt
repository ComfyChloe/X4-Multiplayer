X4 Multiplayer {{VERSION}} — INTERNAL TEST
23 October 2026 | Windows/Linux host or guest | Two players per session
X4 9.00, Steam build23660954 | Matching official DLC including Timelines

Both packages include the server and a private Python runtime. No Python install
or download is needed. Open 00-START-HERE.html for detailed instructions.

WHAT CHANGED
Station and asteroid alignment passed the 7.3.0 human test. The same shared
starting-save approach is retained. This update adds an invitation file picker,
unique launcher numbers, borderless test-profile defaults for Alt+Tab, and all
four PERSONAL teleportation research levels (including unlimited range).
Your normal profile remains read-only. Resolution, quality, sound and custom
bindings are imported; only the test display mode changes to borderless.
Weapon/damage changes and their measured scope are listed in reports/RELEASE-CHECKS.txt.

Ordinary station docking remains incomplete: the other player's native landing
pad occupancy/barriers are not shared, so boarding their docked ship may fail
and NPC traffic may overlap it. Do not use station boarding as a supported flow.
A shared starting save does not make ongoing station economy or all NPC decisions
host-authoritative. Saving/resuming a played multiplayer campaign is not ready.
Existing radar, ordinary cargo, smooth movement and Cooperative Trial evidence
is retained. These do not need a repeat test unless a regression appears.

HOST
1. Close X4 and the old server. Remove the previous mod using its ORIGINAL
   package's 5-REMOVE-MOD (7.3.0 and earlier). Extract this complete archive into a new folder.
2. Run 01-INSTALL, choose H, enter your reachable LAN/ZeroTier IPv4 address.
   Choose your normal settings profile if asked.
3. Run 02-PREPARE-WORLD-HOST once. X4 opens, creates the shared world, saves and closes
   automatically. Wait for "Shared world ready" in the launcher. Do not play or
   load a different save during preparation.
4. Send JOIN-SESSION.json privately to your guest. Run 03-START-SERVER-HOST and keep
   its console open. The host runs BOTH this server and X4.
5. Run 05-START-X4. The saved world loads automatically; wait for your Shuttle.

GUEST
1. Remove the previous mod using its original package and extract the complete
   new archive. Receive the host's JOIN-SESSION.json and keep it in this folder.
2. Run 01-INSTALL, choose G, select that invitation in the file dialog if it is not already beside the starter.
3. Run 04-TEST-CONNECTION, then 05-START-X4. The launcher downloads and checks
   the shared world, then X4 loads it automatically. Do not choose another save.

The numbers identify actions; guests skip HOST-only actions 02 and 03.
Windows: double-click the numbered .cmd files. Linux: run the matching .sh files
from a terminal, for example bash 01-INSTALL.sh. Allow UDP45841 and TCP45844 to the
host on your test network. A successful UDP check alone does not test the save
transfer. No router forwarding is needed with a working ZeroTier connection.

BACKGROUND / TELEPORTATION
Use Alt+Tab to another application while X4 stays open in borderless mode. Keep
the server console running. Do not explicitly pause X4 or minimize it with the
Windows desktop button; true minimization can suspend native rendering/UI ticks.
The launcher includes -nocputhrottle. Background behavior is still part of the
Windows human check. Personal teleportation is unlocked after Shuttle setup;
use the normal map/context menu and normal destination/ownership requirements.
Station-HQ relocation research and campaign progression are separate features.

RESTART / STOP
Every retry requires BOTH games AND the relay to stop first. Restart the relay
and both games. They reload the same STARTING world; previous gameplay progress
is not resumed. To create a different world/session, use a fresh extracted pair
and a new invitation. 02-PREPARE-WORLD-HOST never regenerates an existing world.

ONLY the reserved, recorded save_010 belongs to the launcher. Other or changed
saves are preserved and cause a refusal. Never save a normal campaign in45841.
09-REMOVE-MOD removes its byte-matching reserved copy and archives the owned mod;
the package keeps the immutable world bundle. Close X4 and the relay first.

The setup does not change Workshop selections or subscribe/unsubscribe to mods.
The withdrawn7.2 Fix-1 helper is not included. A separate profile does not by
itself isolate the Steam account. If content differs, stop and compare the two
setups; no automatic mod-selection rewrite is performed.

NEXT-TEST.txt describes tonight's focused test. Run06-DIAGNOSTICS on BOTH PCs before
restarting after a failure. Keep logs, role/OS, object code and approximate time.
Do not share JOIN-SESSION.json, session.json or ui/config.lua publicly.
This remains an INTERNAL test, with two simultaneous players per session.
