# X4 Multiplayer development

- Read `CONTINUE-HERE.txt`, `DEVELOPMENT.txt` and `WORLD-SYNC-REQUIREMENTS.txt`.
- Default to internal work. Public packages require an explicit request for that
  release. Public Test 01 is withheld because full shared-universe authority and
  ordinary cargo/magnet interactions are missing.
- Speak German with the user. Keep shipped documentation and interfaces English.
- Write original code. Neresco projects are research references only; do not copy
  or embed their multiplayer sources, binaries or framework.
- A visual NPC replica is not proof of shared combat, inventory or economy. State
  measured limits and distinguish native X4 tests from mocks/synthetic peers.
- Keep source `extension/x4_lan_lab/ui/config.lua` inert and free of credentials.
  Store local environments, generated packages, logs, installation state and
  session credentials under `.local/` or outside this checkout. Never commit them.
- Do not copy `.venv` between computers. Use `python3 dev/bootstrap.py`, then
  `.venv/bin/python dev/check.py`. Missing LuaJIT coverage is not a passing run.
- Native tests require X4 to be closed and a new disposable world in profile
  45841, with autosave disabled. Never load, delete or overwrite normal saves.
  Preserve the previous mod/settings before replacing a known test installation.
  The current PC's tests and retaining its new internal version were authorized;
  do not assume permission to alter an unrelated machine's game installation.
- Preserve existing Git history and local laptop edits. Fetch before integrating;
  never force-push or reset away another machine's work. The user selected the
  private repository `https://github.com/Lith7ium/X4-Multiplayer` for synchronization.
- Do not regenerate old MD fixture scripts blindly: compare generator output
  against current profile protections and gameplay changes before replacing it.
