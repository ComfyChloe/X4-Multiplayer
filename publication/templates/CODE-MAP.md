# Code map

These links are for inspection, not instructions to install or execute the code.

| Area | Starting points |
| --- | --- |
| Relay and host sessions | [host.py](x4lan/host.py), [universe.py](x4lan/universe.py), [coop.py](x4lan/coop.py) |
| Starting-world capture and loading | [world_launcher.py](distribution/world_launcher.py), [seed.py](x4lan/seed.py), [world_start.py](x4lan/world_start.py), [x4lan_seed.xml](extension/x4_lan_lab/md/x4lan_seed.xml) |
| Native game adapters | [UI/Lua](extension/x4_lan_lab/ui), [Mission Director](extension/x4_lan_lab/md), [AI scripts](extension/x4_lan_lab/aiscripts) |
| Cargo and shared economy fixtures | [cargo.py](x4lan/cargo.py), [economy.py](x4lan/economy.py), [quests.py](x4lan/quests.py) |
| Installer and personal settings | [session_launcher.py](distribution/session_launcher.py), [profile_settings.py](distribution/profile_settings.py), [windows_setup.ps1](distribution/windows_setup.ps1), [linux_setup.py](distribution/linux_setup.py) |
| Native network bridge | [net.c](native/net.c), [build.py](native/build.py) |
| Regression and diagnostic code | [tests](tests), [dev](dev) |

`prepare.py` and `distribution/public/` contain older packaging paths. They do not
represent a newly accepted public release. The withdrawn test-mod isolation helper
remains disabled in source; a separate X4 profile does not establish isolation of
the Steam account. The current implementation must not be inferred from old
package templates alone.

The relay uses Python's standard library. Developer checks additionally use Lupa's
LuaJIT runtime and the project's compiled native bridge. The dependency declaration
is included for inspection; dependencies and compiled files are not bundled.
