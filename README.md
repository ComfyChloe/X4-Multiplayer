# X4 Multiplayer — internal development

Experimental two-player co-op for X4 Foundations 9.00, Steam build 23660954.
The host prepares a native starting save and transfers it to the guest. Selected
live gameplay is synchronized; full ongoing universe authority and campaign
persistence remain unfinished.

New contributors and coding agents: start with the
[onboarding packet](onboarding/00-START-HERE.txt), including its pinned code baseline,
[starter prompt](onboarding/01-AGENT-START-PROMPT.txt) and
[source map](onboarding/03-CODE-AND-STATUS.txt). Current development is on
`contrib/astra-7-4-integration` ([draft PR37](https://github.com/Lith7ium/X4-Multiplayer/pull/37));
do not assume `main` has these changes.

Read [AGENTS.md](AGENTS.md) and the short [current handoff](CONTINUE-HERE.txt).
Use [development setup](DEVELOPMENT.txt) and [contribution rules](CONTRIBUTING.txt)
for your own environment/worktree. [CLAUDE.md](CLAUDE.md) imports the same rules.
Load only task-relevant evidence rather than the entire historical archive.

## Delivered packages and current source

The sealed internal Windows/Linux pair is **7.4.1**, from source `235f9ba`.
Both packages contain the server, private Python runtime and English procedures.
Linux's alternative Small.7z is 19,929,193 bytes with the same payload. Follow
[installation instructions](distribution/INSTALL.html), the
[package guide](distribution/README.txt) and [human test procedures](distribution/NEXT-TEST.txt).
The host runs X4 and the relay; UDP45841 and TCP45844 are required. Both players
use profile45841 with matching game/DLC content and a private host invitation.

The source additionally contains **unreleased** community installer fixes from
`7c6ec19`: Windows process decoding, unavailable Steam drives, explicit game path
selection, validated session recovery and preference reselection. They are not in
those sealed archives. See the [review](reports/COMMUNITY-PATCH-REVIEW-20261025.txt)
for accepted changes and reproduced failures in the excluded fleet/map experiment.

## Acceptance and remaining work

Human tests have confirmed ordinary cargo/magnet interaction, Cooperative Trial,
radar, station/asteroid starting alignment, movement and Alt+Tab within their tested
scope. On 24 October, guest-to-host shield damage was explicitly confirmed on both
screens. This does not establish full hull/death or arbitrary weapon/NPC acceptance.

Open reports include guest inability to select a correctly moving host ship,
boarding/occupied-pad cases, captured-ship ownership, NPC combat and faction-fleet
orders. Complete background economy, progressed-world save/resume and absent-client
catchup remain unfinished. Restarting currently returns to the pristine starting
world. See [world requirements](WORLD-SYNC-REQUIREMENTS.txt) and the current handoff
for evidence limits and the relevant native reports.

The source baseline `7c6ec19` has 590 passing Linux/Python/LuaJIT tests without skips;
PowerShell installation/discovery fixtures also passed on Linux. Native Windows
and human gameplay acceptance remain separate. A new contributor runs the suite
once to establish their environment, then verifies affected behavior and required
integration gates. Documentation changes need reference/diff checks, not gameplay.

Preserve normal saves, settings, Steam subscriptions and sealed packages. Preference
import is read-only at the source. The withdrawn Workshop-selection writer stays
withdrawn. Never commit credentials, saves, configured packages or installed
`config.lua`; source configuration stays inert. Work remains internal; public
packages or source-mirror updates require an explicit owner request.
