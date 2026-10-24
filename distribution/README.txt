X4 Multiplayer {{VERSION}} — INTERNAL TEST
24 October 2026 | Windows/Linux host or guest | Two players per session
X4 9.00, Steam build23660954 | Matching official DLC including Timelines

Both packages include the server and a private Python runtime. No Python install
or download is needed. Open 00-START-HERE.html for detailed instructions.

WHAT CHANGED
This candidate transmits damage from actual stock-weapon impacts and renders the
other pilot's observed projectiles, preserving native aim, spread and misses.
It reserves the same saved station pad in both games and fences stale movement
after landing or changing pilots. A fixed completed-story preset prevents the
main story plots from starting during technical tests. It changes some campaign
outcomes and grants research/HQ progression; it is not a normal campaign preset.

These corrections have local Linux engine and real-relay/synthetic-peer evidence.
The Windows/Linux human pair still needs the checks in NEXT-TEST.txt. Supported
combat scope: the two supplied player Shuttles with uniform, unmodified Shuttle
plasma or generic pulse Mk1/Mk2 guns. Other weapons, NPC/station combat, short-range
shots that end before UI capture, and changing pilot during live fire are not
qualified. Do not treat a visible replica as complete shared NPC gameplay.

Normal profiles stay read-only. Personal resolution, quality, sound and bindings
are imported into the isolated test profile, which uses borderless mode.
Existing radar, ordinary cargo, movement, station/asteroid alignment, Alt+Tab and
Cooperative Trial human results remain valid unless a new regression appears.
A shared starting save does not make ongoing station economy or all NPC decisions
host-authoritative. Saving/resuming a played multiplayer campaign is not ready.

HOST
1. Close X4 and the old server. Remove the previous mod using its ORIGINAL
   package's REMOVE-MOD action (09 in7.3.1;5 in7.3.0). Extract into a new folder.
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
The launcher includes -nocputhrottle. Alt+Tab passed the previous host/guest human test. Personal teleportation is unlocked after Shuttle setup;
use the normal map/context menu and normal destination/ownership requirements.
The technical story preset is fixed for this release; there is no choice menu yet.

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
