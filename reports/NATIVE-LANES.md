# Native test lanes B1/B2 (dev/x4lane.py)

Owner: lane agent, branch `contrib/ferrite-rw-lane` (rewrite program, see `REWRITE-PROGRAM.md`).
Own files: `dev/x4lane.py`, `dev/lane/`, this report, `C:\Users\developer\Documents\X4Mods\lanes\`,
profiles 45850/45851. Live usage notes for the other agents: `rewrite-coord\native-lane.md`.

Status: see "Validation" at the end. Evidence stays outside git (`.local/evidence-075-withheld`).

## What a lane is

| lane | profile | game view | lock |
|---|---|---|---|
| b1 | `Documents\Egosoft\X4\45850` | `X4Mods\lanes\b1\game-view\steamapps\common\X4 Foundations` | `rewrite-coord\lane-b1.lock` |
| b2 | `Documents\Egosoft\X4\45851` | `X4Mods\lanes\b2\game-view\steamapps\common\X4 Foundations` | `rewrite-coord\lane-b2.lock` |

- **Game view**: every directory of the real install except `extensions` is a directory junction,
  every file a hardlink (`X4.exe`, `.cat/.dat`, DLLs). `steam_appid.txt` (392160) is the view's own
  file, `appmanifest_392160.acf` a copy. `extensions\` is a real folder with junctions to the
  `ego_dlc_*` extensions, a fresh copy of the base `x4_lan_lab` and, during a run,
  `x4lane_autostart` plus the run's test extensions. No other installed mod is linked.
- **The install is never written.** All writers in `dev/lane/lanecore.py` refuse a target that
  resolves outside the lane folder (junctions are followed when checking), any link, and any file
  with more than one hardlink (the install files). Deleting refuses a tree that contains a link or a
  hardlinked file. `setup --rebuild-view` removes junctions and hardlink names only, never targets.
- **Profiles**: `config.xml` and `inputmap.xml` are copied read-only from 45842 (display, audio,
  input preferences). The lane config is windowed (`borderless`/`fullscreen` false) at 1280x720,
  autosave false, `uisafemode` false; `load`/`module`/`mpar`/`seed`/`usedefaultconfig` overrides are
  removed and refused. `content.xml` enables the DLCs, `x4_lan_lab`, `x4lane_autostart` and the
  run's test extensions. **No Workshop (`ws_*`) selection is ever written** (Workshop incident,
  `reports/workshop-recovery-20261022.txt`); instead a run refuses while X4 Workshop items are
  installed in the Steam library (none are today), because unlisted items would load.
  The pristine copies live in `lanes\<lane>\profile-base\` and are restored before and after every run.
- **Base `x4_lan_lab`**: `lanes\base\x4_lan_lab`, a snapshot of `extension/x4_lan_lab` with its git
  commit and file hashes in `lanes\base\base.json`. A run refuses if the snapshot no longer matches.
  The source config is inert (`enabled = false`, no token). `--base DIR` uses another x4_lan_lab.
- **Offline guard**: after overlays are applied, the lane `ui/config.lua` must keep
  `enabled = false`, no non-empty `token` and only loopback `host` values, or the run refuses.
  Lanes never talk to the VPS or any network peer.

## Unattended new world (x4lane_autostart)

`-module <gamestart>` on the command line starts a new game directly, without the start menu
(the same option the internal launchers and `sandbox-baseline\host\launch45842.cmd` use);
`-seed:<n>` fixes the universe generation seed (default 276944566, `--seed 0` = random). A fixed
seed does not make two worlds identical (`SHARED-START-20261023.txt`: 46 of ~1380 stations
differed between two native worlds with one seed), so compare runs differentially, not byte for byte.

`dev/lane/x4lane_autostart` is a test-only extension (never in the mod package). The runner
writes `ui/settings.lua` for each run. It logs `[X4LANE] AUTOSTART` markers into the debug log:

| marker | meaning |
|---|---|
| `loaded` | the UI state ran the script (once per UI load) |
| `MENU` | the start menu shows |
| `NEWGAME gamestart=... reason=menu\|module_fallback` | it called `NewGame(<gamestart>)` from the start menu (`--start menu`, or `-module` did not start the world within 60 s) |
| `SKIP` | start menu still shown after 300 s real time; no further attempt |
| `INGAME gamestart=... expected=... match=true` | a player exists outside the start menu |
| `READY` | game time advanced 5 s since INGAME (the simulation runs) |
| `ALIVE realtime= gametime= paused=` | every `--alive` seconds (default 10, 0 = off) |
| `QUIT reason=quit_after` | safety net: `QuitGame()` after timeout + settle + 270 s of process real time |
| `sound=off` | the instance was muted (`--sound` keeps sound) |

`NewGame()` is only ever called while `IsStartmenu()` is true, at most once per UI state and only
in the first 300 s of process real time, so a loading world is never started twice.

## run: what happens

1. Validate options; refuse while `rewrite-coord\quiet.lock` exists, without a logged-in Steam
   client, with X4 Workshop items installed, with less than 12 GB free RAM, if the lane view no
   longer links the current install (X4 update) or `X4.exe` is not the pinned build
   (`native/engine/signatures.py`).
2. Take the lane lock atomically (`mkdir lane-<lane>.lock`, `owner.txt` with agent, run id, pid,
   start, updated). `--lane auto` takes the first free lane. A lock is stale (taken over) when its
   owner process on this machine has exited, or `owner.txt` was not updated for 45 min.
   `--wait S` waits for quiet.lock/lanes/RAM (one check per 60 s).
3. Prepare: kill an orphan X4 of this lane's view (previous runner died), reset `extensions` to
   base, apply overlays, install `x4lane_autostart` and `--extension` copies, restore the profile
   files (plus `--config` overrides), delete the old debug log.
4. Launch, serialized by `rewrite-coord\lane-launch.lock`: find the X4 that holds
   `EGOSOFT_X4_INSTANCE` (`x4lock query`), release it (`x4lock release --pid --expect-path
   --wait-ms 30000`), check that the name is gone (`x4lock list`), start
   `X4.exe -personalfolderid <profile> -module <gamestart> -seed:<n> -skipintro -debug scripts
   -logfile x4lan-debug.txt -nocputhrottle -noworkshopsync` from the lane view
   (`SW_SHOWNOACTIVATE`, so it does not take focus), put it in a kill-on-close job object (it dies
   with a crashed runner), and wait until it holds the instance lock itself. A
   "Game is already running!" box of our process is closed and the run fails (exit 7).
5. Monitor: the debug log is read incrementally (`--until`, `--until-count`, `--fail-on`,
   `--expect`, markers); dialogs of our X4 every 2 s; other X4 processes and our memory every 10 s;
   lock refresh every 60 s.
6. End: after `--until` plus `--settle` seconds, or `--timeout`, or X4 exit, or a `--fail-on`
   match. Close only our X4 (exe path checked): WM_CLOSE, then TerminateProcess after
   `--close-grace` (30 s).
7. Collect into OUTDIR: `x4lan-debug.txt`, `engine-output.log` (X4 stdout/stderr), `installed/`
   (final ui.xml, config.lua, autostart settings, profile config/content), new crash files from
   the profile, `result.json`. Restore the lane, release the locks.

Exit codes: 0 pass, 1 usage, 2 refused (nothing started), 3 `--until` timeout, 4 X4 exited early,
5 `--fail-on` matched, 6 `--expect` not met, 7 launch/lock problem, 8 internal error, 9 interrupted.
Without `--until` the run lasts `--timeout` seconds and passes if the expectations hold.

`result.json`: `verdict`, `exit_reason`, `exit_code`, `message`, `lane`, `profile`, `run_id`,
`timings` (seconds since the runner started: `prepared_s`, `launched_s`, `mutex_s`, `first_log_s`,
`game_start_s`, `ingame_s`, `ready_s`, `until_s`, `closed_s`, `total_s`, `launch_to_ingame_s`),
`markers`, `until`, `expect`, `fail_on`, `log` (lines, `=ERROR=` lines, bytes), `x4` (pid, args,
memory, closed, exit_code, peak memory), `other_x4` (`at_start`, `during_run`, `any`),
`quiet_lock`, `timing_trustworthy`, `timing_note`, `x4lock` output, `install` (base commit,
overlays, copied/appended files, ui files, hashes), `dialogs`, `warnings`, `lane_restored`.

## Overlays

An overlay is a directory mirroring `x4_lan_lab`; `--overlay` may repeat (applied in order):

| overlay file | effect |
|---|---|
| any file, e.g. `native/x4lan_fixedtick_64.dll`, `ui/fixed_tick_probe.lua`, `md/foo.xml` | copied over the lane x4_lan_lab (replaces or adds) |
| `<path>.append`, e.g. `ui/config.lua.append` | appended to `<path>` |
| `ui.xml.files` | one `ui/*.lua` per line, inserted into `ui.xml` right after `ui/config.lua` (no duplicates) |
| `x4lane-*` at the overlay root | documentation only, not installed |

Text files (`.lua .xml .txt .json .md .append .files`) get `@X4LANE_PROFILE@`, `@X4LANE_LANE@`,
`@X4LANE_RUN@`, `@X4LANE_AGENT@` replaced. `--lua LINE` appends a line to `ui/config.lua`,
`--ui-file ui/x.lua` adds a ui.xml entry. `--extension DIR` copies a whole test extension (its
`content.xml` id is enabled for the run; `ego_dlc_*`, `ws_*`, `x4_lan_lab`, `x4lane_autostart` are
reserved). `make-overlay probe` and `make-overlay attnload` build ready overlays (see native-lane.md).

## Timing caveats

A lane run next to other X4 instances (other lanes, lane A, the x4mods-61 tests, save-trim runs)
shares CPU, GPU and memory bandwidth with them. Such runs are functional/differential results only.
Timing numbers need `--timing`: the run takes `rewrite-coord\quiet.lock` itself and refuses while
any other X4 runs; `result.json` then reports `timing_trustworthy: true` only if no other X4 was
seen during the whole run. Unfocused windows run at full rate because of `-nocputhrottle`
(`x4-re/coord/reports/simtick.md`); minimizing or dragging an X4 window stops its simulation.

## Validation

(filled in by the native validation runs)
