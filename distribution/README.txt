X4 Multiplayer {{VERSION}} — INTERNAL TEST
23 October 2026 | Windows/Linux host or guest | Two players per session
X4 9.00, Steam build23660954 | Matching official DLC including Timelines

Both packages include the server and a private Python runtime. No Python install
or download is needed. Open INSTALL.html for detailed instructions.

WHAT CHANGED
The host now creates one native starting save. Both games load that exact world;
the guest downloads it automatically from the host before launching X4. This is
intended to align ordinary stations, gates and the initial universe layout.
A shared starting save does not make the ongoing economy or all NPC decisions
host-authoritative. Saving/resuming a played multiplayer campaign is not ready.

Existing radar, ordinary cargo and the Cooperative Trial passed the previous
human test. Smooth ship movement is retained. Weapons/damage have no new fix in
this release. Graphics, sound and custom bindings are imported from your normal
profile; launch does not reset them. Your normal profile remains read-only.

HOST
1. Close X4 and the old server. Remove the previous mod using its ORIGINAL
   package's 5-REMOVE-MOD. Extract this complete archive into a new folder.
2. Run 1-INSTALL, choose H, enter your reachable LAN/ZeroTier IPv4 address.
   Choose your normal settings profile if asked.
3. Run 2-PREPARE-WORLD once. X4 opens, creates the shared world, saves and closes
   automatically. Wait for "Shared world ready" in the launcher. Do not play or
   load a different save during preparation.
4. Send JOIN-SESSION.json privately to your guest. Run 2-START-SERVER and keep
   its console open. The host runs BOTH this server and X4.
5. Run 3-START-X4-TEST. The saved world loads automatically; wait for your Shuttle.

GUEST
1. Remove the previous mod using its original package and extract the complete
   new archive. Receive the host's JOIN-SESSION.json and keep it in this folder.
2. Run 1-INSTALL, choose G, enter the path to that invitation when asked.
3. Run 2-TEST-CONNECTION, then 3-START-X4-TEST. The launcher downloads and checks
   the shared world, then X4 loads it automatically. Do not choose another save.

Windows: double-click the numbered .cmd files. Linux: run the matching .sh files
from a terminal, for example bash 1-INSTALL.sh. Allow UDP45841 and TCP45844 to the
host on your test network. A successful UDP check alone does not test the save
transfer. No router forwarding is needed with a working ZeroTier connection.

RESTART / STOP
Every retry requires BOTH games AND the relay to stop first. Restart the relay
and both games. They reload the same STARTING world; previous gameplay progress
is not resumed. To create a different world/session, use a fresh extracted pair
and a new invitation. 2-PREPARE-WORLD never regenerates an existing world.

ONLY the reserved, recorded save_010 belongs to the launcher. Other or changed
saves are preserved and cause a refusal. Never save a normal campaign in45841.
5-REMOVE-MOD removes its byte-matching reserved copy and archives the owned mod;
the package keeps the immutable world bundle. Close X4 and the relay first.

The setup does not change Workshop selections or subscribe/unsubscribe to mods.
The withdrawn7.2 Fix-1 helper is not included. A separate profile does not by
itself isolate the Steam account. If content differs, stop and compare the two
setups; no automatic mod-selection rewrite is performed.

NEXT-TEST.txt describes tonight's focused test. Run4-DIAGNOSTICS on BOTH PCs before
restarting after a failure. Keep logs, role/OS, object code and approximate time.
Do not share JOIN-SESSION.json, session.json or ui/config.lua publicly.
This remains an INTERNAL test, with two simultaneous players per session.
