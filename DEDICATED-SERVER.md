# Linux dedicated-server prototype

Status: internal development. This is an unattended **full X4 process** supervised by
Rust, not a renderer-free or standalone universe simulator. Two human guests connect
to one Linux authority. Engine hooks are not used or required.

## Roles and limits

- `p1`: Linux X4 authority and Rust relay/supervisor. Its internal player remains in
  the observer ship and does not count as a human pilot.
- `p2`: human guest, initially assigned shared ship 2.
- `p3`: second human guest, using shared ship 1 through the normal authenticated
  handoff. The authority does not teleport into that ship.
- Each guest has independent sector interest and acknowledged baseline delivery.

Persistent campaign save/resume, progressed-world late join, crash recovery into the
same world, 4–6 players, general economy/attention correctness, and public Internet
exposure are not implemented. Use a trusted LAN or VPN. A failed game or relay is not
automatically restarted; create an explicit fresh session/world instead.

## Build

Requirements: Linux x86-64, Rust 1.82+, Cargo, Python 3.10+, and the pinned X4 9.00
Linux installation (Steam build 23660954). Build an internal triple package:

```sh
python3 distribution/build_packages.py /path/to/new-dedicated-pair --dedicated     --runtime-cache /path/to/internal-runtime-cache
```

`--runtime-cache` (the normal internal runtime cache) is required by the CLI. The
dedicated build compiles `x4lan-dedicated` and the Rust `x4lan-net`
module. It does not use new C code. Existing launchers still include Python helpers
for installation and world transfer.

Configure the Linux package as host, the first Windows package as guest, and
`X4-LAN-*-Windows11-Guest2` as the second guest. Send the same private
`JOIN-SESSION.json` to both guests. Do not publish session files or configured Lua.

## Steam client requirement

The pinned build is the Steam build. Without a running, logged-in Linux Steam client
for the same user it either tries to start `~/.steam/root/steam.sh` and exits 1, or
(with `SteamAppId` set) blocks at a modal "X4 - Startup error" dialog: "Steam client is
required, please start the Steam client and try again." `run` therefore fails closed
when `$HOME/.steam/steam.pid` does not name a live process. A Steam login with an
account that owns X4 is an operator step; the supervisor never automates it.
`dev/linux_x4_start_probe.sh` reproduces one bounded start under Xvfb + lavapipe with
an isolated HOME and reports this dialog.

## WSL2 / WSLg smoke test

WSLg must provide a working X11 display (`DISPLAY=:0` and `/tmp/.X11-unix/X0`). Xvfb
is not required by this setup, but a display and working graphics driver are still
required. `-disableui` is unsupported because it prevents the multiplayer Lua from
loading. No claim is made that WSLg removes GPU work.

Use only the downloaded test installation and a fresh disposable profile 45841 with
autosave disabled and Protected UI off. Prepare the shared host world and install the
dedicated authority configuration through the normal internal launcher. Preserve
normal profiles, saves, Workshop selections, and existing sealed packages.

Start through `03-START-SERVER-HOST.sh --console`, or generate the same configuration
and invoke the Rust binary directly:

```sh
./x4lan-dedicated --config .local/dedicated/config.json run
./x4lan-dedicated --config .local/dedicated/config.json status
./x4lan-dedicated --config .local/dedicated/config.json pause
./x4lan-dedicated --config .local/dedicated/config.json resume
./x4lan-dedicated --config .local/dedicated/config.json stop
```

`run` verifies the pinned executable, prepared save, installed scripts and dedicated
Lua configuration before starting X4. Readiness requires advancing native game time
and an admitted authority peer, not just a live PID or listening UDP socket.
`stop` requests graceful `QuitGame()` through the local control file. After a crash,
the supervisor exits failed and never starts a replacement world automatically.

## Validation boundary

Rust/Python/LuaJIT tests and synthetic p2/p3 peers do not establish native two-human
play. A WSL smoke test with one native authority and synthetic guests is a separate,
limited result. Two actual clients, multi-sector combat/attention behavior, long
sessions, and persistent-world lifecycle require their own acceptance tests.
