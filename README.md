# X4 Multiplayer — internal 7.3.0

Experimental two-player co-op for X4 Foundations. This version prepares one native
host starting save and transfers it to the guest so both start in the same world.
Ongoing universe authority and campaign persistence remain unfinished.

Start development with [AGENTS.md](AGENTS.md) and the short
[current handoff](CONTINUE-HERE.txt). Follow its task-specific references instead
of reading all historical reports. [Claude instructions](CLAUDE.md) use the same
shared rules. Read [setup instructions](DEVELOPMENT.txt) for a new machine and
[contribution instructions](CONTRIBUTING.txt) for collaboration and PR delivery.

## Internal playtest

Use the matched 7.3.0 Windows/Linux packages, both including the server and portable
runtime. Follow [installation instructions](distribution/INSTALL.html), the
[package guide](distribution/README.txt) and the
[focused station test](distribution/NEXT-TEST.txt). The host runs both X4 and the
relay. Host setup prepares the starting save once; guests import the private
invitation and download the verified world at launch. UDP45841 and TCP45844 are
required. Both roles use isolated test profile 45841 and matching game/DLC content.

Normal display/audio/input preferences are imported read-only into the test
profile; autosave is disabled. Preserve normal saves and Steam subscriptions.
The withdrawn Workshop selection helper must not be used. Never commit session
credentials, generated packages or installed configuration.

Restart both games and the relay together. Restarting restores the original
starting world: it does not retain multiplayer progress. Preserve sealed archives;
new builds belong in a new directory. Public Test 01 is withheld; another public
release requires an explicit request.

## Evidence and remaining work

Separate native Linux roles matched 1,359 station identities after loading the
same save. The clean packaged Linux host passed capture/transfer/startup with a
synthetic guest. Native Windows hosting and simultaneous Windows/Linux gameplay
still need human acceptance; see the [native report](reports/SHARED-START-20261023.txt)
and [package evidence](reports/package-7.3.0-checks.json).

Humans passed radar, ordinary cargo/magnet interaction and Cooperative Trial in
7.2. These results do not establish general combat, every cargo case or arbitrary
shared missions. Ongoing station economy, construction/destruction, complete
NPC/docking authority, campaign save/resume, progressed-world join and ingame chat
remain unfinished. See [world requirements](WORLD-SYNC-REQUIREMENTS.txt).

Source: `x4lan/` holds relay/session logic, `extension/x4_lan_lab/` the Lua/MD/AI
adapters, and `distribution/` the launchers and packaging. Development checks
require Linux x86_64, local UDP and the configured LuaJIT runtime; native game
acceptance is separate. Tests should cover affected behavior and required
integration gates, without replaying unchanged accepted work unnecessarily.

The [previous README](reports/handoff-history/2026-10-23/README-before.md) and
[previous handoff](reports/handoff-history/2026-10-23/CONTINUE-HERE.txt) are retained
as historical references. Their old version, installation and coordination
instructions are superseded by the current handoff and package guide.
