X4 LAN Lab {{VERSION}} — two-player co-op, Linux host + Windows 11 guest
20 October 2026 | X4 9.00 (611726), Steam build 23660954

Open INSTALL.html for installation, ZeroTier and the complete playtest sequence.
Read RELEASE-CHECKS.txt for verified results and remaining limitations.

UPGRADE BOTH COMPUTERS
Use the two matching archives from this release folder. Extract the complete ZIPs
before running anything. Close both games, stop the old server, remove each old
mod with its original package's numbered 5 command, then install the new pair.
Use this exact release on both computers, including its new relay. Do not mix
with the original 7.0 or 7.1.0 pairs. Start two new creative test worlds.
Disable other third-party mods for testing. Keep matching official DLC, including
X4: Timelines for the shared Xperimental Shuttles.

Linux: open a terminal in this folder.
  bash 1-INSTALL.sh
  bash 2-START-SERVER.sh        Keep this console on your second monitor.
  bash 3-START-X4-TEST.sh       Use another terminal; keep the server running.
Windows: double-click 1-INSTALL.cmd, 2-TEST-CONNECTION.cmd, 3-START-X4-TEST.cmd.
Enter the LINUX HOST's LAN or ZeroTier IPv4, not the guest's address.

This INTERNAL follow-up mirrors host NPC sector scenes, adds ordinary co-op ship
cargo/magnet transactions, shared names/display codes/radar contacts and smoother
source-clock movement. Read WORLD-SYNC-TEST.txt for the focused playtest and
RELEASE-CHECKS.txt for actual acceptance evidence. Public Test 01 remains withheld.

This patch also corrects native drift when a remote pose stays unchanged and
keeps the two cooperative ships' native linear velocity consistent with their
rendered motion. Packet-loss prediction freezes with zero velocity; lifecycle
guards release overrides on docking, control or connection changes. Initial
physical-velocity support is limited to free ships in aligned zone/sector/cluster
spaces. A separate projectile experiment failed an orientation control; precise
projectile/aim/hit equivalence and simultaneous two-native gameplay remain open.

The console shows connections, received updates, game callback timing, relay CPU
and RAM, ship pilots, economy and shared mission progress. Callback/update rates
are diagnostics, not measured game FPS or ping. Ctrl+C stops the original server;
in an attached viewer it closes only that viewer.
The timeout is 30 seconds; network interruptions reconnect automatically while
the same relay and both games remain running. Restarting either game or the
relay requires fresh worlds on both computers. Uncertain trades are not replayed
into a new world.

TRUE CO-OP FLEET
Both players receive the same Xperimental Shuttle with six guns, a shield,
two engines, combat thrusters and installed software including Docking Computer
Mk2. The unused missile connection has no missile storage. Both players share
a faction and one 200,000-credit treasury. Either player can
control either surviving shared ship, once the other pilot leaves the controls.
Only one pilot can control a ship at a time. Cargo stays with the ship. Friendly
fire stays enabled. Passengers and player figures on foot remain experimental;
walking avatars are disabled. Spacesuit replicas remain enabled.

The former +/-50 km movement cube is removed. Shared ships carry their sector
identity and absolute position and remain the same ships across sector changes.
NPC ship visibility, owner, macro, position and scene lifecycle now follow the
host in the guest sector. This does not synchronize the whole universe: NPC
interactions, other stations, story state and background economics remain local. Only Alpha/Beta
are shared markets. Avoid SETA; travel-drive/highway fidelity needs further testing.

Trade through X4's ordinary menu at LAN Trading Station Alpha or Beta, using the
shared ship you currently pilot. Six wares, mixed baskets and variable quantities
are supported. The Linux game checks and executes each transfer.
6-COLLECT-CONTAINER remains the pickup action for the two original fixture
containers within 250 m. Separately, cargo freely jettisoned from either shared
ship is replicated and collected with the normal magnet. The host commits the
inventory change once and reconciles guest prediction. Arbitrary NPC loot,
multi-ware crates and unrestricted cargo categories are not accepted yet.

COOPERATIVE TRIAL
The mission appears automatically after market setup. Sell a combined total of
20 Energy Cells at Beta using either shared ship; then destroy both LAN Targets.
Both players see the same progress. The shared treasury receives 25,000 credits
once. Buying/selling and combat use the normal mechanisms described above.
This first custom co-op quest does not make every vanilla mission cooperative.

Ship destruction ejects its occupants into suits. Suit death returns the player
to the nearest accessible, non-hostile station in the current sector. Destroyed
ships and cargo are not replaced. Spacesuit weapons are not replicated. Complete
trading and the mission before testing friendly-fire destruction.

TEST PROFILE AND GRAPHICS
Use only new creative sandboxes in profile 45841. Do not load existing saves,
quicksave or manually save. Autosave is disabled. Launchers apply 1920x1080
exclusive fullscreen, Low graphics and a 60 FPS cap only to this profile, with
configuration backups. Linux uses High textures as a measured workaround for
startup shader failures; Windows retains its previously tested Low textures.
Both retain shaderquality 2. Higher textures can use more graphics memory; this
is not a general performance or NPC PiP fix. Windows OneDrive Documents support
remains included.
4-DIAGNOSTICS collects logs; 5-REMOVE-MOD archives this installed mod.
General performance and real cross-platform endurance still need playtesting.
Do not publicly share session.json or ui/config.lua; they contain the session key.
