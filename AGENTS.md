# X4 Multiplayer development

- Start with `CONTINUE-HERE.txt` and the current Git state. It links the relevant
  instructions, source and evidence by task; read those before the affected work.
  Do not load the entire historical chat, handoff archive or all reports by default.
  `CLAUDE.md` imports these shared instructions.
- Project owner is Lith7ium; ownership, licensing and publication decisions remain
  with the owner. Project lead is Ferrite, as assigned on 25 October 2026.
  Astra / A:primary now contributes under that coordination. Agree new task
  ownership, shared interfaces and integration with Ferrite; earlier onboarding
  messages naming Astra as project lead are superseded. Native access remains
  subject to each machine owner's authorization and current session ownership.
- Source development is public. Official public packages require an explicit
  request for that release. Public Test 01 is withheld because full shared-universe authority is
  unfinished. The user confirmed basic ordinary cargo/magnet success in 7.1.1;
  preserve that regression without inferring acceptance of every cargo case.
- Speak German with the user. Keep shipped documentation and interfaces English.
- Write original code. Neresco projects are research references only; do not copy
  or embed their multiplayer sources, binaries or framework.
- Engine instrumentation (runtime hooks, game-memory offsets, patched engine
  functions such as RNG or timestep) is permitted as opt-in work under
  `docs/ENGINE-INSTRUMENTATION.txt`. Never modify or redistribute X4 or Egosoft files;
  verify the exact build before patching and fail closed on any mismatch. Keep the
  `x4lan_net` transport free of hooks.
- A visual NPC replica is not proof of shared combat, inventory or economy. State
  measured limits and distinguish native X4 tests from mocks/synthetic peers.
- Keep source `src/extension/x4_lan_lab/ui/config.lua` inert and free of credentials.
  Store local environments, generated packages, logs, installation state and
  session credentials under `.local/` or outside this checkout. Never commit them.
- Do not copy `.venv` between computers. Use `python3 tools/dev/bootstrap.py`, then
  `.venv/bin/python tools/dev/check.py`. Missing LuaJIT coverage is not a passing run.
- Native tests require X4 to be closed and a new disposable world in profile
  45841, with autosave disabled. Never load, delete or overwrite normal saves.
  Reloading the owned pristine 7.3 starting snapshot is part of that isolated test.
  Preserve the previous mod/settings before replacing a known test installation.
  The current PC's tests and retaining its new internal version were authorized;
  do not assume permission to alter an unrelated machine's game installation.
- Preserve existing Git history and local laptop edits. Fetch before integrating;
  never force-push or reset away another machine's work. The user selected the
  repository `https://github.com/Lith7ium/X4-Multiplayer` for synchronization.
- Do not regenerate old MD fixture scripts blindly: compare generator output
  against current profile protections and gameplay changes before replacing it.
- Give each concurrent editing agent a separate clone/worktree and task branch.
  Agree on task ownership and shared interfaces before overlapping work; Git does
  not share chat context or lock files. Coordinate Python/Lua protocol changes.
- Deliver concurrent contributions through reviewed PRs to `main`. The project
  owner or designated integrator combines changes and prepares matched packages.
  Preserve the original 7.0 test pair; do not silently rebuild or replace it.
- Coordinate native tests on the primary PC. A Git worktree does not isolate an
  X4 installation, Steam profile or relay port. Record actual evidence and pending
  checks. Keep branch progress in its task/PR and integrated state in the handoff.

- Workshop incident22 October2026: Fix-1 test-mod isolation helper is withdrawn.
  A separate personalfolderid is not proof of Steam account isolation. Do not
  reintroduce selection writes based only on filesystem unit tests or assume
  installed ws_ IDs are a historical subscription snapshot. See
  reports/workshop-recovery-20261022.txt; preserve user Workshop subscriptions.

## Code style: readable for human contributors

