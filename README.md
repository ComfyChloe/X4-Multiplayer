# X4 Multiplayer — public development

Free to download, build, use, modify and share for purposes permitted by the
[PolyForm Noncommercial License 1.0.0](LICENSE.md). Commercial use outside those
purposes requires **Lith7ium's explicit written permission**. See
[licensing and commercial requests](LICENSING.md) and [required notices](NOTICE.txt).
This is source-available software with a noncommercial restriction, not an
OSI-approved open-source project.

Anyone may fork and change their own copy. Only the owner and invited
collaborators can push to this original repository or merge changes here;
visitors can propose changes through pull requests.

Existing contributors: read the [history migration notes](PUBLICATION-NOTES.txt)
before pushing from an older checkout. The project owner is Lith7ium; Ferrite
leads technical development.

Experimental two-player co-op for X4 Foundations 9.00, Steam build 23660954.
The host prepares a native starting save and transfers it to the guest. Selected
live gameplay is synchronized; full ongoing universe authority and campaign
persistence remain unfinished.

New contributors and coding agents: start with the
[onboarding packet](onboarding/00-START-HERE.txt), including its pinned code baseline,
[starter prompt](onboarding/01-AGENT-START-PROMPT.txt) and
[source map](onboarding/03-CODE-AND-STATUS.txt). The integrated `main` baseline contains the 7.5.0 delivery. Dedicated-server
and additional-client experiments are on separate development branches; agree
the actual task baseline with Ferrite before contributing.

Read [AGENTS.md](AGENTS.md) and the short [current handoff](CONTINUE-HERE.txt).
Use [development setup](DEVELOPMENT.txt) and [contribution rules](CONTRIBUTING.txt)
for your own environment/worktree. [CLAUDE.md](CLAUDE.md) imports the same rules.
Load only task-relevant evidence rather than the entire historical archive.

## Delivered packages and current source

The delivered internal candidate is **7.5.0**; see the
[delivery evidence](reports/INTERNAL-7-5-DELIVERY.txt). Both Windows/Linux packages include
the server, private Python runtime and English procedures. Follow
[installation instructions](distribution/INSTALL.html), the
[package guide](distribution/README.txt) and [human test procedures](distribution/NEXT-TEST.txt).
The host runs X4 and the relay; UDP45841 and TCP45844 are required. Both players
use profile45841 with matching game/DLC content and a new private host invitation.
The sealed7.4.1 pair from235f9ba remains unchanged and does not contain these additions.

This candidate includes reviewed installer fixes from7c6ec19: Windows process
output, unavailable Steam drives, explicit game paths, owned same-version session
recovery and preference reselection. See the
[community review](reports/COMMUNITY-PATCH-REVIEW-20261025.txt).

The [NPC combat path](reports/NPC-COMBAT-20261025.txt) and first
[ordinary faction mission adapter](reports/FACTION-MISSIONS-20261025.txt) are now
enabled in launcher-generated test configurations. Source and unconfigured
packages remain inert. NPC scope is free-flying S/M main hull/shields with admitted
stock guns; mission scope is standalone matching-ship kill contracts without
inventory rewards. Separate native host/guest runs passed within their documented
scope. Subsequent human tests confirmed supported NPC destruction and shared
kill-contract completion, with intermittent NPC synchronization still unresolved.
See the [human results](reports/PLAYTEST-7.5.0-20261025.txt) and
[new NPC/mission procedure](distribution/HUMAN-NPC-MISSION-TEST.txt).

## Acceptance and remaining work

Human tests have confirmed ordinary cargo/magnet interaction, Cooperative Trial,
radar, station/asteroid starting alignment, movement and Alt+Tab within their tested
scope. The 25 October human test also confirmed PvP damage, the supported shared
kill mission and observed docking/ship-exchange sequences. These are bounded
results, not acceptance of all ships, weapons, missions or docking situations.

Open reports include guest inability to select a correctly moving host ship,
remaining occupied-pad cases, captured-ship ownership and faction-fleet orders.
NPC targeting/speed/fire remain intermittent, and highway travel can cause
position jumps. Guest wreck/debris presentation is incomplete. Complete background economy, progressed-world save/resume and absent-client
catchup remain unfinished. Restarting currently returns to the pristine starting
world. See [world requirements](WORLD-SYNC-REQUIREMENTS.txt) and the current handoff
for evidence limits and the relevant native reports.

The7.5.0 candidate has636 passing Linux/Python/LuaJIT tests without skips;
PowerShell installation/discovery fixtures also passed on Linux. Native Windows
and human gameplay acceptance remain separate. A new contributor runs the suite
once to establish their environment, then verifies affected behavior and required
integration gates. Documentation changes need reference/diff checks, not gameplay.

Preserve normal saves, settings, Steam subscriptions and sealed packages. Preference
import is read-only at the source. The withdrawn Workshop-selection writer stays
withdrawn. Never commit credentials, saves, configured packages or installed
`config.lua`; source configuration stays inert. Source development is public,
while official playtest packages remain internal unless the owner authorizes a
public release. The license permits third-party noncommercial builds, but those
are independent builds and must not be presented as an official project release.
The current code uses no game-memory offsets or copied assets; engine
instrumentation is opt-in under `ENGINE-INSTRUMENTATION.txt`.
