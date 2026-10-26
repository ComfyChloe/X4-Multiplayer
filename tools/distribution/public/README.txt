X4 MULTIPLAYER - PUBLIC TEST 01
Version 6.0.1 | 11 October 2026 | Experimental two-player co-op

START HERE
This is an early public test, not a finished multiplayer mode. Two players run
their own X4 games; one is the authoritative HOST and one is the GUEST. The host
also runs the included server console. Either role can run on Windows or Linux.
The server can start before X4 and does not need the game installed just to run,
but a host player's running X4 game is required for shared gameplay.

Each platform package contains the mod, native networking library, server,
install/remove tools, portable Python 3.14.8 runtime and documentation. No separate
Python installation, pip command or runtime download is needed. Steam, a licensed
X4 installation, your GPU drivers and optional ZeroTier are not bundled.

REQUIREMENTS
- Exactly two players. Windows 11 x64 or native Linux x86-64 (Bazzite is the local
  test machine). Linux needs a glibc-based distribution; not ARM or Alpine/musl.
- Both games: X4 9.00 (611726), Steam build 23660954, with the same official DLC
  selection. The installer rejects a different build. Disable forced Proton on
  Linux; this mod's Linux native library is for the native Linux game.
- Disable additional third-party mods in the TEST PROFILE as well as checking
  the normal profile. Other mods previously caused a Windows loading crash.
- Both players need this same public test version and the same private session
  file. Older internal 6.0.0 packages cannot join this public test.

1. EXTRACT THE COMPLETE PACKAGE
Windows: right-click the ZIP, Extract All, then open the extracted folder.
Linux: extract the .tar.gz with your file manager, then open a terminal inside
the extracted folder. Use a writable local folder. Keep every file together.
Do not run anything from an archive viewer or a read-only USB drive.
Open START-HERE.html or this README. Optionally run 7-VERIFY-FILES first.

Windows commands below mean double-click the named .cmd file.
Linux commands mean: bash <name>.sh from the extracted package directory.
The command names and numbers are the same on both platforms.

2. HOST SETUP - WINDOWS OR LINUX
a. Close X4. If any internal or previous LAN mod is installed, remove it with
   that original package's 5-REMOVE-MOD first. Do not overwrite installed files.
b. Run 1-SETUP and choose H (Host). Enter THIS computer's LAN or ZeroTier IPv4.
   The host's local game will use 127.0.0.1 automatically. Do not enter localhost
   as the address you give your friend; do not use the old laptop's address.
c. Setup locates Steam/X4 and installs into isolated public profile 45842.
   If several installations exist, choose the intended native game folder.
d. Send the generated JOIN-SESSION.json privately to your one other player.
   It is the invitation to your session and contains a private access key.
   Every host creates a different session. Do not publish your configured folder.
e. Run 2-START-SERVER. Keep this console open; Ctrl+C stops the relay.
   On Windows, the bundled Python starts it directly. No Linux machine is needed
   to run the server. A second server from the same folder is refused.

3. GUEST SETUP - WINDOWS OR LINUX
a. Close X4 and remove any older/internal LAN mod with its original package.
b. Save the host's JOIN-SESSION.json somewhere accessible.
c. Run 1-SETUP and choose G (Guest). Enter the full path to that session file.
   Setup imports the host address/key and installs the guest role.
d. With the host server running, run 2-TEST-CONNECTION. Expect CONNECTION OK.
   The guest does not start a second server. The probe does not occupy a slot.

Two Windows PCs or two Linux PCs can also choose opposite roles. These pairings,
Windows game-host authority and real cross-platform endurance need public test
feedback; see RELEASE-CHECKS.txt for the exact evidence available at release.

4. NETWORK AND FIREWALL
Use the host's current LAN IPv4 on the same network, or join and authorise both
computers in the same ZeroTier network and use the host's Managed IPv4. Remove
any /24 suffix. The server listens on UDP 45842 for PUBLIC tests (not 45841).
The mod is intended for LAN/ZeroTier with a trusted co-player. Its own protocol
is not encrypted. Direct public-internet port forwarding is outside this test.

Windows host: if Windows asks about network access for the bundled python.exe,
allow your intended private network. If the probe still fails, check the
firewall rules for that executable and inbound UDP 45842 on the actual interface.
Do not turn off the whole firewall. Managed organisational rules may need an admin.
Linux host using firewalld: inspect the interface's active/default zone, then if
needed open UDP 45842 temporarily in that zone:
  firewall-cmd --get-active-zones
  firewall-cmd --get-default-zone
  sudo firewall-cmd --zone=YOUR_ZONE --add-port=45842/udp --timeout=2h
