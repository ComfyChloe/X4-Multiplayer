# Native test lanes b1-b4 (dev/x4lane.py)

Owner: lane agent, branch `contrib/ferrite-rw-lane` (rewrite program, see `REWRITE-PROGRAM.md`).
Own files: `dev/x4lane.py`, `dev/lane/`, this report, `C:\Users\developer\Documents\X4Mods\lanes\`,
profiles 45850/45851/45853/45854. Live usage notes for the other agents: `rewrite-coord\native-lane.md`.

Status: single-lane runs READY, validated natively on 26 October 2026 (see "Validation"); session
mode, queue, stop, doctor and stall detection offline-tested, native validation pending. Evidence
stays outside git (`.local/evidence-075-withheld`).

The other agents run a read-only export of a tested commit, `X4Mods\lanes\release` (made by
`x4lane.py publish-release`, commit in `RELEASE.txt`), never this development worktree.

## What a lane is

| lane | profile | game view | lock |
|---|---|---|---|
| b1 | `Documents\Egosoft\X4\45850` | `X4Mods\lanes\b1\game-view\steamapps\common\X4 Foundations` | `rewrite-coord\lane-b1.lock` |
| b2 | `Documents\Egosoft\X4\45851` | `X4Mods\lanes\b2\...` | `rewrite-coord\lane-b2.lock` |
| b3 | `Documents\Egosoft\X4\45853` | `X4Mods\lanes\b3\...` | `rewrite-coord\lane-b3.lock` |
| b4 | `Documents\Egosoft\X4\45854` | `X4Mods\lanes\b4\...` | `rewrite-coord\lane-b4.lock` |
| b5..b8 | `Documents\Egosoft\X4\45855`..`45858` | `X4Mods\lanes\b5\...`..`b8` | `rewrite-coord\lane-b5.lock`..`b8` |

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
| `QUIT reason=quit_after` | safety net: `QuitGame()` after timeout + settle + 270 s of real time since the world started |
| `sound=off` | the instance was muted (`--sound` keeps sound) |

`NewGame()` is only ever called while `IsStartmenu()` is true, at most once per UI state and only
in the first 300 s of real time, so a loading world is never started twice.

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

## Waiting: the lane queue

`--wait S` puts a run or session into `rewrite-coord\lane-queue` (one JSON ticket per waiting
runner, named by creation time). The queue is strict first come, first served: only the oldest live
ticket attempts to take lanes (every 10 s); the others only refresh their ticket (every 30 s). A
queued session keeps lanes it already got while it waits for the rest, for at most 10 minutes
(then it gives them back and stays first). A run without `--wait` takes a free lane directly but is
refused while others are queued. A quiet-window handoff target bypasses the queue. Tickets of an
exited runner, or not refreshed for 5 minutes, are dropped. `x4lane.py queue` lists the queue.

## Sessions: N clients in one loopback session

`x4lane.py session --clients 2|3` (terminology: the dedicated X4 is the server, players are clients
1..N, all equal):

1. Takes N lanes (`--lanes b3,b4` or any free ones; all locks held for the whole session) and checks
   RAM for N instances (12 GB each by default).
2. Prepares each client like a single run, with the shared `--overlay/--config/--lua/--extension`
   followed by that client's `--client-overlay N:DIR`, `--client-config N:K=V`, `--client-lua N:LINE`,
   `--client-extension N:DIR`. Every client gets the same `--gamestart/--seed`. Session tokens are
   substituted in client files: `@X4LANE_PORT@`, `@X4LANE_TOKEN@` (fresh 32 hex digits),
   `@X4LANE_BUILD@`, `@X4LANE_BUBBLE@`, `@X4LANE_CLIENT@`, `@X4LANE_SLOT@` (p1..pN), `@X4LANE_CLIENTS@`,
   `@X4LANE_SESSION@`.
3. Profile gates: the lane copy is re-pinned from 45841 to the lane profile (quoted `'45841'`
   literals, `test_profile` included, and `/45841/save` paths in every .lua of x4_lan_lab and the
   test extensions). Each gate then accepts exactly the lane profile, as it accepted exactly 45841
   before. `--no-repin` keeps them; single runs opt in with `--repin-profile`.
4. Loopback guard instead of the offline guard: `enabled = true` and a token are allowed, but every
   `host` value and every quoted IPv4 literal in the final config must be `127.0.0.1`, and no quoted URL.
5. Optional server (`--server-cmd`): started first with `{port} {token} {build} {bubble} {clients}
   {session} {outdir} {python} {repo}` substituted (and the same values as `X4LANE_*` environment),
   in `--server-cwd` (default: the tool's checkout, which carries the `x4lan` relay package), output
   in `server.log`, inside a kill-on-close job with every process it starts. It is ready when it
   matches `--server-ready` or owns an endpoint on 127.0.0.1:{port}. Its TCP/UDP endpoints (IPv4 and
   IPv6, all processes of its job) are checked at start and every 5 s: anything not bound to loopback,
   or a TCP peer outside loopback, ends the session (`network_violation`, exit 5).
6. Launches the clients one after the other under `lane-launch.lock` (each releases the previous
   client's instance lock), then monitors all of them: pass when every client matched `--until`
   `--until-count` times (`--until-mode any`: one client) and the server matched `--server-until`.
   Any client exit, `--fail-on`, `--server-fail-on`, server exit or stall ends it.
7. Closes all clients (WM_CLOSE to all, then wait/terminate), stops the server (job terminate),
   collects `clientN\` (result.json, logs, installed files) and `server.log`, restores every lane and
   writes the session `result.json` (clients[], server{command with the token masked, endpoints},
   network{port, token hash}, until hits per client).

`make-overlay session-telemetry` builds the client overlay for the 7.x telemetry adapter
(`game.lua` with only `enabled = true`: HELLO/WELCOME to the relay, then poses): the simtick DLL
(Windows frame driver) and the loopback config with the session tokens. With
`python -m x4lan.host --bind 127.0.0.1 --port {port} --token {token} --build {build} --bubble {bubble}`
as the server, both clients log `connected to relay as pN` and `relay=connected remote=receiving`,
and the relay prints both peers in join order (`p2@127.0.0.1:..., p1@127.0.0.1:...` is as likely as the
reverse), so `--server-until` must not depend on the order:
`(?=.*p1@127\.0\.0\.1:\d+)(?=.*p2@127\.0\.0\.1:\d+)`.

## Stop, stall, doctor

- `x4lane.py stop --all-lanes|--lane b1,b2 [--force] [--reason]`: WM_CLOSE to lane X4 instances only
  (exe path must be a lane view X4.exe); a `stop-request.json` marker lets their runners report
  `exit_reason: stopped` (exit 9) with who and why. `--force` terminates after `--grace` (30 s).
- `--stall S` (default 240): after INGAME the autostart writes an ALIVE line every `--alive` seconds;
  a debug log silent for S seconds ends the run as `stalled` (exit 3) and records whether the window
  still responds (IsHungAppWindow). Off with `--stall 0` or `--alive 0`.
- `x4lane.py doctor [--fix] [--json]`: checks base, x4lock, Steam, release export, and per lane the
  view, lock (stale?), base state (dirty while free?), orphan lane X4 without a runner, stop markers,
  temp/staging leftovers; reports the queue. `--fix` repairs only what is safe (stale locks, dirty
  free lanes under their own lock, orphan lane X4s with a verified exe path, leftovers). Every run
  records the non-ok findings of a quick pass in `result.json` (`selfcheck`). Lanes also heal on use:
  a run on a dirty lane restores it, kills its orphan X4 and takes over a stale lock.

## Protected X4 instances

User decision (26 October): the x4mods-61 session's two clients and the user's own game are
protected. Protected = an X4 whose command line names profile 45841 or 45842, whose exe is in the
real Steam install or in `X4-Multiplayer-fixed-tick\.local\sandbox-baseline\guest\game-view`, or whose
image path cannot be read (fail closed). The lanes never run x4lock on them (not even `query`), never
close or terminate them; `stop`, `doctor --fix` and the orphan kill act on non-protected lane exes
only. Presence of the instance mutex is read from the session's object directory
(NtQueryDirectoryObject), never by opening it, so a lane can never hold a handle that keeps the name
alive while x4mods-61 releases its own client. When only protected X4 run and the mutex exists, or
when x4lock reports exit 5 after releasing a lane holder (a protected client holds the name too),
the run waits (queue / `--wait`, retried every 10 s with the launch lock released in between).

## Admission and memory pressure

User: "we can handle like 7 or 8 instances at once". Every X4.exe on the PC counts (lanes, protected
clients, other sessions): a lane X4 starts only while fewer than `MAX_X4_TOTAL = 8` run and at least
`MIN_FREE_PER_START = 8` GB are available per X4 to be started (a session counts each client). The
rule is checked at preflight and again under `lane-launch.lock`, so two runs cannot both pass it.
Every running lane samples the machine every 10 s; when available memory stays under
`PRESSURE_LOW_GB = 4` for more than 60 s, `rewrite-coord\lane-pressure.json` switches to `pressure`
and no new lane X4 starts until available memory stayed at or above 8 GB for 60 s. Nothing is ever
stopped for it (x4mods-61 reclaims the PC with quiet.lock or `x4lane stop --all-lanes`). result.json
carries the samples (available RAM, X4 total, every X4's working set, hard faults from the kernel's
PageReadCount, pressure) and summaries (`machine`, `log.fps`). `x4lane.py observe` records the same
machine-wide, read-only, for any period.

## Quiet-window handoff chain

`handoff: a, b, c` in quiet.lock's owner.txt: a's `--timing` run adopts the lock (no other X4 may run);
while it runs the rest of the chain is stored as `then:` (so nobody else can adopt it mid-run); at
its end the lock is handed on (`handoff: b, c`, `handed_from: a`, `handoff_since`) instead of removed.
A handed-on member that does not adopt within 10 minutes is skipped (by any run's checks or
`doctor --fix`); the last member, or running out of members, removes the lock. The first member of
a lead-made chain is not timed. `--timing` waiters never hold the queue head.

## Timing caveats

A lane run next to other X4 instances (other lanes, lane A, the x4mods-61 tests, save-trim runs)
shares CPU, GPU and memory bandwidth with them. Such runs are functional/differential results only.
Timing numbers need `--timing`: the run takes `rewrite-coord\quiet.lock` itself and refuses while
any other X4 runs; `result.json` then reports `timing_trustworthy: true` only if no other X4 was
seen during the whole run. Unfocused windows run at full rate because of `-nocputhrottle`
(`x4-re/coord/reports/simtick.md`); minimizing or dragging an X4 window stops its simulation.

## Validation (26 October 2026, Windows, X4 9.00 build 23660954)

Offline: `tests/test_x4lane.py`, 35 tests under real LuaJIT (overlay copy/append/ui.xml order and
tokens, offline config guard, path safety with real junctions and hardlinks, lock acquire/stale
takeover/foreign locks, log watcher with partial and recreated logs, profile config/content files,
autostart Lua state machine, make-overlay probe/attnload). Pass, no skips; `test_fixed_tick_probe` still passes.

Native (evidence `X4-Multiplayer-rw-lane/.local/evidence-099-withheld`, result.json + logs per run). Every run
had at least one other X4 running (a save-trim session X4 on 45843, or the other lane), so all
numbers below are functional, not timing-grade. Overlay: fixed-tick probe built from this branch.

| run | what | result |
|---|---|---|
| v1-b1-alone | b1, until 20 WALKPROF | pass; x4lock released the save-trim X4's lock (exit 0, placeholder installed); launch->INGAME 51 s, READY 61 s, until 101 s; WM_CLOSE exit 0 in 5.6 s |
| v1-b2-alone | b2, same | pass; launch->INGAME 50 s, READY 60 s; WM_CLOSE 5.3 s |
| v1-both-1/-2 | two runners `--lane auto` started together | both pass (b1, b2); the second launch released the first lane's lock; INGAME 67/68 s, 30 WALKPROF each; WM_CLOSE 5.6/5.0 s |
| v1-b1-attnload | probe + `make-overlay attnload` (steps 0,8; 30/90 s) | pass: `HOOKS_READY ... profile=45850` (lane guard change works), 139 sectors ranked, step 0 then 8 proxy sectors, `ATTNLOAD done`; WALKPROF `sectorHook=chained` in all 140 lines; 8-sector ideal speedup 3.2-4.0 (cf. 3.96 in the lead's run) |
| v1-b2-already-running | `--test-skip-release` while b1's X4 held the lock | box `Game is already running!` detected and closed, X4 exit 50, run failed cleanly (exit 7) in 2 s, lane restored |
| v1-b2-crash-victim, v1-b2-after-crash | runner hard-killed after READY | the kill-on-close job ended its X4 at once; `status` showed the lock STALE (owner pid gone); the next run took it over, warned `lane was left in phase running`, restored, passed |
| --timing | timing run with `--wait` | refusal path only: it waited while other X4 instances ran; not yet run holding quiet.lock (PC busy) |

Peak commit per lane X4: 8.8-9.0 GB (fresh custom_creative world). No save files were written,
no dialog other than the provoked one appeared, the foreground window stayed with the user's
application while lane X4 instances started (SW_SHOWNOACTIVATE). The autostart's `GetCurRealTime()`
restarts near 0 when the new world starts, so its 300 s menu deadline and the quit_after safety net
count from the latest world start.

## Open points

- `--timing` with quiet.lock held (own or adopted by handoff): validate when the PC is quiet.
- Session mode (assigned by the lead after Q6/Q7): implemented, offline-tested; native two-client
  loopback validation pending (the PC was reserved for the user's N-player test).
- Single runs keep the offline guard; only sessions allow loopback networking. Profile gates in
  source stay pinned to 45841; lane copies are re-pinned per run (sessions by default).
- `native/engine/build.py --fixedtick --test`: the DLL and fixedtick tests pass; `test_simprof`'s
  overhead assertion (suspended < 3.0 %) failed once at 3.25 % while this PC was busy (load-sensitive).
- `dev/lane/x4lock` is the project's x4lock tool, vendored unchanged from
  `X4-Multiplayer-fixed-tick/.local/evidence-243-withheld` (not in git there); it is built into
  `X4Mods/lanes/tools/x4lock-target` by `setup` or the first run.