Human developers review and debug this code by hand. Write every new or changed
line (Lua, Python, Rust, C, MD XML) for them, not for compactness. This rule
overrides "match the surrounding code" wherever the surrounding code is dense.

- One statement per line. No `;`-chained statements, and no loop, `if` and
  `return` packed onto one line. Put the body of `for`, `while`, `if`/`else` and
  function definitions on their own indented lines.
- One assignment per line. Do not use multiple assignment
  (`a, b, c = x, y, z`) except for a function that returns several values, such
  as `local ok, err = pcall(f)`.
- Descriptive names. Name variables, parameters and fields for what they hold:
  `api`, `message`, `state`, `pose`, `velocity`, `sectorName`. No single-letter
  names, except a loop index (`i`, `j`) or a coordinate (`x`, `y`, `z`). Name
  module aliases for their meaning: `local API = X4LAN_BUBBLE_API`, not `local A`.
- Declare locals where they are first needed, each on its own line, not in the
  middle of a long line.
- Give magic numbers a named constant, for example
  `local MAX_COOP_COORDINATE = 50000000`.
- Write a short comment when a line's intent is not obvious from the code.
  Explain engine quirks, units and frames (zone or sector, metres or km).
- Keep functions short enough to read in one screen, and split long
  expressions across lines.
- Existing dense code: tidy what you touch. Reformat and rename inside the
  function you change, with the same behaviour and passing tests, in a separate
  commit when the reformat is large. Do not mass-reformat whole files on the
  side; that hides real changes from review and risks behaviour changes.

## Focused context and verification

- Keep quality first. Read the affected source, interfaces and relevant evidence;
  a summary is a navigation aid, not proof. Expand context when a dependency,
  contradiction, safety concern or failure requires it. Use bounded searches and
  selected log fields/time ranges; avoid repeatedly dumping full files or logs.
- Maintain `CONTINUE-HERE.txt` as current state, ideally under 800 words. Replace
  superseded status instead of prepending a diary. Link detailed reports and
  archived failures; retain their original evidence and acceptance limits.
- Preserve accepted human radar/cargo/Cooperative Trial results. Repeat affected
  checks after changes; run required integration checks before integration. Reuse
  recorded results only for their tested revision/scope. Do not rerun unchanged
  suites merely to refresh a summary. Documentation-only work needs diff/link
  checks, not gameplay tests. Never equate synthetic peers with two native games.
- After a milestone, update the handoff before context compaction or a new chat.
  Do not recreate historical conversation text or start extra agents/chats merely
  to reduce context. Git shares committed instructions, not private chat memory.
- Keep current task ownership and time limits in the handoff. Historical Slack
  cadence/deadlines do not authorize new background work. Weapons/damage remain
  Claude-owned until explicitly reassigned or coordinated.
- Preserve normal settings and import display/audio/input preferences read-only
  into the test profile. Do not restore old forced graphics defaults. No access
  to personal documents/mail or spending. Preserve sealed release archives.
- New Windows and Linux internal packages include server/runtime and English
  documentation. Two simultaneous players remain the implemented limit.

## Public source and licensing

- The owner authorized public development and free noncommercial use, changes,
  forks and redistribution on 26 October 2026. LICENSE.md is the unmodified
  PolyForm Noncommercial 1.0.0 text; preserve NOTICE.txt and third-party terms.
  Read LICENSING.md before changing licensing or making commercial-use claims.
- Public read/fork access does not grant write access to the original repository.
  Only the owner and invited collaborators may push or merge there. Outside
  contributors use their own forks and pull requests. Do not invite collaborators
  or grant write access merely because someone wants to download or fork.
- Official playable packages still need explicit release authorization; permitted
  independent noncommercial builds are distinct from official releases.
- Public source is not permission to publish private session data, credentials,
  raw personal logs, local account paths or private coordination material. Keep
  those outside Git and scrub new reports before committing. Do not rewrite
  existing shared history as an incidental cleanup.
