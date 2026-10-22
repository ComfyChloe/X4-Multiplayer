X4 LAN Lab {{VERSION}} — INTERNAL TEST — Windows/Linux host or guest
22 October 2026 | Two players per session | X4 9.00, Steam build 23660954

Open INSTALL.html, then follow NEXT-TEST.txt for the focused human test.
Both packages include the relay server and a private portable Python runtime.
No separate Python installation or download is required at setup time.

NEW IN 7.2.0
- Either Windows or Linux can host or join. The host creates a private session
  invitation; the guest imports it. Each test group gets separate credentials.
- Shared Shuttles use native creation that correctly initializes their sensors.
  Nearby ship/station radar and HUD checks passed in separate Linux role tests.
- Setup imports your normal profile display, graphics, audio and control settings.
  Launching the test does not reset your resolution, quality or key bindings.
- Existing cargo/magnet, smooth movement, identities and Cooperative Trial remain.

QUICK START
1. Both: extract the COMPLETE archive for your OS into a new folder. Close X4
   and the previous relay. Remove the old mod using its ORIGINAL package's
   5-REMOVE-MOD command. Keep the old archives for rollback.
2. Host: run 1-INSTALL, select H, enter the host's LAN or ZeroTier IPv4.
   Send the generated JOIN-SESSION.json privately to the other player.
3. Guest: run 1-INSTALL, select G, and enter the path to JOIN-SESSION.json.
   Both need this exact version; use the invitation from the intended host.
   Setup copies config.xml and inputmap*.xml from your normal game profile. If
   several profiles exist, choose the one you use. No saves, mod selection or
   uidata are copied. Existing test settings are backed up. Autosave and protected
   UI mode stay disabled because this internal test requires those settings.
   Later edits in the test profile are retained. To import again with X4 closed,
   use 9-IMPORT-SETTINGS; --settings-profile selects a specific source folder,
   and --keep-test-settings keeps the current test preferences.
4. Host: run 2-START-SERVER and keep its console open. The host must ALSO run X4.
   Guest: run 2-TEST-CONNECTION. Then both run 3-START-X4-TEST.
5. After testing, run 4-DIAGNOSTICS on both PCs and keep the host server logs.

Windows commands are .cmd files; double-click them. Linux commands are .sh files;
run them in a terminal, for example: bash 1-INSTALL.sh
All commands use the included runtime. Ctrl+C in the host console stops the relay.
7-VERIFY-PACKAGE checks the files against the packaged hash inventory.

The server is a relay, not a headless X4 universe simulator. One player hosts the
relay AND plays X4 as p1; the other joins as p2. Windows/Windows, Linux/Windows,
Windows/Linux and Linux/Linux role selection are supported by the launcher.
A new joint native Windows/Linux gameplay test is still required.

SCOPE AND LIMITATIONS
This release still launches SEPARATE fresh creative worlds in profile 45841.
Experimental host-save transfer and shared attachment branches are not included.
Ordinary stations, NPC orders/damage, equipment, economy, vanilla missions and
persistent save/load are not synchronized. Ingame chat is not implemented.
The player-faction Mercury freighter is a protected cargo service fixture,
not a controllable shared fleet ship. Use the two LAN Targets for damage checks.

Both players retain an Xperimental Shuttle with six guns, full software including
Docking Computer Mk2, Mk3 combat thrusters and a shared treasury. Timelines must
be enabled on both PCs. The custom Cooperative Trial remains shared. Ordinary
cargo jettison/magnet pickup works for the shared ships; general NPC loot and
multi-ware containers remain outside acceptance. Motion/weapon/hit equivalence
and NPC PiP artifacts have remaining limits described in RELEASE-CHECKS.txt.

Use matching official DLC, disable Ventures for this test, and disable other
third-party mods. Never load or save a normal game with this experimental mod.
If either game or relay is restarted, both players must start fresh test worlds.
For another group/session, use a fresh extracted package and a new host invitation.

This is INTERNAL only. Do not publish this package or its generated session files.
Detailed acceptance evidence: RELEASE-CHECKS.txt and AUTOMATED-TESTS.txt.