If you use a different firewall, apply its equivalent narrow rule.

5. START BOTH GAMES
Keep the server open. On each PC run 3-START-X4-TEST. Linux needs a second terminal.
Both start a NEW creative world in PUBLIC profile 45842. Never load an existing
save, quicksave or manually save during this test. Autosave is disabled.
Use the default creative start, matching small ship/weapons in Argon Prime.
Wait in both pilot seats with normal flight controls for fixture creation.

Expect two LAN Player ships, two LAN Targets, two assigned cargo containers and
LAN Trading Station Alpha/Beta. A donor freighter also belongs to the fixture.
Confirm both P1 and P2 receive updates in the server console. One spawned ship
or CONNECTION OK alone does not establish a working two-game session.

Public profile settings: 1920x1080 fullscreen with borderless disabled, Low,
60 FPS cap, autosave off. Changed test configs are backed up. Normal profiles
and saves are not edited. Linux display presentation still depends on the OS.
If the isolated profile contains saves or unsafe links, setup/start refuses it.
Windows OneDrive Documents handling is retained; do not bypass profile guards.

6. WHAT TO TEST
Follow TEST-PLAN.txt in order. In particular:
- Take the other player's vacant cockpit and undock, in both directions.
- Travel beyond mutual sensor coverage and through a gate; verify updates after
  rejoining and in the destination sector without returning to the spawn point.
- Use 6-COLLECT-CONTAINER within 250 m of each ship's assigned test container.
  The normal cargo magnet and arbitrary containers are not integrated.
- Trade through X4's ordinary Trade menu at Alpha/Beta. Shared funds start at
  200,000 credits. Six supported wares and mixed baskets are available.
- Cooperative Trial: sell a combined 20 Energy Cells to Beta, then destroy both
  LAN Targets. One 25,000-credit reward goes into the common treasury.
- Test a 40-second guest network interruption, then friendly fire, ejection,
  suit death and station respawn. Run destructive checks last.
Walking players are not replicated. Other NPC markets, universe simulation,
ordinary missions, construction and save/load are not shared.

7. RECONNECT AND RESTART RULES
The timeout is 30 seconds. Restore an interrupted network while keeping both
games and the SAME relay running; reconnect should retain the session.
Restarting either game or the server requires new creative worlds on BOTH PCs.
Reuse your existing session file and the same packages for those fresh worlds.
Do not create a new order if a trade is pending or uncertain. Preserve logs;
for partial/uncertain trades restart both worlds and the relay.
If 6-COLLECT-CONTAINER times out, retry that same command; it retains its ID.

8. REPORT A PROBLEM
Before restarting, run 4-DIAGNOSTICS on BOTH PCs. Keep each logs/ folder and fill
out TEST-REPORT-TEMPLATE.txt. The host's logs/server-*.jsonl and both full engine
logs matter. Logs may contain paths and IPs; review before sharing. The diagnostic
collector redacts the current session key and never includes JOIN-SESSION.json.
No reports are uploaded automatically. Send your report to the organiser who
gave you these packages; no public bug-report address is configured here.

The console shows roles, update rates, ship pilots, shared world/economy/mission
state, relay CPU and peak memory. Game callbacks/s are NOT rendered FPS or ping.
Record actual rendered FPS separately, especially if only one machine is slow.

9. STOP, REMOVE OR SWITCH CHANNELS
Close both X4 games, then press Ctrl+C in the original server console.
Run 5-REMOVE-MOD on each installed package. It archives only that package's owned
mod and leaves the public profile and saves in place. Restore your normal mod
selection before loading a normal save through Steam.
Internal and public profiles/session identities/ports are separate, but the game
extension directory is shared. Only one channel may be installed at a time.
To change roles, partner/session or host IP, remove the mod, extract a FRESH copy
of the public download, create a new host session and share its new invitation.

COMMAND REFERENCE
1-SETUP               Configure HOST/GUEST and install the game mod.
2-START-SERVER        HOST: open the live server console; leave it running.
2-TEST-CONNECTION     Check the configured session without occupying a slot.
3-START-X4-TEST       Launch a fresh isolated creative test world.
4-DIAGNOSTICS         Collect local logs and version/role information.
5-REMOVE-MOD          Archive the owned installed mod; retain profiles/saves.
6-COLLECT-CONTAINER   Request the current ship's assigned container pickup.
7-VERIFY-FILES        Verify shipped hashes (configured session files excluded).

All scripts use the bundled runtime. Advanced: add --server-only to 1-SETUP to
configure the package without installing X4; rerun ordinary 1-SETUP to install
later. A server alone does not simulate the X4 universe. No account, subscription
or separate Python setup is required by these launchers.
