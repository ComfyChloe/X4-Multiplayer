# Research sources and evidence

Original research: 9 October 2026. References describe their authors' work; they
are not substitutes for running this mod on the exact target game/platform.

1. [SirNukes Mod Support APIs](https://github.com/bvbohnen/x4-projects/tree/master/extensions/sn_mod_support_apis)
   demonstrates Lua/UI integration and bidirectional external-program communication
   through named pipes. The original pipe implementation is Windows-specific.
2. [Linux port and author instructions](https://github.com/bekopharm/x4-projects/wiki/Quick-manual):
   Unix-socket/LuaSocket 5.1 approach, documentation for X4 8.x. Inspected branch
   `linux-compat`, commit `4d792d593bba6aebd5044bcb91eb8071f2e8f539`. Existing Linux work
   does not guarantee X4 9.00 compatibility. Our direct FFI socket attempt was
   rejected in-game; the current mod uses its own nonblocking UDP Lua C module.
3. [X4 Simpit](https://github.com/bekopharm/x4-simpit), inspected commit
   `683f975f1e0c14f47a64d9165334a4013ad121a3`: actual ship telemetry to an external
   application, with Linux/Windows notes. Telemetry is a useful data-path precedent,
   not a multiplayer implementation by itself.
4. [X4Native](https://github.com/eg3r/X4Native), inspected commit
   `fc4b8e26d74365ca332c3b0749eb9bbe167c76a1`: native Windows x64 C++ framework,
   exported APIs and research notes for X4 9.x. Our mod does not embed the framework,
   assume a Linux port or use its fixed memory addresses.
5. [State Mutation](https://github.com/eg3r/X4Native/blob/fc4b8e26d74365ca332c3b0749eb9bbe167c76a1/docs/rev/STATE_MUTATION.md):
   section 8 documents GetObjectPositionInSector (radians) and SetObjectSectorPos
   (degrees). Our conversion was subsequently verified by Linux game readback.
6. [API signatures](https://github.com/eg3r/X4Native/blob/fc4b8e26d74365ca332c3b0749eb9bbe167c76a1/sdk/x4_game_func_list.inc)
   and [types](https://github.com/eg3r/X4Native/blob/fc4b8e26d74365ca332c3b0749eb9bbe167c76a1/sdk/x4_game_types.h)
   were references for position, context, controlled ship, pause, component checks
   and experimental spawn/remove calls. Availability is verified per game build.
7. [Egosoft wiki: Lua, MD and AI communication](https://wiki.egosoft.com/X4%20Foundations%20Wiki/Modding%20Support/UI%20Modding%20support/Talking%20with%20MD%20and%20AI%20scripts/?language=en),
   [author's article](https://chemodun.github.io/x4/modding-support/ui-modding/talking-with-md-and-ai-scripts/)
   and [extension anatomy](https://chemodun.github.io/x4/modding-support/anatomy-of-an-extension/):
   event/blackboard bridges, ID representations, ui.xml and load ordering.
8. [Microsoft ioctlsocket](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-ioctlsocket),
   [recvfrom](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-recvfrom)
   and [Linux recv](https://man7.org/linux/man-pages/man2/recvfrom.2.html): nonblocking
   network I/O. The Linux implementation was exercised using real UDP and LuaJIT.
9. Own-module ABI: [Lua 5.1 API](https://www.lua.org/source/5.1/lua.h.html),
   [auxiliary library](https://www.lua.org/source/5.1/lauxlib.h.html) and
   [package.loadlib](https://www.lua.org/manual/5.1/manual.html#pdf-package.loadlib).
10. Deployment: [ZeroTier CLI](https://docs.zerotier.com/cli/),
    [Bazzite launchers](https://docs.bazzite.gg/Gaming/Game_Launchers/),
    [firewalld](https://firewalld.org/documentation/man-pages/firewall-cmd.html),
    [PowerShell execution policy](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1).

## Other work

On 11 October 2026 the user supplied
[Neresco/X4Multiplayer_Mod](https://github.com/Neresco/X4Multiplayer_Mod).
Reviewed distribution/docs commit `2b549931e3f806095c87aa340c94cc7c6d3a20f6`
and the actual C++ implementation found separately in
[Neresco/X4Native_Linux](https://github.com/Neresco/X4Native_Linux/tree/0acec0f8bb50dee5814bbd6a1728cb90254f4b53),
commit `0acec0f8bb50dee5814bbd6a1728cb90254f4b53`, whose root contains an MIT license.
The host enumerates NPC ships by faction and streams relevant sectors; the guest
reconciles local objects or spawns replicas, using `ActivateObject(false)` to
suppress independent ship simulation. Its later notes describe erroneous
same-type/nearest-position binding and a move towards ghost rendering.

The source's inventory channel is not a dropped-container lifecycle protocol.
Its feature matrix leaves station production, credits and shot/damage replication
unsynchronized; several actions await gameplay validation. Windows binaries exist
but the latest distribution commit labels them untested. We have not run them.
Do not equate these published claims with complete universe synchronization.

A read-only ELF check confirmed nine relevant candidate exports in our installed
Linux build, including ship enumeration and `ActivateObject`. Source review also
found first-object framing loss (reproduced), suppressed empty cargo snapshots,
and discarded partial TCP sends. Preserve our host-validated MD transaction path
when learning from its dynamic ship streaming design. The detailed review and
pinned references are in [the local research record](../../../research/neresco-x4mp/REVIEW.txt).
No external code has been integrated or deployed.

X4Native notes refer to an X4Online project and object replication. A public
repository at github.com/eg3r/X4Online returned 404 during the original research.
The [linked Egosoft forum thread](https://forum.egosoft.com/viewtopic.php?t=474865)
was behind a browser check. Its full feature set or release availability was not
verified. These references make blanket impossibility claims unwarranted, but do
not establish that the outstanding simulation problems have been solved. No one
was contacted on the user's behalf.

## Own game observations

Native Linux X4 9.00 (611726), Steam build 23660954 exposed all ten initial required
APIs in its ELF executable. UI/MD loaded. ffi.C.socket was restricted and ffi.load
was absent even with protected UI disabled, leading to the own package.loadlib
module. ui.xml validated against the installed addon schema. A lengthy external
full MD schema compilation was stopped; it is not reported as passed.

On 9 October the native module connected as p1 and received the synthetic peer.
The external observer received actual player movement from (-1000, 0, 0) to values
including (-991.515625, -40.8966789, -27.9609375) metres. The creative sandbox then
confirmed spawn, position writes and rotation readback. The first deliberate
25 m displacement read back with 0.001 m error. The user saw the moving extra ship.
UI reload during startup required an explicit Lua/MD readiness handshake.

A hidden multiplayer menu and exports named NewMultiplayerGame and
ConnectToMultiplayerGame exist locally. In the inspected Linux binary,
IsNetworkEngineEnabled returned constant false (31 c0 c3). Names and UI remnants
are not evidence of a usable built-in LAN mode. No game binary was modified.

Later native cargo/combat/trade evidence and the user's first short two-computer
ZeroTier connection are recorded in docs/testing/TRADE_TESTS.txt. They do not establish a full
shared universe, stable long session or unique historical first implementation.
