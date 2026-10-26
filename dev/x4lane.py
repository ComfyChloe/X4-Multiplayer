#!/usr/bin/env python3
"""Native X4 test lanes b1-b4: unattended disposable new-world runs on profiles 45850/45851/45853/45854.

  python dev/x4lane.py status [--json]
  python dev/x4lane.py setup [--lane b1|b2|b3|b4|all] [--check] [--refresh-base [--base-from DIR]]
  python dev/x4lane.py run --agent NAME --collect OUTDIR --timeout S [--lane b1|b2|b3|b4|auto]
         [--overlay DIR]... [--extension DIR]... [--until REGEX [--until-count N]] [--settle S]
         [--expect [N:]REGEX]... [--fail-on REGEX]... [--lua LINE]... [--ui-file ui/X.lua]...
         [--config KEY=VALUE]... [--gamestart ID] [--seed N] [--start module|menu]
         [--wait S] [--timing] [--base DIR] [--label TEXT] [--overwrite]
  python dev/x4lane.py make-overlay probe --out DIR [--root WORKTREE] [--mode MODE] [--dll FILE]
  python dev/x4lane.py make-overlay attnload --out DIR --attnload WORKTREE [--steps 0,8 ...]
  python dev/x4lane.py restore --lane LANE      (after a crashed runner; needs the lane lock)
  python dev/x4lane.py unlock --lane LANE       (removes a stale lane lock only)

Lane game views (C:/Users/<you>/Documents/X4Mods/lanes/<lane>/game-view) link the real X4
install through junctions and hardlinks and never write to it. A run refuses while
rewrite-coord/quiet.lock exists, holds rewrite-coord/lane-<lane>.lock, installs base
x4_lan_lab + overlays + x4lane_autostart, releases the single-instance lock of another
running X4 with x4lock, starts X4 itself, waits for --until/--timeout, closes only the X4
it started, collects the logs to OUTDIR, restores the lane and writes OUTDIR/result.json.
Usage and caveats: reports/NATIVE-LANES.md and rewrite-coord/native-lane.md.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dev' / 'lane'))
sys.path.insert(0, str(ROOT))
import lanecore as core  # noqa: E402
from lanecore import LaneError, Refused, UsageError  # noqa: E402

TOOL_VERSION = 1
LOG_NAME = 'x4lan-debug.txt'
POLL_S = 0.5
TRANSIENT_WAIT_S = 60
LAUNCH_LOCK_WAIT_S = 180
MUTEX_WAIT_S = 90
CLOSE_GRACE_S = 30
ALREADY_RUNNING = 'already running'


def win():
    import lanewin
    return lanewin


def tool_version() -> str:
    """The commit of this tool: lanes/release RELEASE.txt, or the development checkout's HEAD."""
    release = ROOT / 'RELEASE.txt'
    if release.is_file():
        first = release.read_text(encoding='utf-8').split('\n', 1)[0].split()
        return f'release {first[-1][:12]}' if first else 'release ?'
    try:
        head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], capture_output=True,
                              text=True, timeout=10).stdout.strip()
        return f'dev {head or "?"} ({ROOT})'
    except (OSError, subprocess.SubprocessError):
        return f'dev ? ({ROOT})'


def pinned_sha256() -> str:
    from native.engine.signatures import EXECUTABLE
    return EXECUTABLE['sha256'].lower()


# Protected X4 instances (user decision, 26 Oct 2026): the x4mods-61 session's two clients and the
# user's own game. Never closed, terminated or x4lock-ed by the lanes; a run waits while one of them
# holds the single-instance mutex (x4mods-61 frees it after starting its clients).
PROTECTED_PROFILES = ('45841', '45842')
PROTECTED_EXE_DIRS = (
    r'C:\Program Files (x86)\Steam\steamapps\common\X4 Foundations',
    r'C:\Users\developer\Documents\X4Mods\X4-Multiplayer-fixed-tick\.local\sandbox-baseline\guest\game-view',
)
INSTANCE_MUTEX = 'EGOSOFT_X4_INSTANCE'


def is_protected(proc) -> bool:
    """Protected: profile 45841/45842 on the command line, an exe in a protected folder (the real
    install, the x4mods-61 guest view), or an X4 whose image path cannot be read (fail closed)."""
    from pathlib import PureWindowsPath
    if core.profile_from_cmdline(getattr(proc, 'cmdline', None)) in PROTECTED_PROFILES:
        return True
    exe = getattr(proc, 'exe', None)
    if not exe:
        return True
    exe = PureWindowsPath(exe)
    for folder in PROTECTED_EXE_DIRS:
        folder = PureWindowsPath(folder)
        if exe == folder / 'X4.exe' or exe.is_relative_to(folder):
            return True
    return False


def describe_protected(procs) -> str:
    return ', '.join(f'{p.pid}/{core.profile_from_cmdline(p.cmdline) or "?"}' for p in procs) or 'unknown'


class LaunchFailed(LaneError):
    def __init__(self, message, reason='launch_failed'):
        super().__init__(message)
        self.reason = reason


class TransientRefusal(Refused):
    """A refusal that --wait may outlast (quiet.lock, busy lanes, low RAM)."""


class LaunchWait(TransientRefusal):
    """A start must wait (re-checked under the launch lock): the launch loops retry it."""


class ProtectedHolder(LaunchWait):
    """The single-instance mutex is held by a protected X4 (or a process we may not query)."""


class CapReached(LaunchWait):
    """MAX_X4_TOTAL X4 instances run, too little memory is available, or memory pressure is on."""


def admission(paths, instances, min_free_gb, who):
    """Raise CapReached unless `instances` more X4 may start now: every X4.exe on the PC counts
    towards MAX_X4_TOTAL, MIN_FREE_PER_START GB must be available per start, and no memory
    pressure episode may be active. Returns (x4 total, available GB) when admitted."""
    total = len(x4_processes())
    if total + instances > core.MAX_X4_TOTAL:
        raise CapReached(f'{total} X4 run on this PC; {instances} more would exceed MAX_X4_TOTAL='
                         f'{core.MAX_X4_TOTAL} (lanes, protected clients and other sessions all count)')
    _ram, avail = free_ram_gb()
    state = core.observe_pressure(paths.pressure, avail, who)
    if state and state.get('state') == 'pressure':
        raise CapReached(f'memory pressure: available stayed under {core.PRESSURE_LOW_GB:g} GB for more than '
                         f'{core.PRESSURE_HOLD_S} s (now {avail} GB); no new lane starts until it stays at or above '
                         f'{core.MIN_FREE_PER_START:g} GB for {core.PRESSURE_RECOVER_S} s')
    need = max(core.MIN_FREE_PER_START, min_free_gb) * instances
    if avail < need:
        raise CapReached(f'only {avail} GB RAM available (need {need:g} GB: '
                         f'{max(core.MIN_FREE_PER_START, min_free_gb):g} GB per X4 start)')
    return total, avail


# ---------------------------------------------------------------------------------------
# shared checks


def require_windows():
    if os.name != 'nt':
        raise Refused('native lanes need Windows (X4 install, x4lock, profiles on this PC)')


def lane_names(value):
    return list(core.LANE_PROFILES) if value in (None, 'all', 'auto') else [value]


def x4_processes():
    return win().processes('X4.exe')


def describe_x4(proc) -> dict:
    return {'pid': proc.pid, 'exe': proc.exe, 'profile': core.profile_from_cmdline(proc.cmdline)}


def free_ram_gb() -> tuple:
    total, avail = win().memory_status()
    return round(total / 2**30, 1), round(avail / 2**30, 1)


def workshop_items(paths) -> list:
    try:
        return sorted(p.name for p in paths.workshop.iterdir())
    except OSError:
        return []


def ensure_x4lock(paths, build=True) -> Path:
    exe = paths.x4lock_exe
    if exe.is_file():
        return exe
    if not build:
        raise Refused(f'x4lock not built ({exe}); run: python dev/x4lane.py setup')
    cargo = shutil.which('cargo')
    if not cargo:
        raise Refused('x4lock missing and cargo not found')
    manifest = ROOT / 'dev' / 'lane' / 'x4lock' / 'Cargo.toml'
    subprocess.run([cargo, 'build', '--release', '--quiet', '--manifest-path', str(manifest),
                    '--target-dir', str(paths.tools / 'x4lock-target')], check=True)
    if not exe.is_file():
        raise Refused(f'x4lock build produced no {exe}')
    return exe


def x4lock(paths, *args, timeout=60) -> tuple:
    proc = subprocess.run([str(ensure_x4lock(paths)), *map(str, args)], capture_output=True, text=True,
                          timeout=timeout)
    lines = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
    return proc.returncode, lines


def verify_view(paths, lane, check_hash=True) -> list:
    """Problems that make a lane view unusable (empty list = ok)."""
    problems = []
    if not lane.exe.is_file():
        return [f'lane {lane.name}: no game view at {lane.game} (python dev/x4lane.py setup)']
    if core.is_link(lane.game) or core.is_link(lane.extensions) or not lane.extensions.is_dir():
        problems.append(f'lane {lane.name}: game root or extensions folder is a link or missing')
    for name in ('X4.exe', '01.cat', '01.dat'):
        view, real = lane.game / name, paths.install / name
        try:
            if os.stat(view).st_ino != os.stat(real).st_ino:
                problems.append(f'lane {lane.name}: {name} is not linked to the current install file '
                                '(X4 was updated?); rebuild the view: setup --rebuild-view')
        except OSError as error:
            problems.append(f'lane {lane.name}: {name}: {error}')
    for dlc in core.dlc_ids(paths.install):
        if not core.is_link(lane.extensions / dlc):
            problems.append(f'lane {lane.name}: extensions/{dlc} junction missing')
    if check_hash and not problems:
        digest = core.sha256_file(lane.exe)
        if digest != pinned_sha256():
            problems.append(f'lane {lane.name}: X4.exe sha256 {digest} is not the pinned build')
    for name in ('config.xml', 'inputmap.xml', 'content.xml'):
        if not (lane.profile_base / name).is_file():
            problems.append(f'lane {lane.name}: profile base {name} missing (python dev/x4lane.py setup)')
    return problems


# ---------------------------------------------------------------------------------------
# setup


def junction(target: Path, source: Path):
    subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(target), str(source)], check=True,
                   capture_output=True)
    if not core.is_link(target):
        raise LaneError(f'junction {target} was not created')


def make_view(paths, lane, log):
    """Task-owned linked game view (pattern of save-trim/.local/loadtest/make_view.py)."""
    game = lane.game
    if os.path.lexists(game):
        log(f'lane {lane.name}: view exists ({game})')
        return
    install = paths.install
    if not (install / 'X4.exe').is_file():
        raise LaneError(f'no X4 install at {install}')
    game.mkdir(parents=True)
    for source in sorted(install.iterdir()):
        target = game / source.name
        if source.is_dir():
            if source.name != 'extensions':
                junction(target, source)
        elif source.name != 'steam_appid.txt':
            os.link(source, target)
    manifest = install.parent.parent / 'appmanifest_392160.acf'
    if manifest.is_file():
        shutil.copy2(manifest, lane.view / 'steamapps' / manifest.name)   # a copy, not a link
    extensions = game / 'extensions'
    extensions.mkdir()
    for dlc in core.dlc_ids(install):
        junction(extensions / dlc, install / 'extensions' / dlc)
    core.safe_write(game / 'steam_appid.txt', core.STEAM_APP_ID, lane.dir)
    reset_extensions(paths, lane, paths.base_lab)
    log(f'lane {lane.name}: view created ({game})')


def destroy_view(paths, lane, log):
    """Remove a lane view without touching the install: unlink hardlink names, remove
    junctions themselves, delete only lane-owned real directories."""
    game = lane.game
    if not os.path.lexists(game):
        return
    if core.is_link(game) or core.is_link(lane.view):
        raise core.UnsafeError(f'{game} is a link; refusing')
    for entry in list(game.iterdir()):
        if core.is_link(entry):
            if entry.is_dir():
                os.rmdir(entry)                               # removes the junction, not its target
            else:
                os.unlink(entry)
        elif entry.is_file():
            os.unlink(entry)                                  # only this name; install keeps its own
        elif entry.name == 'extensions':
            for item in list(entry.iterdir()):
                if core.is_link(item):
                    os.rmdir(item)
                elif item.is_dir():
                    core.safe_rmtree(item, lane.dir)
                else:
                    core.safe_unlink(item, lane.dir)
            entry.rmdir()
        else:
            raise core.UnsafeError(f'unexpected directory {entry} in a lane view; refusing')
    game.rmdir()
    log(f'lane {lane.name}: view removed')


def make_base(paths, source: Path, log, refresh=False):
    source = Path(source)
    core.check_base_lab(source)
    if paths.base_lab.exists():
        if not refresh:
            return
        core.safe_rmtree(paths.base_lab, paths.lanes)
    paths.base.mkdir(parents=True, exist_ok=True)
    core.copy_tree(source, paths.base_lab, paths.lanes)
    commit = None
    try:
        commit = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True,
                                text=True, timeout=30).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    core.write_json(paths.base / 'base.json', {
        'source': str(source), 'commit': commit, 'created': core.now_iso(),
        'files': core.tree_hashes(paths.base_lab)})
    log(f'base x4_lan_lab from {source} (commit {commit})')


def check_base(paths, base_lab=None) -> Path:
    base_lab = Path(base_lab) if base_lab else paths.base_lab
    core.check_base_lab(base_lab)
    if base_lab == paths.base_lab:
        record = json.loads((paths.base / 'base.json').read_text(encoding='utf-8'))
        if core.tree_hashes(base_lab) != record['files']:
            raise Refused(f'{base_lab} differs from base.json; refresh it: setup --refresh-base')
    return base_lab


def make_profile(paths, lane, log):
    if lane.profile not in core.LANE_PROFILES.values() or lane.profile in core.FOREIGN_PROFILES:
        raise core.UnsafeError(f'profile {lane.profile} is not a lane profile')
    lane.profile_base.mkdir(parents=True, exist_ok=True)
    source = paths.profiles / core.SOURCE_PROFILE
    if not (lane.profile_base / 'config.xml').is_file():
        text = (source / 'config.xml').read_text(encoding='utf-8-sig')       # read only
        core.safe_write(lane.profile_base / 'config.xml', core.lane_config_xml(text), lane.dir)
    if not (lane.profile_base / 'inputmap.xml').is_file():
        core.safe_write(lane.profile_base / 'inputmap.xml', (source / 'inputmap.xml').read_bytes(), lane.dir)
    if not (lane.profile_base / 'content.xml').is_file():
        core.safe_write(lane.profile_base / 'content.xml', core.lane_content_xml(core.dlc_ids(paths.install)),
                        lane.dir)
    if not lane.profile_dir.is_dir():
        lane.profile_dir.mkdir(parents=True)
        log(f'lane {lane.name}: profile {lane.profile_dir} created')
    restore_profile(lane)


def restore_profile(lane, config_overrides=None, extra_ids=(), dlcs=None):
    """Put the lane profile's config/content/inputmap back to the lane base (plus run options)."""
    root = lane.profile_dir
    if lane.profile not in core.LANE_PROFILES.values() or root.name != lane.profile:
        raise core.UnsafeError(f'{root} is not a lane profile')
    config = (lane.profile_base / 'config.xml').read_text(encoding='utf-8')
    config = core.apply_config_overrides(config, config_overrides or {})
    core.check_lane_config(config)
    core.safe_write(root / 'config.xml', config, root)
    core.safe_write(root / 'inputmap.xml', (lane.profile_base / 'inputmap.xml').read_bytes(), root)
    if extra_ids:
        content = core.lane_content_xml(dlcs or [], extra_ids)
    else:
        content = (lane.profile_base / 'content.xml').read_text(encoding='utf-8')
    core.safe_write(root / 'content.xml', content, root)


def reset_extensions(paths, lane, base_lab):
    """Lane extensions back to base: DLC junctions plus a fresh copy of the base x4_lan_lab."""
    extensions = lane.extensions
    core.assert_inside(extensions, lane.dir, 'extensions folder')
    if core.is_link(extensions):
        raise core.UnsafeError(f'{extensions} is a link')
    for item in list(extensions.iterdir()):
        if core.is_link(item):
            if not item.name.startswith('ego_dlc_'):
                raise core.UnsafeError(f'unexpected link {item} in lane extensions')
            continue
        if item.is_dir():
            core.safe_rmtree(item, lane.dir)
        else:
            core.safe_unlink(item, lane.dir)
    core.copy_tree(base_lab, lane.lab, lane.dir)


def cmd_setup(args, paths):
    require_windows()
    log = print
    names = lane_names(args.lane)
    if args.check:
        problems = []
        for name in names:
            problems += verify_view(paths, paths.lane(name))
        try:
            check_base(paths)
        except (LaneError, OSError, ValueError) as error:
            problems.append(f'base: {error}')
        for problem in problems:
            print('PROBLEM', problem)
        print('OK' if not problems else f'{len(problems)} problem(s)')
        return 0 if not problems else 2
    paths.lanes.mkdir(parents=True, exist_ok=True)
    if args.refresh_base or not paths.base_lab.exists():
        held = []
        try:
            if args.refresh_base:
                for name in core.LANE_PROFILES:
                    held.append(core.DirLock.acquire(paths.lane(name).lock, lock_info('setup', 'refresh base'),
                                                     win().pid_alive))
            make_base(paths, Path(args.base_from or ROOT / 'extension' / 'x4_lan_lab'), log, args.refresh_base)
        finally:
            for lock in held:
                lock.release()
    ensure_x4lock(paths)
    log(f'x4lock: {paths.x4lock_exe}')
    for name in names:
        lane = paths.lane(name)
        try:
            lock = core.DirLock.acquire(lane.lock, lock_info('setup', 'setup lane'), win().pid_alive)
        except core.LockBusy as busy:
            print(f'lane {name}: busy, skipped ({busy})')
            continue
        try:
            if any(p.exe and Path(p.exe) == lane.exe for p in x4_processes()):
                raise Refused(f'an X4 from lane {name} is running')
            if args.rebuild_view:
                destroy_view(paths, lane, log)
            make_view(paths, lane, log)
            make_profile(paths, lane, log)
        finally:
            lock.release()
        problems = verify_view(paths, lane)
        print(f'lane {name}: ' + ('ready' if not problems else '; '.join(problems)))
    return 0


HEAD_POLL_S = 10
QUEUE_POLL_S = 30
RESERVE_MAX_S = 600
PROTECTED_POLL_S = 10


def handoff_for(paths, args) -> bool:
    """quiet.lock hands the quiet window to this agent's --timing run (it bypasses the queue)."""
    if not getattr(args, 'timing', False) or not paths.quiet_lock.exists():
        return False
    chain = core.handoff_chain(core.read_owner(paths.quiet_lock))
    return bool(chain) and chain[0] == args.agent


def release_lock(lock, agent, warn):
    """Release one lock; a quiet.lock adopted from a handoff chain is handed to the next agent."""
    try:
        if core.hand_on(lock, agent):
            warn(f'{lock.path.name} handed on to the next agent of the handoff chain')
        elif not lock.release():
            warn(f'{lock.path.name} had been taken over; left in place')
    except Exception as error:                                       # noqa: BLE001
        warn(f'releasing {lock.path.name} failed: {error}')


def wait_in_queue(runner, attempt, give_back, need, purpose):
    """Run attempt() (raises TransientRefusal) under the lane queue. Without --wait: refuse while
    other runs are queued. With --wait: take a ticket; only the oldest live ticket attempts (every
    10 s, others check every 30 s); give_back() releases partial reservations when the wait ends."""
    paths, a = runner.paths, runner.args
    alive = win().pid_alive
    priority = handoff_for(paths, a)
    timing = bool(getattr(a, 'timing', False))
    if a.wait <= 0:
        # --timing waiters never block others: only the lead's quiet window gives them a quiet PC.
        tickets = [t for t in core.live_tickets(paths.queue, alive) if not t[1].get('timing')]
        if tickets and not priority:
            raise TransientRefusal(f'{len(tickets)} queued run(s) wait for lanes (oldest: agent '
                                   f'{tickets[0][1].get("agent")}, needs {tickets[0][1].get("need")}); '
                                   'use --wait S to queue behind them')
        attempt()
        return
    me = os.getpid()
    ticket = core.Ticket.create(paths.queue, {'agent': a.agent, 'purpose': purpose, 'need': need, 'pid': me,
                                              'pid_created': win().process_created(me),
                                              'machine': socket.gethostname(), 'timing': timing})
    deadline = time.monotonic() + a.wait
    last = None
    try:
        while True:
            tickets = core.live_tickets(paths.queue, alive)
            names = [path.name for path, _info in tickets]
            if ticket.path.name not in names:                     # deleted as stale: queue again
                ticket = core.Ticket.create(paths.queue, ticket.info)
                tickets.append((ticket.path, ticket.info))
                names.append(ticket.path.name)
            order = [path.name for path, info in tickets if not info.get('timing')]
            position = order.index(ticket.path.name) if not timing else names.index(ticket.path.name)
            if position == 0 or timing or priority or handoff_for(paths, a):
                try:
                    attempt()
                    return
                except TransientRefusal as error:
                    note, pause = str(error), HEAD_POLL_S
            else:
                note, pause = f'queued at position {position + 1} of {len(names)}', QUEUE_POLL_S
            if time.monotonic() + pause > deadline:
                give_back()
                raise TransientRefusal(f'--wait {a.wait:g} s elapsed ({note})')
            if note != last:
                print(f'x4lane: waiting ({note})', flush=True)
                last = note
            ticket.refresh(note=note[:300])
            runner.refresh_reserved()
            time.sleep(pause)
    except BaseException:
        give_back()
        raise
    finally:
        ticket.remove()


def acquire_launch_lock(paths, agent, purpose, run):
    deadline = time.monotonic() + LAUNCH_LOCK_WAIT_S
    while True:
        try:
            return core.DirLock.acquire(paths.launch_lock, lock_info(agent, purpose, run), win().pid_alive)
        except core.LockBusy:
            if time.monotonic() > deadline:
                raise LaunchFailed('lane-launch.lock stayed busy')
            time.sleep(1)


def lock_info(agent, purpose, run=None) -> dict:
    me = os.getpid()
    return {'agent': agent, 'purpose': purpose, 'start': core.now_iso(), 'tool': 'dev/x4lane.py',
            'run': run or f'{agent}-{me}-{int(time.time())}', 'machine': socket.gethostname(), 'pid': me,
            'pid_created': win().process_created(me)}


# ---------------------------------------------------------------------------------------
# status


def cmd_status(args, paths):
    require_windows()
    info = {'time': core.now_iso(), 'quiet_lock': None, 'lanes': {}, 'other_locks': [], 'x4': [],
            'ram_gb': None, 'steam': None, 'x4lock': str(paths.x4lock_exe) if paths.x4lock_exe.is_file() else None,
            'workshop_items': len(workshop_items(paths))}
    if paths.quiet_lock.exists():
        info['quiet_lock'] = core.read_owner(paths.quiet_lock) or {'owner': '(no owner.txt)'}
    for name in core.LANE_PROFILES:
        lane = paths.lane(name)
        entry = {'profile': lane.profile, 'lock': None, 'view': 'missing', 'state': None}
        if lane.lock.exists():
            owner = core.read_owner(lane.lock)
            stale = core.lock_is_stale(lane.lock, owner, win().pid_alive)
            entry['lock'] = dict(owner, stale=stale)
        if lane.exe.is_file():
            problems = verify_view(paths, lane, check_hash=False)
            entry['view'] = 'ok' if not problems else '; '.join(problems)
        if lane.state_file.exists():
            entry['state'] = json.loads(lane.state_file.read_text(encoding='utf-8')).get('phase')
        info['lanes'][name] = entry
    if paths.launch_lock.exists():
        info['launch_lock'] = core.read_owner(paths.launch_lock)
    info['queue'] = [ticket for _path, ticket in core.live_tickets(paths.queue, win().pid_alive)]
    if paths.coord.is_dir():
        for item in sorted(paths.coord.glob('*.lock')):
            if item.name not in ('quiet.lock', 'lane-launch.lock') and not item.name.startswith('lane-b'):
                info['other_locks'].append({item.name: core.owner_summary(core.read_owner(item))})
    for proc in x4_processes():
        entry = describe_x4(proc)
        entry['protected'] = is_protected(proc)
        for name in core.LANE_PROFILES:
            if proc.exe and Path(proc.exe) == paths.lane(name).exe:
                entry['lane'] = name
        info['x4'].append(entry)
    info['instance_mutex_present'] = win().named_object_exists(INSTANCE_MUTEX)
    try:
        info['pressure'] = json.loads(paths.pressure.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        info['pressure'] = None
    total, avail = free_ram_gb()
    info['ram_gb'] = {'total': total, 'free': avail}
    info['steam'] = win().steam_status()
    if args.json:
        print(json.dumps(info, indent=1, default=str))
        return 0
    print(f"X4 native lanes {info['time']}")
    quiet = info['quiet_lock']
    handoff = f"; handoff to agent {quiet['handoff']} (its --timing run may adopt it)" if quiet and quiet.get('handoff') else ''
    print('quiet.lock: ' + (f'HELD ({core.owner_summary(quiet)}){handoff} -> other lane runs are refused' if quiet else 'free'))
    for name, entry in info['lanes'].items():
        lock = entry['lock']
        state = 'free' if not lock else ('STALE ' if lock.get('stale') else 'held ') + core.owner_summary(lock)
        print(f"lane {name} (profile {entry['profile']}): {state}; view {entry['view']}"
              + (f"; last run phase {entry['state']}" if entry['state'] not in (None, 'idle') else ''))
    if 'launch_lock' in info:
        print('lane-launch.lock: held ' + core.owner_summary(info['launch_lock']))
    if info['queue']:
        print(f"lane queue: {len(info['queue'])} waiting (first: agent {info['queue'][0].get('agent')}, "
              f"needs {info['queue'][0].get('need')}); see x4lane.py queue")
    for item in info['other_locks']:
        print('other lock: ' + ', '.join(f'{k} ({v})' for k, v in item.items()))
    print(f"X4 processes: {len(info['x4'])}")
    for entry in info['x4']:
        lane = f" lane={entry['lane']}" if 'lane' in entry else ''
        mark = ' PROTECTED' if entry['protected'] else ''
        print(f"  pid={entry['pid']} profile={entry['profile']}{lane}{mark} exe={entry['exe']}")
    protected = [e for e in info['x4'] if e['protected']]
    holder = ('held; only protected X4 run, so a protected one holds it -> lane launches wait'
              if info['instance_mutex_present'] and protected and len(protected) == len(info['x4'])
              else 'present' if info['instance_mutex_present'] else 'free')
    print(f'instance mutex {INSTANCE_MUTEX}: {holder}')
    print(f"admission: {len(info['x4'])} of MAX_X4_TOTAL={core.MAX_X4_TOTAL} X4 running (all X4.exe count); "
          f"each start needs MIN_FREE_PER_START={core.MIN_FREE_PER_START:g} GB available")
    pressure = info.get('pressure')
    print('memory pressure: ' + (f"{pressure.get('state')} since {pressure.get('since') or pressure.get('low_since')} "
                                 f"(min {pressure.get('min_avail_gb')} GB) -> new lane starts held back"
                                 if pressure and pressure.get('state') == 'pressure' else
                                 f"low, watching (under {core.PRESSURE_LOW_GB:g} GB)" if pressure else 'none'))
    print(f"RAM: {avail} GB available of {total} GB")
    steam = info['steam']
    print('Steam: ' + (f"running pid={steam['pid']}" if steam['running'] else 'NOT running (runs are refused)'))
    print('x4lock: ' + (info['x4lock'] or 'not built (setup builds it)'))
    if info['workshop_items']:
        print(f"WARNING: {info['workshop_items']} X4 Workshop item(s) installed; runs refuse (--allow-workshop)")
    return 0


# ---------------------------------------------------------------------------------------
# run


class Run:
    def __init__(self, args, paths):
        self.args = args
        self.paths = paths
        self.t0 = time.monotonic()
        self.lane = None
        self.lock = None
        self.quiet = None
        self.launch_lock = None
        self.proc = None
        self.job = None
        self.engine_out = None
        self.watch = None
        self.t_launch = None
        self.outdir = None
        self.base_lab = None
        self.run_id = None
        self.dirty = False
        self.reason = None
        self.message = None
        self.other_seen = {}
        self.closing = None                 # (time, windows sent) after request_close()
        self.wait_deadline = 0.0            # set by acquire(): --wait also covers a protected mutex holder
        self.faults = None                  # hard page fault counter (sample_system)
        self.owns_quiet = False             # a session holds quiet.lock for all of its clients
        self.extra_tokens = {}              # session tokens (@X4LANE_PORT@, @X4LANE_SLOT@, ...)
        self.network = 'offline'            # 'loopback' only inside a session
        self.repin = bool(getattr(args, 'repin_profile', False))
        self.result = {
            'tool': 'dev/x4lane.py', 'tool_version': TOOL_VERSION, 'tool_commit': tool_version(),
            'run_id': None, 'agent': args.agent,
            'label': args.label, 'lane': None, 'profile': None, 'machine': socket.gethostname(),
            'started_at': core.now_iso(), 'verdict': None, 'exit_reason': None, 'exit_code': None,
            'message': None, 'command': sys.argv, 'warnings': [], 'timings': {}, 'x4': {},
            'other_x4': {'at_start': [], 'during_run': [], 'any': None},
            'quiet_lock': {'present_at_start': None, 'held_by_this_run': False},
            'timing_trustworthy': False, 'paths': {}, 'install': {}, 'until': None, 'expect': [],
            'fail_on': None, 'markers': {}, 'log': {}, 'dialogs': [], 'x4lock': [], 'lane_restored': None,
        }

    # -- helpers

    def elapsed(self):
        return round(time.monotonic() - self.t0, 3)

    def warn(self, text):
        self.result['warnings'].append(text)

    def mark(self, name):
        self.result['timings'][name] = self.elapsed()

    def state(self, phase, **extra):
        if self.lane is None:
            return
        data = {'phase': phase, 'run': self.run_id, 'agent': self.args.agent, 'runner_pid': os.getpid(),
                'updated': core.now_iso(), **extra}
        if self.proc is not None:
            data['x4_pid'] = self.proc.pid
        core.write_json(self.lane.state_file, data)

    # -- phases

    def validate(self):
        a = self.args
        require_windows()
        core.check_agent(a.agent)
        if not 1 <= a.timeout <= core.MAX_TIMEOUT_S:
            raise UsageError(f'--timeout must be 1..{core.MAX_TIMEOUT_S} s (lane locks are held at most 30 min)')
        if not 0 <= a.settle <= 120:
            raise UsageError('--settle must be 0..120 s')
        if a.until_count < 1:
            raise UsageError('--until-count must be >= 1')
        if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', a.gamestart):
            raise UsageError('--gamestart must be a gamestart id')
        if not 0 <= a.seed <= 2147483647:
            raise UsageError('--seed must be 0 (random world) .. 2147483647')
        stall, alive = getattr(a, 'stall', 0) or 0, getattr(a, 'alive', 0) or 0
        if stall < 0 or (stall > 0 and alive > 0 and stall < 3 * alive):
            raise UsageError('--stall must be 0 (off) or at least 3 x --alive')
        self.until = core.compile_regex(a.until, '--until') if a.until else None
        self.expect = [core.parse_expect(spec) for spec in a.expect]
        self.fail_on = [core.compile_regex(p, '--fail-on') for p in a.fail_on]
        for line in a.lua:
            if '\n' in line or '\r' in line:
                raise UsageError('--lua takes one line per option')
        self.overrides = {}
        for item in a.config:
            key, sep, value = item.partition('=')
            if not sep:
                raise UsageError(f'--config {item!r}: use KEY=VALUE')
            self.overrides[key] = value
        core.apply_config_overrides('<root/>', self.overrides)
        self.overlays = [Path(p).resolve() for p in a.overlay]
        for overlay in self.overlays:
            core.overlay_plan(overlay)
        self.extensions = []
        reserved = {core.LAB_ID, core.AUTOSTART_ID}
        for item in a.extension:
            path = Path(item).resolve()
            ident = core.extension_id(path)
            name = path.name
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) or name.startswith('ego_dlc_') \
                    or name in reserved or ident in reserved or ident.startswith(('ego_dlc_', 'ws_')):
                raise UsageError(f'--extension {path}: name/id {name}/{ident} is reserved or invalid')
            core.check_tree_unlinked(path)
            self.extensions.append((path, ident))
        if len({path.name for path, _ident in self.extensions}) != len(self.extensions):
            raise UsageError('--extension directory names must differ')
        self.base_lab = Path(a.base).resolve() if a.base else None
        self.outdir = core.check_outdir(a.collect, self.paths, a.overwrite)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.result['paths']['outdir'] = str(self.outdir)

    def general_checks(self, instances=1):
        """Checks before any lane is taken. Returns the quiet.lock owner (or None) and whether this
        run may adopt it (quiet-window handoff)."""
        paths, a = self.paths, self.args
        if paths.quiet_lock.exists():
            skipped = core.advance_handoff(paths.quiet_lock)
            if skipped:
                print(f'x4lane: quiet-window handoff chain: {skipped}', flush=True)
        quiet_owner = core.read_owner(paths.quiet_lock) if paths.quiet_lock.exists() else None
        chain = core.handoff_chain(quiet_owner)
        handoff = bool(quiet_owner is not None and a.timing and chain and chain[0] == a.agent)
        if quiet_owner is not None and not handoff:
            raise TransientRefusal('rewrite-coord/quiet.lock is held (' + core.owner_summary(quiet_owner)
                                   + (f", handoff to {quiet_owner['handoff']}" if quiet_owner.get('handoff') else '')
                                   + '); no X4 may be started now')
        steam = win().steam_status()
        if not steam['running']:
            raise Refused('the Steam client is not running/logged in; X4 needs it (the lanes never start Steam)')
        items = workshop_items(paths)
        if items and not a.allow_workshop:
            raise Refused(f'{len(items)} X4 Workshop item(s) are installed; they would load into lane worlds '
                          'and the lanes do not write Workshop selections (see the Workshop incident). '
                          'Use --allow-workshop only if that is acceptable for this run.')
        procs = x4_processes()
        protected = [p for p in procs if is_protected(p)]
        if protected and len(protected) == len(procs) and win().named_object_exists(INSTANCE_MUTEX):
            raise ProtectedHolder(f'instance mutex held by protected X4 {describe_protected(protected)}; waiting')
        admission(paths, instances, a.min_free_gb, f'admission {a.agent}')
        self.base_lab = check_base(paths, self.base_lab)
        ensure_x4lock(paths)
        return quiet_owner, handoff

    def usable_lanes(self, wanted):
        names, problems = [], []
        for name in wanted:
            found = verify_view(self.paths, self.paths.lane(name), check_hash=False)
            problems += found
            if not found:
                names.append(name)
        if not names:
            raise Refused('; '.join(problems))
        return names

    def take_lane(self, names):
        """Take the first free lane of names (lane lock, pinned X4.exe check); raises TransientRefusal."""
        a, busy = self.args, []
        for name in names:
            lane = self.paths.lane(name)
            run_id = core.run_id(name, a.agent)
            try:
                lock = core.DirLock.acquire(lane.lock, lock_info(a.agent, f'x4lane run {a.label or ""}'.strip(), run_id),
                                            win().pid_alive)
            except core.LockBusy as busy_error:
                busy.append(str(busy_error))
                continue
            self.lock, self.lane, self.run_id = lock, lane, run_id
            break
        if self.lock is None:
            raise TransientRefusal('no free lane: ' + '; '.join(busy))
        problems = verify_view(self.paths, self.lane, check_hash=True)
        if problems:
            self.give_back_lane()
            raise Refused('; '.join(problems))
        self.result.update(run_id=self.run_id, lane=self.lane.name, profile=self.lane.profile)

    def give_back_lane(self):
        if self.lock is not None:
            self.lock.release()
        self.lock = self.lane = None

    def preflight(self):
        """One attempt to get a lane; raises TransientRefusal/Refused."""
        paths, a = self.paths, self.args
        quiet_owner, handoff = self.general_checks()
        self.take_lane(self.usable_lanes(list(core.LANE_PROFILES) if a.lane == 'auto' else [a.lane]))
        try:
            if a.timing:
                others = [describe_x4(p) for p in x4_processes()]
                if others:
                    raise TransientRefusal(f'--timing needs no other X4 running: {others}')
                quiet_info = dict(
                    lock_info(a.agent, f'x4lane timing run lane {self.lane.name}', self.run_id),
                    owner=f'x4lane run {self.run_id} (agent {a.agent})',
                    reason='timing-sensitive lane measurement', since=core.now_iso(),
                    release='dev/x4lane.py removes it when the run ends')
                try:
                    if handoff:
                        self.quiet = core.DirLock.adopt(paths.quiet_lock, quiet_info, a.agent)
                        self.result['quiet_lock']['handed_from'] = self.quiet.info.get('handed_from')
                    else:
                        self.quiet = core.DirLock.acquire(paths.quiet_lock, quiet_info, take_stale=False)
                except core.LockBusy as error:
                    raise TransientRefusal(str(error))
                self.result['quiet_lock']['held_by_this_run'] = True
        except Exception:
            self.give_back_lane()
            raise
        self.result['quiet_lock']['present_at_start'] = quiet_owner is not None

    def selfcheck(self):
        """Quick lane self-check (no fixes, no hashing); findings other than ok go into result.json."""
        try:
            self.result['selfcheck'] = [f for f in doctor(self.paths, fix=False, check_hash=False)
                                        if f['level'] != 'ok']
        except Exception as error:                                   # noqa: BLE001 - never blocks a run
            self.result['selfcheck'] = [{'level': 'warn', 'what': 'selfcheck failed', 'detail': repr(error)}]

    def acquire(self):
        self.wait_deadline = time.monotonic() + max(0.0, self.args.wait)
        wait_in_queue(self, self.preflight, self.give_back_lane, 1, f'run {self.args.label}'.strip())

    def refresh_reserved(self):
        if self.lock is not None:
            self.lock.refresh()

    def kill_orphans(self):
        for proc in x4_processes():
            if proc.exe and Path(proc.exe) == self.lane.exe and not is_protected(proc):
                killed = win().terminate(proc.pid, self.lane.exe)
                self.warn(f'orphan X4 pid {proc.pid} of lane {self.lane.name} (a previous runner died): '
                          + ('terminated' if killed else 'could not be terminated'))
                time.sleep(3)

    def prepare(self):
        lane, a = self.lane, self.args
        self.dirty = True
        previous = None
        if lane.state_file.exists():
            try:
                previous = json.loads(lane.state_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                previous = {'phase': 'unreadable'}
        if previous and previous.get('phase') not in ('idle', None):
            self.warn(f"lane was left in phase {previous.get('phase')} by run {previous.get('run')}; restored")
        self.state('prepare')
        self.kill_orphans()
        core.safe_unlink(lane.dir / STOP_FILE, lane.dir)
        tokens = {'@X4LANE_LANE@': lane.name, '@X4LANE_PROFILE@': lane.profile,
                  '@X4LANE_RUN@': self.run_id, '@X4LANE_AGENT@': a.agent, **self.extra_tokens}
        reset_extensions(self.paths, lane, self.base_lab)
        lua_lines = [core.substitute_text(line, tokens) for line in a.lua]
        manifest = core.apply_overlays(lane.lab, self.overlays, lane.dir, tokens, a.ui_file, lua_lines)
        if self.repin:
            manifest['repinned'] = core.repin_profile(lane.lab, lane.profile, lane.dir)
        config_text = (lane.lab / 'ui' / 'config.lua').read_text(encoding='utf-8')
        if self.network == 'loopback':
            core.check_loopback_config(config_text)
        else:
            core.check_offline_config(config_text)
        core.copy_tree(self.paths.autostart_source, lane.extensions / core.AUTOSTART_ID, lane.dir)
        quit_after = round(a.timeout + a.settle + CLOSE_GRACE_S + 240, 1)
        settings = core.autostart_settings(
            enabled=True, lane=lane.name, profile=lane.profile, run=self.run_id, agent=a.agent,
            gamestart=a.gamestart, start=a.start, menu_fallback_s=60, menu_delay_s=3,
            menu_deadline_s=300, ready_gametime_s=5, alive_s=a.alive, quit_after_s=quit_after,
            mute=not a.sound)
        core.safe_write(lane.extensions / core.AUTOSTART_ID / 'ui' / 'settings.lua', settings, lane.dir)
        extension_ids = []
        for path, ident in self.extensions:
            core.copy_tree(path, lane.extensions / path.name, lane.dir, tokens)
            extension_ids.append(ident)
            manifest.setdefault('extensions', []).append({'dir': str(path), 'id': ident})
            if self.repin:
                manifest.setdefault('repinned_extensions', {})[path.name] = core.repin_profile(
                    lane.extensions / path.name, lane.profile, lane.dir)
        appid = lane.game / 'steam_appid.txt'
        if not appid.is_file() or appid.read_text(encoding='ascii', errors='replace').strip() != core.STEAM_APP_ID:
            core.safe_write(appid, core.STEAM_APP_ID, lane.dir)
        restore_profile(lane, self.overrides, [core.LAB_ID, core.AUTOSTART_ID] + extension_ids,
                        core.dlc_ids(self.paths.install))
        core.safe_unlink(lane.debug_log, lane.profile_dir)
        installed = {}
        for rel in ('ui.xml', 'ui/config.lua'):
            installed[rel] = core.sha256_file(lane.lab / rel)
        manifest['sha256'] = installed
        manifest['quit_after_s'] = quit_after
        self.result['install'] = {
            'base': str(self.base_lab), 'overlays': [str(p) for p in self.overlays],
            'base_commit': self.base_commit(), **manifest}
        snapshot = self.outdir / 'installed'
        snapshot.mkdir(exist_ok=True)
        shutil.copy2(lane.lab / 'ui.xml', snapshot / 'x4_lan_lab-ui.xml')
        shutil.copy2(lane.lab / 'ui' / 'config.lua', snapshot / 'x4_lan_lab-config.lua')
        shutil.copy2(lane.extensions / core.AUTOSTART_ID / 'ui' / 'settings.lua', snapshot / 'autostart-settings.lua')
        shutil.copy2(lane.profile_dir / 'content.xml', snapshot / 'profile-content.xml')
        shutil.copy2(lane.profile_dir / 'config.xml', snapshot / 'profile-config.xml')
        self.result['paths'].update(view=str(lane.game), profile_dir=str(lane.profile_dir),
                                    exe=str(lane.exe))
        self.mark('prepared_s')

    def base_commit(self):
        record = self.paths.base / 'base.json'
        if self.base_lab == self.paths.base_lab and record.is_file():
            return json.loads(record.read_text(encoding='utf-8')).get('commit')
        return None

    def launch_args(self):
        a, lane = self.args, self.lane
        args = [str(lane.exe), '-personalfolderid', lane.profile]
        if a.start == 'module':
            args += ['-module', a.gamestart]
        if a.seed:
            args.append(f'-seed:{a.seed}')
        args += ['-skipintro', '-debug', 'scripts', '-logfile', LOG_NAME, '-nocputhrottle', '-noworkshopsync']
        return args

    def holders(self, procs=None):
        """Non-protected X4 processes that hold EGOSOFT_X4_INSTANCE (x4lock query exit 0). Protected
        X4 instances are never queried."""
        out = []
        for proc in (x4_processes() if procs is None else procs):
            if is_protected(proc):
                continue
            code, _lines = x4lock(self.paths, 'query', '--pid', proc.pid)
            if code == 0:
                out.append(proc)
        return out

    def quiet_is_ours(self):
        return self.quiet is not None or self.owns_quiet

    def launch_x4(self):
        while True:
            self.launch_lock = acquire_launch_lock(self.paths, self.args.agent, f'launching lane {self.lane.name}',
                                                   self.run_id)
            try:
                self.start_sequence()
                return
            except LaunchWait as error:
                waiting = error
            finally:
                self.launch_lock.release()
                self.launch_lock = None
            if time.monotonic() + PROTECTED_POLL_S > self.wait_deadline:
                raise waiting
            print(f'x4lane: waiting ({waiting})', flush=True)
            self.refresh_reserved()
            time.sleep(PROTECTED_POLL_S)

    def start_sequence(self):
        """Release the instance lock of the X4 that holds it, start our X4, wait until it holds the
        lock itself. The caller holds rewrite-coord/lane-launch.lock."""
        paths = self.paths
        if paths.quiet_lock.exists() and not self.quiet_is_ours():
            raise Refused('quiet.lock appeared before launch; not starting X4')
        admission(paths, 1, self.args.min_free_gb, self.run_id)           # re-check under the launch lock
        procs = x4_processes()
        others = [describe_x4(p) for p in procs]
        self.result['other_x4']['at_start'] = others
        for entry in others:
            self.other_seen[entry['pid']] = entry
        protected = [p for p in procs if is_protected(p)]
        holders = [] if self.args.test_skip_release else self.holders(procs)
        if self.args.test_skip_release:
            self.warn('--test-skip-release: the instance lock was deliberately not released')
        if len(holders) > 1:
            raise LaunchFailed(f'several X4 hold the instance lock: {[p.pid for p in holders]}',
                               'lock_release_failed')
        if holders:
            holder = holders[0]
            code, lines = x4lock(paths, 'release', '--pid', holder.pid, '--expect-path', holder.exe,
                                 '--wait-ms', 30000)
            self.result['x4lock'] += [f'release pid {holder.pid} exit {code}'] + lines
            if code == 5 and protected:
                # the holder we may release is free now, but a protected client holds the name too
                raise ProtectedHolder(f'instance mutex also held by protected X4 {describe_protected(protected)}; '
                                      'waiting (it is never released by the lanes)')
            if code != 0:
                raise LaunchFailed(f'x4lock release of pid {holder.pid} failed (exit {code}); '
                                   'X4 not started (it would show "Game is already running!")',
                                   'lock_release_failed')
        # Presence by listing the object directory: the mutex itself is never opened.
        if win().named_object_exists(INSTANCE_MUTEX) and not self.args.test_skip_release:
            if protected:
                raise ProtectedHolder(f'instance mutex held by protected X4 {describe_protected(protected)}; '
                                      'waiting (it is never released by the lanes)')
            raise LaunchFailed(f'{INSTANCE_MUTEX} exists but no X4 the lanes may query holds it; X4 not started',
                               'lock_release_failed')
        self.mark('lock_released_s')
        self.start_x4()
        self.wait_for_mutex()

    def start_x4(self):
        args = self.launch_args()
        self.result['x4']['args'] = args[1:]
        self.engine_out = open(self.outdir / 'engine-output.log', 'wb')
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 4                                   # SW_SHOWNOACTIVATE: do not steal focus
        self.job = win().KillOnCloseJob()
        self.state('launching')
        self.proc = subprocess.Popen(args, cwd=str(self.lane.game), stdin=subprocess.DEVNULL,
                                     stdout=self.engine_out, stderr=subprocess.STDOUT, startupinfo=startup,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            self.job.assign(int(self.proc._handle))
        except OSError as error:
            self.warn(f'could not assign X4 to a kill-on-close job ({error}); an orphan is possible')
        self.t_launch = time.monotonic()
        self.mark('launched_s')
        self.result['x4']['pid'] = self.proc.pid
        self.result['x4']['exe'] = str(self.lane.exe)
        self.state('running')
        self.watch = core.LogWatcher(self.lane.debug_log, self.until, self.args.until_count, self.fail_on,
                                     self.expect)
        self.watch.t0 = self.t0                                   # all times: seconds since runner start

    def check_dialogs(self):
        for dialog in win().dialogs_of(self.proc.pid):
            text = ' | '.join([dialog['title']] + dialog['texts'])
            if text not in self.result['dialogs']:
                self.result['dialogs'].append(text)
            if ALREADY_RUNNING in text.lower():
                win().close_dialog(dialog['hwnd'])
                raise LaunchFailed('X4 showed "Game is already running!" (the instance lock was not free); '
                                   'dialog closed', 'already_running')

    def wait_for_mutex(self):
        deadline = time.monotonic() + MUTEX_WAIT_S
        while True:
            if self.proc.poll() is not None:
                code = self.proc.returncode
                why = 'the instance lock existed (exit 50)' if code == 50 else 'Steam start-up check failed?' if code == 1 else ''
                raise LaunchFailed(f'X4 exited during start-up with code {code} {why}'.strip(),
                                   'already_running' if code == 50 else 'launch_failed')
            self.check_dialogs()
            code, _lines = x4lock(self.paths, 'query', '--pid', self.proc.pid)
            if code == 0:
                self.mark('mutex_s')
                return
            if time.monotonic() > deadline:
                raise LaunchFailed(f'X4 did not create its instance lock within {MUTEX_WAIT_S} s')
            time.sleep(POLL_S)

    def sample(self):
        procs = x4_processes()
        for proc in procs:
            if self.proc is not None and proc.pid == self.proc.pid:
                handle = win().open_process(proc.pid, win().PROCESS_QUERY_LIMITED_INFORMATION)
                if handle:
                    with handle as h:
                        memory = win().memory_counters(h)
                    if memory:
                        self.result['x4']['memory'] = memory
                continue
            if proc.pid not in self.other_seen:
                entry = describe_x4(proc)
                entry['first_seen_s'] = self.elapsed()
                self.other_seen[proc.pid] = entry
                self.result['other_x4']['during_run'].append(entry)
        self.sample_system(procs)

    def sample_system(self, procs):
        """Machine-wide sample (every 10 s): available RAM, X4 count and working sets, hard faults,
        and the shared memory-pressure state (this run only observes; it never stops anything)."""
        try:
            _ram, avail = free_ram_gb()
            state = core.observe_pressure(self.paths.pressure, avail, self.run_id or 'run')
            working = win().working_sets_gb([p.pid for p in procs])
            if self.faults is None:
                self.faults = win().HardFaultCounter()
            row = {'t': self.elapsed(), 'avail_gb': avail, 'x4_total': len(procs),
                   'hard_fault_pages_per_s': self.faults.read(),
                   'own_ws_gb': working.get(self.proc.pid) if self.proc is not None else None,
                   'ws_gb': {str(pid): gb for pid, gb in working.items()},
                   'pressure': state.get('state') if state else None}
        except Exception as error:                                   # noqa: BLE001 - sampling never fails a run
            row = {'t': self.elapsed(), 'error': repr(error)}
            state = None
        samples = self.result.setdefault('samples', [])
        samples.append(row)
        del samples[:-400]
        if state and state.get('state') == 'pressure' and 'pressure' not in self.result:
            self.result['pressure'] = {'seen_s': self.elapsed(), 'since': state.get('since'),
                                       'min_avail_gb': state.get('min_avail_gb')}
            self.warn(f"memory pressure on this PC (available under {core.PRESSURE_LOW_GB:g} GB for more than "
                      f"{core.PRESSURE_HOLD_S} s): new lane starts are held back; this run continues")

    def begin_monitor(self):
        self.mon = {'start': time.monotonic(), 'dialog': 0.0, 'sample': 0.0, 'refresh': 0.0}

    def poll(self, now):
        """One monitoring step: debug log, dialogs (2 s), other X4/memory (10 s), lock refresh (60 s).
        Returns (reason, message) when this X4 failed or exited, else None."""
        m = self.mon
        self.watch.poll()
        if now >= m['dialog']:
            m['dialog'] = now + 2
            self.check_dialogs()
        if now >= m['sample']:
            m['sample'] = now + 10
            self.sample()
            if self.paths.quiet_lock.exists() and not self.quiet_is_ours() \
                    and 'quiet.lock appeared during the run' not in self.result['warnings']:
                self.warn('quiet.lock appeared during the run')
        if now >= m['refresh']:
            m['refresh'] = now + 60
            self.lock.refresh()
            if self.quiet:
                self.quiet.refresh()
        if self.watch.failed_line is not None:
            return 'fail_pattern', f'--fail-on matched: {self.watch.failed_line}'
        stall = (getattr(self.args, 'stall', 0) or 0) if (getattr(self.args, 'alive', 0) or 0) > 0 else 0
        if stall > 0 and 'autostart_ingame' in self.watch.markers and self.watch.last_line_at is not None \
                and self.proc.poll() is None and now - self.watch.last_line_at > stall:
            hung = win().window_hung(self.proc.pid)
            return 'stalled', (f'no debug log line for {now - self.watch.last_line_at:.0f} s after INGAME '
                               f'(--stall {stall:g}); window ' + ('not responding' if hung else 'responding')
                               + ' - X4 hung, minimized or frozen')
        if self.proc.poll() is not None:
            self.watch.flush()
            stop = self.stop_request()
            if stop is not None:
                return 'stopped', (f"stopped by x4lane stop (by {stop.get('by')}: "
                                   f"{stop.get('reason') or 'no reason given'})")
            quit_marker = 'autostart_quit' in self.watch.markers
            return 'x4_exited', (f'X4 exited with code {self.proc.returncode}'
                                 + (' after the autostart quit_after safety net' if quit_marker else ''))
        return None

    def stop_request(self):
        """The `x4lane.py stop` marker for our X4, if one was written."""
        if self.lane is None or self.proc is None:
            return None
        try:
            data = json.loads((self.lane.dir / STOP_FILE).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return data if data.get('pid') == self.proc.pid else None

    def monitor(self):
        a = self.args
        self.begin_monitor()
        start = self.mon['start']
        settle_until = None
        while True:
            now = time.monotonic()
            ended = self.poll(now)
            if ended:
                if ended[0] in ('x4_exited', 'stalled', 'stopped') and settle_until is not None:
                    return 'until_matched', f'--until matched; then during --settle: {ended[1]}'
                return ended
            if settle_until is None and self.watch.until_matched:
                self.mark('until_s')
                settle_until = now + a.settle
            if settle_until is not None and now >= settle_until:
                return 'until_matched', f'--until matched: {self.watch.until_line}'
            if settle_until is None and now - start >= a.timeout:
                if self.until is None:
                    return 'duration_elapsed', f'--timeout {a.timeout} s elapsed (no --until)'
                return 'timeout', f'--until not matched within {a.timeout} s ({self.watch.until_hits} hit(s))'
            time.sleep(POLL_S)

    def request_close(self):
        """Post WM_CLOSE to our X4 (exe path checked); close_x4() waits and terminates if needed."""
        proc = self.proc
        if proc is None or proc.poll() is not None or getattr(self, 'closing', None) is not None:
            return
        exe = None
        handle = win().open_process(proc.pid, win().PROCESS_QUERY_LIMITED_INFORMATION)
        if handle:
            with handle as h:
                exe = win().image_path(h)
        if exe is None or Path(exe) != self.lane.exe:
            self.warn(f'pid {proc.pid} image {exe} is not the lane X4; terminating through our own handle')
        sent = win().post_close(proc.pid) if exe and Path(exe) == self.lane.exe else 0
        self.closing = (time.monotonic(), sent)

    def close_x4(self):
        x4 = self.result['x4']
        proc = self.proc
        if proc is None:
            return
        self.request_close()
        if getattr(self, 'closing', None) is None:
            x4.setdefault('closed', 'exited by itself')
        else:
            closed_at, sent = self.closing
            grace = (self.args.close_grace if sent else 0.1) - (time.monotonic() - closed_at)
            try:
                proc.wait(timeout=max(0.1, grace))
                x4['closed'] = 'graceful (WM_CLOSE)'
                x4['close_s'] = round(time.monotonic() - closed_at, 2)
            except subprocess.TimeoutExpired:
                proc.kill()                                         # TerminateProcess on our own handle
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.warn(f'X4 pid {proc.pid} did not exit after TerminateProcess')
                x4['closed'] = 'forced (TerminateProcess)' + ('' if sent else ', no window')
                x4['close_s'] = round(time.monotonic() - closed_at, 2)
        x4['exit_code'] = proc.returncode
        if self.job is not None:
            peak = self.job.peak_memory_gb()
            if peak is not None:
                x4['peak_process_memory_gb'] = peak
        self.mark('closed_s')

    def collect(self):
        lane, out = self.lane, self.outdir
        if self.engine_out is not None:
            self.engine_out.close()
            self.engine_out = None
        if out is None:
            return
        if self.watch is not None:
            self.watch.flush()
            self.result['log'] = {'lines': self.watch.lines, 'errors': self.watch.errors,
                                  'bytes': self.watch.offset, 'fps': self.watch.fps_summary()}
            rows = [row for row in self.result.get('samples', []) if 'avail_gb' in row]
            if rows:
                faults = [row['hard_fault_pages_per_s'] for row in rows if row.get('hard_fault_pages_per_s') is not None]
                own = [row['own_ws_gb'] for row in rows if row.get('own_ws_gb') is not None]
                self.result['machine'] = {
                    'samples': len(rows), 'min_avail_gb': min(row['avail_gb'] for row in rows),
                    'max_x4_total': max(row['x4_total'] for row in rows),
                    'max_own_ws_gb': max(own) if own else None,
                    'max_hard_fault_pages_per_s': max(faults) if faults else None,
                    'pressure_seen': any(row.get('pressure') == 'pressure' for row in rows)}
            self.result['markers'] = self.watch.markers
            self.result['expect'] = self.watch.expect_report()
            if self.until is not None:
                self.result['until'] = {'regex': self.until.pattern, 'count': self.args.until_count,
                                        'hits': self.watch.until_hits, 'line': self.watch.until_line,
                                        's': self.watch.until_at}
            if self.watch.failed_line is not None:
                self.result['fail_on'] = {'regex': self.watch.failed_regex, 'line': self.watch.failed_line}
            timings = self.result['timings']
            for key, marker in (('ingame_s', 'autostart_ingame'), ('ready_s', 'autostart_ready'),
                                ('first_log_s', 'log_started'), ('game_start_s', 'game_start')):
                if marker in self.watch.markers:
                    timings[key] = self.watch.markers[marker]['s']
            if 'ingame_s' in timings and 'launched_s' in timings:
                timings['launch_to_ingame_s'] = round(timings['ingame_s'] - timings['launched_s'], 3)
        if lane is not None and lane.debug_log.is_file():
            shutil.copy2(lane.debug_log, out / LOG_NAME)
            self.result['paths']['debug_log'] = str(out / LOG_NAME)
        if (out / 'engine-output.log').is_file():
            self.result['paths']['engine_output'] = str(out / 'engine-output.log')
        if lane is not None and lane.profile_dir.is_dir() and self.proc is not None:
            started = time.time() - (time.monotonic() - self.t_launch) - 5
            extra = out / 'profile-files'
            for item in sorted(lane.profile_dir.rglob('*')):
                if item.is_file() and item.name != LOG_NAME and item.suffix.lower() in ('.dmp', '.txt', '.log') \
                        and item.stat().st_mtime >= started and item.stat().st_size < 200 * 2**20:
                    extra.mkdir(exist_ok=True)
                    shutil.copy2(item, extra / item.name)
        saves = lane.profile_dir / 'save' if lane is not None else None
        if saves is not None and saves.is_dir() and self.proc is not None:
            new = [p.name for p in saves.iterdir() if p.stat().st_mtime >= time.time() - self.elapsed()]
            if new:
                self.warn(f'the run wrote save files in its lane profile: {new}')

    def restore(self):
        if self.lane is None or not self.dirty:
            return
        try:
            if self.proc is not None and self.proc.poll() is None:
                raise LaneError('X4 still running; lane not restored')
            reset_extensions(self.paths, self.lane, self.paths.base_lab)
            restore_profile(self.lane)
            core.safe_unlink(self.lane.dir / STOP_FILE, self.lane.dir)
            self.state('idle', restored=core.now_iso())
            self.result['lane_restored'] = True
            self.dirty = False
        except Exception as error:                                   # recorded, never masks the result
            self.result['lane_restored'] = False
            self.warn(f'lane restore failed: {error}')

    def release(self):
        for attr in ('launch_lock', 'quiet', 'lock'):
            lock = getattr(self, attr)
            if lock is not None:
                release_lock(lock, self.args.agent, self.warn)
                setattr(self, attr, None)
        if self.job is not None:
            self.job.close()
            self.job = None

    def finish(self, reason, message):
        r = self.result
        if reason == 'until_matched' or reason == 'duration_elapsed':
            if self.watch is not None and not self.watch.expectations_ok():
                missing = [e for e in self.watch.expect_report() if not e['ok']]
                reason, message = 'expect_failed', f'expectations not met: {missing}'
        code = {'until_matched': 0, 'duration_elapsed': 0, 'usage': 1, 'refused': 2, 'timeout': 3,
                'x4_exited': 4, 'fail_pattern': 5, 'expect_failed': 6, 'launch_failed': 7,
                'already_running': 7, 'lock_release_failed': 7, 'error': 8, 'interrupted': 9, 'stopped': 9, 'stalled': 3}[reason]
        others = list(self.other_seen.values())
        r['other_x4']['any'] = bool(others)
        r['timing_trustworthy'] = bool(self.result['quiet_lock']['held_by_this_run'] and not others
                                       and reason in ('until_matched', 'duration_elapsed', 'expect_failed'))
        r['timing_note'] = ('quiet.lock held by this run and no other X4 seen' if r['timing_trustworthy'] else
                            'functional/differential result only: timings are not trustworthy '
                            '(no --timing quiet.lock, or other X4 instances ran)')
        r.update(verdict='pass' if code == 0 else 'fail', exit_reason=reason, exit_code=code, message=message,
                 finished_at=core.now_iso())
        r['timings']['total_s'] = self.elapsed()
        return code

    def execute(self) -> int:
        reason, message = 'error', None
        try:
            self.validate()
            self.selfcheck()
            self.acquire()
            self.prepare()
            self.launch_x4()
            reason, message = self.monitor()
        except UsageError as error:
            reason, message = 'usage', str(error)
        except Refused as error:
            reason, message = 'refused', str(error)
        except LaunchFailed as error:
            reason, message = error.reason, str(error)
        except KeyboardInterrupt:
            reason, message = 'interrupted', 'interrupted (Ctrl-C)'
        except Exception as error:                                   # noqa: BLE001 - reported in result.json
            reason, message = 'error', f'{type(error).__name__}: {error}'
            self.result['traceback'] = traceback.format_exc()
        finally:
            for step in (self.close_x4, self.collect, self.restore, self.release):
                try:
                    step()
                except Exception as error:                           # noqa: BLE001
                    self.warn(f'{step.__name__} failed: {type(error).__name__}: {error}')
        code = self.finish(reason, message)
        if self.outdir is not None:
            core.write_json(self.outdir / 'result.json', self.result)
        r = self.result
        print(f"X4LANE RESULT verdict={r['verdict']} reason={r['exit_reason']} lane={r['lane']} "
              f"total={r['timings'].get('total_s')}s ingame={r['timings'].get('ingame_s')}s "
              f"trustworthy_timing={r['timing_trustworthy']} outdir={self.outdir}")
        if message:
            print(f'X4LANE MESSAGE {message}')
        for warning in r['warnings']:
            print(f'X4LANE WARNING {warning}')
        return code


def cmd_run(args, paths):
    return Run(args, paths).execute()


# ---------------------------------------------------------------------------------------
# session: N clients (lanes) at once in one loopback session, optional server process


class NetworkViolation(LaneError):
    pass


class ServerFailed(LaneError):
    pass


SERVER_TOKENS = ('port', 'token', 'build', 'bubble', 'clients', 'session', 'outdir', 'python', 'repo')
ATOM = re.compile(r'[A-Za-z0-9_.-]{1,96}')


def loopback_endpoint(entry) -> bool:
    """A server endpoint is allowed only on loopback (TCP peers on loopback too, or listening)."""
    if entry['proto'] in ('tcp', 'udp'):
        ok = entry['local'] == core.LOOPBACK
        return ok and (entry['proto'] == 'udp' or entry['remote'] in ('0.0.0.0', core.LOOPBACK))
    ok = entry['local'] == '::1'
    return ok and (entry['proto'] == 'udp6' or entry['remote'] in ('::', '::1'))


def server_command(template, values) -> str:
    command = template
    for key in SERVER_TOKENS:
        command = command.replace('{' + key + '}', str(values[key]))
    leftover = re.findall(r'\{(\w+)\}', command)
    if leftover:
        raise UsageError(f'--server-cmd: unknown placeholder(s) {leftover}; known: {list(SERVER_TOKENS)}')
    return command


class Server:
    """The caller's relay/server process of a session: started before the clients, kept in a
    kill-on-close job (with every process it starts), checked for non-loopback endpoints."""

    def __init__(self, command, cwd, outdir, port, ready, until, fail_on, t0):
        self.command, self.cwd, self.port, self.t0 = command, Path(cwd), port, t0
        self.log_path = Path(outdir) / 'server.log'
        self.ready, self.until, self.fail_on = ready, until, fail_on
        self.proc = self.job = self.log = self.watch = self.ready_watch = None
        self.result = {'command': command, 'cwd': str(cwd), 'port': port, 'bind': core.LOOPBACK,
                       'log': str(self.log_path), 'endpoints': [], 'violations': []}

    def start(self, env):
        self.log = open(self.log_path, 'wb')
        self.job = win().KillOnCloseJob()
        self.proc = subprocess.Popen(self.command, cwd=str(self.cwd), stdin=subprocess.DEVNULL, stdout=self.log,
                                     stderr=subprocess.STDOUT, env=env,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        self.job.assign(int(self.proc._handle))                   # processes it starts join the job
        self.result['pid'] = self.proc.pid
        self.watch = core.LogWatcher(self.log_path, until=self.until, fail_on=self.fail_on)
        self.watch.t0 = self.t0
        if self.ready is not None:
            self.ready_watch = core.LogWatcher(self.log_path, until=self.ready)

    def pids(self):
        try:
            pids = self.job.pids()
        except OSError:
            pids = []
        if not pids and self.proc is not None and self.proc.poll() is None:
            pids = [self.proc.pid]
        return pids

    def check_network(self):
        found = win().endpoints(self.pids())
        if found:
            self.result['endpoints'] = found
        bad = [e for e in found if not loopback_endpoint(e)]
        if bad:
            self.result['violations'] = bad
            raise NetworkViolation(f'the server has endpoint(s) outside loopback: {bad}')
        return found

    def wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                raise ServerFailed(f'the server exited with code {self.proc.returncode} before it was ready '
                                   f'(see {self.log_path})')
            found = self.check_network()
            if self.ready_watch is not None:
                self.ready_watch.poll()
                if self.ready_watch.until_matched:
                    return
            elif any(e['lport'] == self.port for e in found):
                return
            if time.monotonic() > deadline:
                raise ServerFailed(f'the server was not ready within {timeout:g} s (no '
                                   + ('--server-ready match' if self.ready is not None
                                      else f'endpoint on {core.LOOPBACK}:{self.port}') + ')')
            time.sleep(0.25)

    def poll(self):
        self.watch.poll()
        if self.watch.failed_line is not None:
            return 'fail_pattern', f'--server-fail-on matched: {self.watch.failed_line}'
        if self.proc.poll() is not None:
            return 'server_failed', f'the server exited with code {self.proc.returncode} during the session'
        return None

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.job.terminate(1)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
            self.result['stopped'] = 'terminated by the runner'
        if self.proc is not None:
            self.result['exit_code'] = self.proc.returncode
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.watch is not None:
            self.watch.flush()
            self.result['log_lines'] = self.watch.lines
            if self.until is not None:
                self.result['until'] = {'regex': self.until.pattern, 'line': self.watch.until_line,
                                        's': self.watch.until_at}
        if self.job is not None:
            self.job.close()
            self.job = None


def per_client(values, option, clients):
    out = {index: [] for index in range(1, clients + 1)}
    for value in values:
        match = re.match(r'^(\d+):(.+)$', value, re.S)
        if not match or not 1 <= int(match.group(1)) <= clients:
            raise UsageError(f'{option} {value!r}: use N:VALUE with N = 1..{clients}')
        out[int(match.group(1))].append(match.group(2))
    return out


class SessionRun:
    """N lanes (clients) at once: all lane locks for the whole run, per-client overlays/config, one
    --gamestart/--seed, loopback-only networking, an optional server on a free 127.0.0.1 port."""

    def __init__(self, args, paths):
        self.args, self.paths = args, paths
        self.t0 = time.monotonic()
        self.clients, self.server, self.quiet, self.launch_lock, self.outdir = [], None, None, None, None
        self.session_id = None
        self.port = None
        self.reserved_since = None
        self.wait_deadline = 0.0
        self.result = {
            'tool': 'dev/x4lane.py', 'tool_version': TOOL_VERSION, 'tool_commit': tool_version(),
            'kind': 'session', 'session_id': None,
            'agent': args.agent, 'label': args.label, 'machine': socket.gethostname(), 'started_at': core.now_iso(),
            'verdict': None, 'exit_reason': None, 'exit_code': None, 'message': None, 'command': sys.argv,
            'clients': [], 'server': None, 'network': {}, 'until': None, 'timings': {}, 'warnings': [],
            'quiet_lock': {'present_at_start': None, 'held_by_this_run': False}, 'timing_trustworthy': False,
            'other_x4': {'seen': [], 'any': None},
        }

    def elapsed(self):
        return round(time.monotonic() - self.t0, 3)

    def mark(self, name):
        self.result['timings'][name] = self.elapsed()

    def warn(self, text):
        self.result['warnings'].append(text)

    def client_args(self, index, per):
        a = self.args
        return argparse.Namespace(
            agent=a.agent, label=f'{a.label} client{index}'.strip(), collect=str(self.outdir / f'client{index}'),
            overwrite=a.overwrite, overlay=list(a.overlay) + per['overlay'][index],
            extension=list(a.extension) + per['extension'][index], base=a.base,
            lua=list(a.lua) + per['lua'][index], ui_file=list(a.ui_file), config=list(a.config) + per['config'][index],
            until=a.until, until_count=a.until_count, timeout=a.timeout, settle=a.settle, expect=list(a.expect),
            fail_on=list(a.fail_on), gamestart=a.gamestart, seed=a.seed, start=a.start, alive=a.alive,
            sound=a.sound, wait=0, timing=a.timing, min_free_gb=a.min_free_gb, close_grace=a.close_grace, stall=a.stall,
            allow_workshop=a.allow_workshop, test_skip_release=False, lane='auto', repin_profile=not a.no_repin)

    def validate(self):
        a, paths = self.args, self.paths
        require_windows()
        core.check_agent(a.agent)
        if not 2 <= a.clients <= 3:
            raise UsageError('--clients must be 2 or 3')
        for value, option in ((a.build, '--build'), (a.bubble, '--bubble')):
            if not ATOM.fullmatch(value):
                raise UsageError(f'{option} must be 1..96 characters of letters, digits, _ . -')
        if a.until_mode not in ('all', 'any'):
            raise UsageError('--until-mode must be all or any')
        self.lanes = None
        if a.lanes:
            self.lanes = [name.strip() for name in a.lanes.split(',') if name.strip()]
            if len(self.lanes) != a.clients or len(set(self.lanes)) != a.clients \
                    or any(name not in core.LANE_PROFILES for name in self.lanes):
                raise UsageError(f'--lanes needs {a.clients} different lanes of {list(core.LANE_PROFILES)}')
        self.outdir = core.check_outdir(a.collect, paths, a.overwrite)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.server_until = core.compile_regex(a.server_until, '--server-until') if a.server_until else None
        self.server_ready = core.compile_regex(a.server_ready, '--server-ready') if a.server_ready else None
        self.server_fail = [core.compile_regex(p, '--server-fail-on') for p in a.server_fail_on]
        if (a.server_until or a.server_ready or a.server_fail_on) and not a.server_cmd:
            raise UsageError('--server-until/--server-ready/--server-fail-on need --server-cmd')
        self.server_cwd = Path(a.server_cwd).resolve() if a.server_cwd else ROOT
        if a.server_cmd:
            server_command(a.server_cmd, {k: 0 for k in SERVER_TOKENS})          # placeholder check
            if not self.server_cwd.is_dir():
                raise UsageError(f'--server-cwd {self.server_cwd} is not a directory')
        per = {'overlay': per_client(a.client_overlay, '--client-overlay', a.clients),
               'extension': per_client(a.client_extension, '--client-extension', a.clients),
               'lua': per_client(a.client_lua, '--client-lua', a.clients),
               'config': per_client(a.client_config, '--client-config', a.clients)}
        self.session_id = core.run_id('session', a.agent)
        self.result['session_id'] = self.session_id
        for index in range(1, a.clients + 1):
            client = Run(self.client_args(index, per), paths)
            client.network = 'loopback'
            client.validate()
            self.clients.append(client)
        self.result['paths'] = {'outdir': str(self.outdir)}

    def give_back_all(self):
        for client in self.clients:
            client.give_back_lane()
        self.reserved_since = None

    def refresh_reserved(self):
        """While queued: keep reserved lanes alive, but give them back after RESERVE_MAX_S (the queue
        head keeps its place, so no newer run can take lanes ahead of it anyway)."""
        held = [c for c in self.clients if c.lock is not None]
        if not held:
            self.reserved_since = None
            return
        if self.reserved_since is None:
            self.reserved_since = time.monotonic()
        if time.monotonic() - self.reserved_since > RESERVE_MAX_S:
            names = [c.lane.name for c in held]
            self.give_back_all()
            print(f'x4lane: gave back reserved lanes {names} after {RESERVE_MAX_S // 60} min; still first in the queue',
                  flush=True)
            return
        for client in held:
            client.refresh_reserved()

    def preflight(self, keep=False):
        """Take a lane per client. keep=True (queued with --wait): lanes taken so far stay reserved
        when another one is still busy, so the session cannot be starved by single runs."""
        a, first = self.args, self.clients[0]
        quiet_owner, handoff = first.general_checks(instances=a.clients)
        for client in self.clients[1:]:
            client.base_lab = first.base_lab
        usable = first.usable_lanes(self.lanes or list(core.LANE_PROFILES))
        if self.lanes and usable != self.lanes:
            raise Refused(f'lanes {sorted(set(self.lanes) - set(usable))} are not usable')
        try:
            for index, client in enumerate(self.clients):
                if client.lock is not None:
                    continue                                      # reserved in an earlier attempt
                held = {c.lane.name for c in self.clients if c.lock is not None}
                choice = [self.lanes[index]] if self.lanes else [n for n in usable if n not in held]
                try:
                    client.take_lane(choice)
                except TransientRefusal as error:
                    reserved = [c.lane.name for c in self.clients if c.lock is not None]
                    if keep and reserved:
                        raise TransientRefusal(f'{error} (reserved so far: {reserved})')
                    raise
            if a.timing:
                others = [describe_x4(p) for p in x4_processes()]
                if others:
                    raise TransientRefusal(f'--timing needs no other X4 running: {others}')
                info = dict(lock_info(a.agent, f'x4lane timing session {self.session_id}', self.session_id),
                            owner=f'x4lane session {self.session_id} (agent {a.agent})',
                            reason='timing-sensitive lane session', since=core.now_iso(),
                            release='dev/x4lane.py removes it when the session ends')
                try:
                    self.quiet = (core.DirLock.adopt(self.paths.quiet_lock, info, a.agent) if handoff else
                                  core.DirLock.acquire(self.paths.quiet_lock, info, take_stale=False))
                except core.LockBusy as error:
                    raise TransientRefusal(str(error))
                self.result['quiet_lock']['held_by_this_run'] = True
                for client in self.clients:
                    client.owns_quiet = True
        except TransientRefusal:
            if not keep:
                self.give_back_all()
            raise
        except Exception:
            self.give_back_all()
            raise
        self.result['quiet_lock']['present_at_start'] = quiet_owner is not None

    def acquire(self):
        self.wait_deadline = time.monotonic() + max(0.0, self.args.wait)
        wait_in_queue(self, lambda: self.preflight(keep=self.args.wait > 0), self.give_back_all,
                      self.args.clients, f'session {self.args.label}'.strip())

    def prepare(self):
        a = self.args
        self.port = core.free_loopback_port()
        token = secrets.token_hex(16)
        self.values = {'port': self.port, 'token': token, 'build': a.build, 'bubble': a.bubble,
                       'clients': a.clients, 'session': self.session_id, 'outdir': str(self.outdir),
                       'python': f'"{sys.executable}"', 'repo': str(ROOT)}
        self.result['network'] = {'bind': core.LOOPBACK, 'port': self.port, 'build': a.build, 'bubble': a.bubble,
                                  'token_sha256_12': hashlib.sha256(token.encode()).hexdigest()[:12]}
        for index, client in enumerate(self.clients, 1):
            client.extra_tokens = {
                '@X4LANE_PORT@': str(self.port), '@X4LANE_TOKEN@': token, '@X4LANE_BUILD@': a.build,
                '@X4LANE_BUBBLE@': a.bubble, '@X4LANE_CLIENT@': str(index), '@X4LANE_CLIENTS@': str(a.clients),
                '@X4LANE_SLOT@': f'p{index}', '@X4LANE_SESSION@': self.session_id}
            client.prepare()
        self.mark('prepared_s')

    def start_server(self):
        a = self.args
        if not a.server_cmd:
            return
        command = server_command(a.server_cmd, self.values)
        env = dict(os.environ, PYTHONUNBUFFERED='1', X4LANE_PORT=str(self.port), X4LANE_TOKEN=self.values['token'],
                   X4LANE_BUILD=a.build, X4LANE_BUBBLE=a.bubble, X4LANE_SESSION=self.session_id)
        self.server = Server(command, self.server_cwd, self.outdir, self.port, self.server_ready, self.server_until,
                             self.server_fail, self.t0)
        self.server.result['command'] = command.replace(self.values['token'], '<token>')
        self.server.start(env)
        self.server.wait_ready(a.server_ready_timeout)
        self.mark('server_ready_s')

    def launch(self):
        for client in self.clients:
            while True:
                self.launch_lock = acquire_launch_lock(self.paths, self.args.agent,
                                                       f'launching session {self.session_id}', self.session_id)
                try:
                    client.start_sequence()
                    break
                except LaunchWait as error:
                    waiting = error
                finally:
                    self.launch_lock.release()
                    self.launch_lock = None
                if time.monotonic() + PROTECTED_POLL_S > self.wait_deadline:
                    raise waiting
                print(f'x4lane: waiting ({waiting})', flush=True)
                for other in self.clients:
                    other.refresh_reserved()
                time.sleep(PROTECTED_POLL_S)
        self.mark('launched_s')

    def passed(self):
        a = self.args
        if a.until is None and self.server_until is None:
            return False
        if a.until is not None:
            hits = [c.watch.until_matched for c in self.clients]
            if not (all(hits) if a.until_mode == 'all' else any(hits)):
                return False
        return self.server_until is None or self.server.watch.until_matched

    def monitor(self):
        a = self.args
        for client in self.clients:
            client.begin_monitor()
        start, settle_until, next_network = time.monotonic(), None, 0.0
        while True:
            now = time.monotonic()
            for index, client in enumerate(self.clients, 1):
                ended = client.poll(now)
                if ended and settle_until is None:
                    return ended[0], f'client {index} (lane {client.lane.name}): {ended[1]}'
            if self.server is not None:
                ended = self.server.poll()
                if ended and settle_until is None:
                    return ended
                if now >= next_network:
                    next_network = now + 5
                    self.server.check_network()
            if settle_until is None and self.passed():
                self.mark('until_s')
                settle_until = now + a.settle
            if settle_until is not None and now >= settle_until:
                return 'until_matched', 'session pass condition met (--until ' + a.until_mode + \
                    (', --server-until' if self.server_until is not None else '') + ')'
            if settle_until is None and now - start >= a.timeout:
                if a.until is None and self.server_until is None:
                    return 'duration_elapsed', f'--timeout {a.timeout:g} s elapsed (no --until)'
                hits = [c.watch.until_hits for c in self.clients]
                return 'timeout', f'pass condition not met within {a.timeout:g} s (client --until hits {hits})'
            time.sleep(POLL_S)

    def shutdown(self):
        steps = [client.request_close for client in self.clients] + [client.close_x4 for client in self.clients]
        if self.server is not None:
            steps.append(self.server.stop)
        for client in self.clients:
            steps += [client.collect, client.restore]
        steps += [client.release for client in self.clients]
        for step in steps:
            try:
                step()
            except Exception as error:                               # noqa: BLE001
                self.warn(f'{getattr(step, "__name__", step)} failed: {type(error).__name__}: {error}')
        for attr in ('launch_lock', 'quiet'):
            lock = getattr(self, attr)
            if lock is not None:
                release_lock(lock, self.args.agent, self.warn)
                setattr(self, attr, None)
        self.mark('closed_s')

    def finish(self, reason, message):
        r, a = self.result, self.args
        if reason in ('until_matched', 'duration_elapsed'):
            missing = [(i, [e for e in c.watch.expect_report() if not e['ok']])
                       for i, c in enumerate(self.clients, 1) if c.watch is not None and not c.watch.expectations_ok()]
            if missing:
                reason, message = 'expect_failed', f'expectations not met: {missing}'
        code = {'until_matched': 0, 'duration_elapsed': 0, 'usage': 1, 'refused': 2, 'timeout': 3,
                'x4_exited': 4, 'fail_pattern': 5, 'network_violation': 5, 'expect_failed': 6,
                'launch_failed': 7, 'already_running': 7, 'lock_release_failed': 7, 'server_failed': 7,
                'error': 8, 'interrupted': 9, 'stopped': 9, 'stalled': 3}[reason]
        members = {c.proc.pid for c in self.clients if c.proc is not None}
        seen = {}
        for client in self.clients:
            for pid, entry in client.other_seen.items():
                if pid not in members:
                    seen[pid] = entry
        r['other_x4'] = {'seen': list(seen.values()), 'any': bool(seen)}
        r['timing_trustworthy'] = bool(r['quiet_lock']['held_by_this_run'] and not seen
                                       and reason in ('until_matched', 'duration_elapsed', 'expect_failed'))
        r['timing_note'] = ('quiet.lock held by this session and no other X4 seen' if r['timing_trustworthy'] else
                            'functional result only: timings are not trustworthy')
        if a.until is not None:
            r['until'] = {'regex': a.until, 'count': a.until_count, 'mode': a.until_mode,
                          'hits': [c.watch.until_hits if c.watch else 0 for c in self.clients]}
        for index, client in enumerate(self.clients, 1):
            cr = client.result
            cr.update(client=index, slot=f'p{index}', session_id=self.session_id, verdict='pass' if code == 0 else 'fail',
                      exit_reason=reason, exit_code=code, message=message, finished_at=core.now_iso())
            cr['timings']['total_s'] = client.elapsed()
            if client.outdir is not None:
                core.write_json(client.outdir / 'result.json', cr)
            r['clients'].append(cr)
        r['server'] = self.server.result if self.server is not None else None
        r.update(verdict='pass' if code == 0 else 'fail', exit_reason=reason, exit_code=code, message=message,
                 finished_at=core.now_iso())
        r['timings']['total_s'] = self.elapsed()
        return code

    def execute(self) -> int:
        reason, message = 'error', None
        try:
            self.validate()
            self.clients[0].selfcheck()
            self.result['selfcheck'] = self.clients[0].result.get('selfcheck')
            self.acquire()
            self.prepare()
            self.start_server()
            self.launch()
            reason, message = self.monitor()
        except UsageError as error:
            reason, message = 'usage', str(error)
        except Refused as error:
            reason, message = 'refused', str(error)
        except LaunchFailed as error:
            reason, message = error.reason, str(error)
        except ServerFailed as error:
            reason, message = 'server_failed', str(error)
        except NetworkViolation as error:
            reason, message = 'network_violation', str(error)
        except KeyboardInterrupt:
            reason, message = 'interrupted', 'interrupted (Ctrl-C)'
        except Exception as error:                                   # noqa: BLE001
            reason, message = 'error', f'{type(error).__name__}: {error}'
            self.result['traceback'] = traceback.format_exc()
        finally:
            self.shutdown()
        code = self.finish(reason, message)
        if self.outdir is not None:
            core.write_json(self.outdir / 'result.json', self.result)
        r = self.result
        lanes = ','.join(c.lane.name if c.lane else '-' for c in self.clients) or '-'
        print(f"X4LANE SESSION verdict={r['verdict']} reason={r['exit_reason']} lanes={lanes} "
              f"port={self.port} total={r['timings'].get('total_s')}s outdir={self.outdir}")
        if message:
            print(f'X4LANE MESSAGE {message}')
        for warning in r['warnings'] + [w for c in self.clients for w in c.result['warnings']]:
            print(f'X4LANE WARNING {warning}')
        return code


def cmd_session(args, paths):
    return SessionRun(args, paths).execute()


# ---------------------------------------------------------------------------------------
# restore / unlock


def cmd_restore(args, paths):
    require_windows()
    lane = paths.lane(args.lane)
    lock = core.DirLock.acquire(lane.lock, lock_info('restore', f'restore lane {lane.name}'), win().pid_alive)
    try:
        for proc in x4_processes():
            if proc.exe and Path(proc.exe) == lane.exe:
                raise Refused(f'X4 pid {proc.pid} of lane {lane.name} is running; close it first')
        reset_extensions(paths, lane, paths.base_lab)
        restore_profile(lane)
        core.write_json(lane.state_file, {'phase': 'idle', 'restored': core.now_iso(), 'by': 'restore'})
        print(f'lane {lane.name} restored')
    finally:
        lock.release()
    return 0


def doctor(paths, fix=False, check_hash=True):
    """Self-check of the lanes. Returns findings {level: ok|warn|problem|fixed, what, detail}.
    fix=True repairs only what is safe: stale lane/launch locks, dirty lanes nobody holds, orphan
    lane X4 instances (exe path checked), leftover stop markers, temp and staging files."""
    out = []

    def note(level, what, detail=''):
        out.append({'level': level, 'what': what, 'detail': str(detail)})

    alive = win().pid_alive
    if paths.quiet_lock.exists() and fix:
        advanced = core.advance_handoff(paths.quiet_lock)
        if advanced:
            note('fixed', 'quiet-window handoff chain', advanced)
    if paths.quiet_lock.exists():
        note('warn', 'quiet.lock held', core.owner_summary(core.read_owner(paths.quiet_lock)))
    if not win().steam_status()['running']:
        note('problem', 'Steam client not running (runs are refused)')
    if not paths.x4lock_exe.is_file():
        if fix:
            ensure_x4lock(paths)
            note('fixed', 'x4lock built', paths.x4lock_exe)
        else:
            note('problem', 'x4lock not built', paths.x4lock_exe)
    if workshop_items(paths):
        note('warn', 'X4 Workshop items installed (runs are refused)', len(workshop_items(paths)))
    try:
        check_base(paths)
    except (LaneError, OSError, ValueError) as error:
        note('problem', 'base x4_lan_lab', error)
    if not (paths.lanes / 'release' / 'RELEASE.txt').is_file():
        note('warn', 'no lanes/release export (lane owner: x4lane.py publish-release)')
    procs = x4_processes()
    for name in core.LANE_PROFILES:
        lane = paths.lane(name)
        for problem in verify_view(paths, lane, check_hash=check_hash):
            note('problem', f'lane {name} view', problem)
        owner = core.read_owner(lane.lock) if lane.lock.exists() else None
        stale = core.lock_is_stale(lane.lock, owner, alive) if owner is not None else None
        if owner is not None and stale:
            if fix:
                core._remove_lock_dir(lane.lock, stale)
                note('fixed', f'lane {name}: stale lock removed', stale)
                owner = None
            else:
                note('problem', f'lane {name}: stale lock', stale)
        held = owner is not None and not stale
        orphans = [p for p in procs if p.exe and Path(p.exe) == lane.exe and not is_protected(p)]
        if orphans and not held:
            for proc in orphans:
                if fix and win().terminate(proc.pid, lane.exe):
                    note('fixed', f'lane {name}: orphan X4 pid {proc.pid} terminated')
                else:
                    note('problem', f'lane {name}: orphan X4 pid {proc.pid} without a runner')
        state = None
        if lane.state_file.exists():
            try:
                state = json.loads(lane.state_file.read_text(encoding='utf-8')).get('phase')
            except (OSError, ValueError):
                state = 'unreadable'
        extras = [p.name for p in lane.extensions.iterdir() if not core.is_link(p) and p.name != core.LAB_ID] \
            if lane.extensions.is_dir() else []
        dirty = (state not in (None, 'idle')) or extras or (lane.dir / STOP_FILE).exists()
        if dirty and not held:
            if fix and not any(p.exe and Path(p.exe) == lane.exe for p in x4_processes()):
                try:
                    lock = core.DirLock.acquire(lane.lock, lock_info('doctor', f'restore lane {name}'), alive)
                except core.LockBusy:
                    note('warn', f'lane {name}: dirty but just taken by a run; left alone')
                else:
                    try:
                        reset_extensions(paths, lane, paths.base_lab)
                        restore_profile(lane)
                        core.safe_unlink(lane.dir / STOP_FILE, lane.dir)
                        core.write_json(lane.state_file, {'phase': 'idle', 'restored': core.now_iso(),
                                                          'by': 'doctor'})
                        note('fixed', f'lane {name}: restored to base', f'phase {state}, extras {extras}')
                    finally:
                        lock.release()
            else:
                note('problem', f'lane {name}: not in base state while free', f'phase {state}, extras {extras}')
        temps = []                    # lane-owned folders only; never walk the junctions into the install
        for folder, pattern in ((lane.profile_base, '*.x4lane-tmp-*'), (lane.profile_dir, '*.x4lane-tmp-*'),
                                (lane.game, '*.x4lane-tmp-*'), (lane.dir, '*.x4lane-tmp-*'),
                                (lane.lab, '**/*.x4lane-tmp-*')):
            if folder.is_dir() and not core.is_link(folder):
                temps += [(item, folder) for item in folder.glob(pattern) if item.is_file()]
        for item, folder in temps:
            if fix:
                core.safe_unlink(item, folder)
                note('fixed', f'lane {name}: temp file removed', item.name)
            else:
                note('warn', f'lane {name}: temp file left', item)
        if not held and not dirty and not orphans:
            note('ok', f'lane {name}: free and clean')
        elif held:
            note('ok', f'lane {name}: in use', core.owner_summary(owner))
    if paths.launch_lock.exists():
        owner = core.read_owner(paths.launch_lock)
        stale = core.lock_is_stale(paths.launch_lock, owner, alive)
        if stale and fix:
            core._remove_lock_dir(paths.launch_lock, stale)
            note('fixed', 'stale lane-launch.lock removed', stale)
        elif stale:
            note('problem', 'stale lane-launch.lock', stale)
    leftovers = [*paths.coord.glob('*.lock.removing-*')] if paths.coord.is_dir() else []
    leftovers += [*paths.lanes.glob('release.new-*'), *paths.lanes.glob('release.old-*'),
                  *paths.lanes.glob('release-*.new-*')]
    for item in leftovers:
        if fix:
            core.check_tree_unlinked(item)
            shutil.rmtree(item)
            note('fixed', 'leftover removed', item)
        else:
            note('warn', 'leftover from an interrupted step', item)
    tickets = core.live_tickets(paths.queue, alive)
    if tickets:
        note('ok', f'{len(tickets)} run(s) queued', ', '.join(str(t.get('agent')) for _p, t in tickets))
    return out


def cmd_canary(args, paths):
    """Native self-test: one queued run without overlays until the world runs (AUTOSTART READY).
    Results go to X4Mods/lanes/canary/<time>-<lane> (the last 10 are kept); exit code as for run."""
    root = paths.lanes / 'canary'
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    argv = ['run', '--agent', args.agent, '--label', 'canary', '--lane', args.lane, '--timeout', str(args.timeout),
            '--wait', str(args.wait), '--until', r'\[X4LANE\] AUTOSTART READY',
            '--expect', r'\[X4LANE\] AUTOSTART INGAME .*match=true', '--collect', str(root / f'{stamp}-{args.lane}')]
    code = Run(parser().parse_args(argv), paths).execute()
    runs = sorted(p for p in root.iterdir() if p.is_dir())
    for old in runs[:-10]:
        core.check_tree_unlinked(old)
        shutil.rmtree(old)
    with open(root / 'canary.log', 'a', encoding='utf-8') as log:
        log.write(f'{core.now_iso()} lane={args.lane} exit={code} dir={stamp}-{args.lane} {tool_version()}\n')
    return code


def cmd_doctor(args, paths):
    require_windows()
    findings = doctor(paths, fix=args.fix)
    if args.json:
        print(json.dumps(findings, indent=1))
    else:
        for item in findings:
            print(f"{item['level'].upper():8} {item['what']}" + (f" - {item['detail']}" if item['detail'] else ''))
    return 1 if any(item['level'] == 'problem' for item in findings) else 0


def cmd_publish_release(args, paths):
    """Lane owner: export a committed, tested version of this branch to X4Mods/lanes/release, which
    the other agents run (so edits in the development worktree never reach a running agent)."""
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--verify', args.commit + '^{commit}'],
                            capture_output=True, text=True, check=True).stdout.strip()
    version = paths.lanes / f'release-{commit[:12]}'
    if not version.exists():
        staging = paths.lanes / f'release-{commit[:12]}.new-{os.getpid()}'
        staging.mkdir(parents=True)
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', commit], capture_output=True,
                                 check=True).stdout
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(staging, filter='data')
        (staging / 'RELEASE.txt').write_text(f'x4lane release {commit}\nexported {core.now_iso()} from {ROOT}\n'
                                             'Read only: run python dev\\x4lane.py from here. Do not edit.\n',
                                             encoding='utf-8')
        retry(lambda: os.replace(staging, version), f'rename {staging.name}')     # fresh files may be scanned
    link = paths.lanes / 'release'
    if os.path.lexists(link) and not core.is_link(link):                          # legacy plain folder
        legacy = paths.lanes / f'release-legacy-{time.strftime("%Y%m%d-%H%M%S")}'
        retry(lambda: os.replace(link, legacy), 'move the legacy release folder')
    if core.is_link(link):
        os.rmdir(link)                                                           # removes the junction only
    junction(link, version)                                                      # path is missing for ~ms
    keep = {version.name}
    versions = sorted((p for p in paths.lanes.glob('release-*') if p.is_dir() and not core.is_link(p)
                       and '.new-' not in p.name), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in versions[3:]:
        if old.name not in keep:
            try:
                core.check_tree_unlinked(old)
                shutil.rmtree(old)
            except OSError as error:
                print(f'note: could not remove {old.name} ({error}); doctor --fix retries later')
    print(f'{link} -> {version.name} = {commit}')
    return 0


def retry(action, what, attempts=60, pause=0.5):
    """Windows briefly locks fresh files (virus scan, indexer): retry a rename for up to 30 s."""
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise LaneError(f'could not {what}: access denied for {attempts * pause:g} s')
            time.sleep(pause)


STOP_FILE = 'stop-request.json'


def cmd_stop(args, paths):
    """Gracefully close lane X4 instances (exe path must be a lane view's X4.exe; no other X4 is
    touched). Their runners notice, report exit_reason 'stopped', collect and restore the lanes."""
    require_windows()
    if args.all_lanes:
        names = list(core.LANE_PROFILES)
    else:
        names = [n.strip() for n in (args.lane or '').split(',') if n.strip()]
        if not names or any(n not in core.LANE_PROFILES for n in names):
            raise UsageError(f'--lane takes lanes of {list(core.LANE_PROFILES)} (comma list) or use --all-lanes')
    targets = []
    for proc in x4_processes():
        for name in names:
            lane = paths.lane(name)
            if proc.exe and Path(proc.exe) == lane.exe and not is_protected(proc):
                targets.append((lane, proc))
    if not targets:
        print(f'no lane X4 running in {names}')
        return 0
    for lane, proc in targets:
        core.write_json(lane.dir / STOP_FILE, {'pid': proc.pid, 'by': args.by, 'reason': args.reason,
                                               'time': core.now_iso()})
        sent = win().post_close(proc.pid)
        print(f'lane {lane.name}: WM_CLOSE to X4 pid {proc.pid} ({sent} window(s))')
    deadline = time.monotonic() + args.grace
    pending = list(targets)
    while pending and time.monotonic() < deadline:
        time.sleep(1)
        pending = [(lane, proc) for lane, proc in pending if win().pid_alive(proc.pid, proc.created)]
    for lane, proc in pending:
        if args.force:
            killed = win().terminate(proc.pid, lane.exe)
            print(f'lane {lane.name}: X4 pid {proc.pid} still running after {args.grace:g} s; '
                  + ('terminated' if killed else 'could NOT be terminated'))
        else:
            print(f'lane {lane.name}: X4 pid {proc.pid} still running after {args.grace:g} s (use --force)')
    stopped = len(targets) - (0 if args.force else len(pending))
    print(f'stopped {stopped} of {len(targets)} lane X4 instance(s); their runners collect and restore the lanes')
    return 0 if stopped == len(targets) else 3


def cmd_queue(args, paths):
    require_windows()
    tickets = core.live_tickets(paths.queue, win().pid_alive)
    if args.json:
        print(json.dumps([dict(info, file=path.name) for path, info in tickets], indent=1, default=str))
        return 0
    if not tickets:
        print('lane queue: empty (runs take a free lane directly)')
        return 0
    print(f'lane queue: {len(tickets)} waiting, oldest first (only the first may take lanes)')
    for number, (_path, info) in enumerate(tickets, 1):
        print(f"  {number}. agent={info.get('agent')} need={info.get('need')} purpose={info.get('purpose')} "
              f"since={info.get('created')} pid={info.get('pid')} note={info.get('note', '')}")
    return 0


def cmd_unlock(args, paths):
    require_windows()
    lane = paths.lane(args.lane)
    if not lane.lock.exists():
        print(f'{lane.lock.name}: not held')
        return 0
    owner = core.read_owner(lane.lock)
    reason = core.lock_is_stale(lane.lock, owner, win().pid_alive)
    if not reason:
        print(f'{lane.lock.name} is held and not stale ({core.owner_summary(owner)}); not removed')
        return 2
    core._remove_lock_dir(lane.lock, reason)
    print(f'{lane.lock.name} removed ({reason}); run "x4lane.py restore --lane {lane.name}" next')
    return 0


# ---------------------------------------------------------------------------------------
# overlays


def fresh_overlay_dir(out: Path, force: bool) -> Path:
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        if not force or not (out / 'x4lane-overlay.txt').is_file():
            raise UsageError(f'{out} is not empty (use --force to replace an overlay made by make-overlay)')
        core.safe_rmtree(out, out.parent)
    out.mkdir(parents=True, exist_ok=True)
    return out


def copy_into(src: Path, out: Path, rel: str, note: list):
    if not Path(src).is_file():
        raise UsageError(f'missing {src}')
    dest = out / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    note.append(f'{rel} <- {src} sha256={core.sha256_file(dest)}')


GUARD_OLD = ("assert(cfg.attention_load_profile=='45841' or cfg.attention_load_profile=='45842',"
             "'unsupported attention load profile')")
GUARD_NEW = ("assert(cfg.attention_load_profile=='45841' or cfg.attention_load_profile=='45842' or "
             + ' or '.join(f"cfg.attention_load_profile=='{p}'" for p in core.LANE_PROFILES.values())
             + ",'unsupported attention load profile')")


def cmd_make_overlay(args, paths):
    out = fresh_overlay_dir(args.out, args.force)
    note = [f'x4lane overlay "{args.kind}" made {core.now_iso()} by dev/x4lane.py make-overlay', '']
    if args.kind == 'probe':
        root = Path(args.root or ROOT).resolve()
        copy_into(Path(args.dll) if args.dll else root / '.local' / 'engine' / 'x4lan_fixedtick_64.dll', out,
                  'native/x4lan_fixedtick_64.dll', note)
        copy_into(root / 'dev' / 'fixed_tick_probe.lua', out, 'ui/fixed_tick_probe.lua', note)
        (out / 'ui.xml.files').write_text('ui/fixed_tick_probe.lua\n', encoding='utf-8')
        if args.mode != 'probe':
            (out / 'ui' / 'config.lua.append').write_text(
                f"X4LAN_FIXEDTICK_MODE = '{args.mode}' -- x4lane probe overlay\n", encoding='utf-8')
        note += ['', f'mode: {args.mode}. Expect "[X4LAN] FRAMEPROBE armed" and per-second FRAMEPROBE/WALKPROF lines.']
    elif args.kind == 'session-telemetry':
        root = Path(args.root or ROOT).resolve()
        copy_into(Path(args.dll) if args.dll else root / '.local' / 'engine' / 'x4lan_simtick_64.dll', out,
                  'native/x4lan_simtick_64.dll', note)
        (out / 'ui').mkdir(parents=True, exist_ok=True)
        (out / 'ui' / 'config.lua.append').write_text('\n'.join([
            '-- x4lane session-telemetry overlay (test only): the 7.x telemetry adapter of every client',
            '-- talks to the session server on 127.0.0.1; slot p1..pN follows the client number.',
            'X4LAN_CONFIG.enabled = true',
            'X4LAN_CONFIG.host = "127.0.0.1"',
            'X4LAN_CONFIG.port = @X4LANE_PORT@',
            'X4LAN_CONFIG.token = "@X4LANE_TOKEN@"',
            'X4LAN_CONFIG.build = "@X4LANE_BUILD@"',
            'X4LAN_CONFIG.bubble = "@X4LANE_BUBBLE@"',
            'X4LAN_CONFIG.slot = "@X4LANE_SLOT@"',
            'X4LAN_CONFIG.mode = "telemetry"', '']), encoding='utf-8')
        note += ['', 'Session use: x4lane session --clients 2 --overlay <this> --server-cmd '
                 '"{python} -m x4lan.host --bind 127.0.0.1 --port {port} --token {token} --build {build} '
                 '--bubble {bubble}" (server cwd: a worktree with the x4lan package; default this one)',
                 'Clients log "[X4LAN] connected to relay as pN" and every 5 s "[X4LAN] relay=connected '
                 'remote=receiving"; the relay prints "p1@127.0.0.1:<port>, p2@127.0.0.1:<port>".',
                 'Needs re-pinned profile gates (session default) and the simtick DLL from '
                 'native/engine/build.py (no --fixedtick).']
    else:
        attn = Path(args.attnload).resolve()
        installer = attn / 'dev' / 'install_attention_load_test.py'
        spec = importlib.util.spec_from_file_location('x4lane_attnload_installer', installer)
        module = importlib.util.module_from_spec(spec)
        previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # never write into that worktree
        try:
            spec.loader.exec_module(module)                           # read only: uses its overlay() text
        finally:
            sys.dont_write_bytecode = previous
        copy_into(Path(args.dll) if args.dll else attn / '.local' / 'hooks' / 'x4lan_hooks_64.dll', out,
                  'native/x4lan_hooks_64.dll', note)
        hooks = (attn / 'extension' / 'x4_lan_lab' / 'ui' / 'engine_hooks.lua').read_text(encoding='utf-8')
        if GUARD_OLD in hooks:
            hooks = hooks.replace(GUARD_OLD, GUARD_NEW)
            note.append('ui/engine_hooks.lua: lane copy only - attention_load_profile guard also accepts '
                        'the lane profiles ' + '/'.join(core.LANE_PROFILES.values()) + ' (one-line change, everything else unchanged)')
        elif "'45850'" not in hooks:
            raise UsageError('engine_hooks.lua profile guard changed and does not accept the lane profiles; '
                             'ask the attention-hooks owner to allow the lane profiles')
        (out / 'ui').mkdir(parents=True, exist_ok=True)
        (out / 'ui' / 'engine_hooks.lua').write_text(hooks, encoding='utf-8')
        copy_into(attn / 'dev' / 'attention_load_test.lua', out, 'ui/attention_load_test.lua', note)
        copy_into(attn / 'dev' / 'attention_load_test.xml', out, 'md/x4lan_attnload.xml', note)
        (out / 'ui.xml.files').write_text('ui/engine_hooks.lua\nui/attention_load_test.lua\n', encoding='utf-8')
        steps = [int(v) for v in args.steps.split(',') if v.strip()]
        durations = [int(v) for v in args.durations.split(',') if v.strip()]
        if not steps or len(steps) != len(durations) or any(n < 0 or n > 8 for n in steps):
            raise UsageError('--steps must be 0..8 with one --durations entry per step')
        extra = []
        for item in args.set:
            key, _, value = item.partition('=')
            if not re.fullmatch(r'attention_load_\w+', key) or not value:
                raise UsageError(f'--set accepts attention_load_* keys only: {item}')
            extra.append(f'X4LAN_CONFIG.{key} = {value}')
        text = module.overlay(steps, durations, args.warmup, '@X4LANE_PROFILE@', extra)
        (out / 'ui' / 'config.lua.append').write_text(text, encoding='utf-8')
        try:
            commit = subprocess.run(['git', '-C', str(attn), 'rev-parse', '--short', 'HEAD'], capture_output=True,
                                    text=True, timeout=30).stdout.strip() or '?'
        except (OSError, subprocess.SubprocessError):
            commit = '?'
        note += ['', f'attnload worktree {attn} at {commit}; schedule steps={steps} durations={durations} '
                 f'warmup={args.warmup}', 'Apply AFTER a probe overlay: --overlay <probe> --overlay <this>.',
                 'Ends with "[X4LAN] ATTNLOAD done"; failures log "ATTNLOAD error" or "HOOKS_DISABLED".']
    (out / 'x4lane-overlay.txt').write_text('\n'.join(note) + '\n', encoding='utf-8')
    print(out)
    return 0


# ---------------------------------------------------------------------------------------


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('status', help='locks, quiet.lock, running X4, RAM, Steam')
    s.add_argument('--json', action='store_true')
    s = sub.add_parser('setup', help='create/check lane views, profiles, base and x4lock')
    s.add_argument('--lane', default='all', choices=tuple(core.LANE_PROFILES) + ('all',))
    s.add_argument('--check', action='store_true')
    s.add_argument('--refresh-base', action='store_true', help='replace lanes/base/x4_lan_lab')
    s.add_argument('--base-from', help='x4_lan_lab to snapshot (default: this worktree)')
    s.add_argument('--rebuild-view', action='store_true', help='remove and recreate the view (after X4 updates)')
    r = sub.add_parser('run', help='one unattended native run in a lane')
    r.add_argument('--lane', default='auto', choices=tuple(core.LANE_PROFILES) + ('auto',))
    r.add_argument('--agent', required=True)
    r.add_argument('--label', default='')
    r.add_argument('--collect', required=True, help='output directory (result.json, logs)')
    r.add_argument('--overwrite', action='store_true', help='allow a --collect dir with a result.json')
    r.add_argument('--overlay', action='append', default=[], help='files copied over the lane x4_lan_lab')
    r.add_argument('--extension', action='append', default=[], help='extra test extension directory')
    r.add_argument('--base', help='x4_lan_lab to use instead of lanes/base (must stay offline/inert)')
    r.add_argument('--lua', action='append', default=[], help='line appended to the lane ui/config.lua')
    r.add_argument('--ui-file', action='append', default=[], help='ui/*.lua to add to ui.xml after config.lua')
    r.add_argument('--config', action='append', default=[], help='KEY=VALUE for the lane config.xml (this run)')
    r.add_argument('--until', help='regex on the debug log that ends the run (pass)')
    r.add_argument('--until-count', type=int, default=1, help='matching lines needed (default 1)')
    r.add_argument('--timeout', type=float, required=True, help='seconds from launch')
    r.add_argument('--settle', type=float, default=3.0, help='seconds X4 keeps running after --until (default 3)')
    r.add_argument('--expect', action='append', default=[], help="'[N:]REGEX' must match >= N lines (default 1)")
    r.add_argument('--fail-on', action='append', default=[], help='regex that aborts the run (fail)')
    r.add_argument('--gamestart', default='custom_creative')
    r.add_argument('--seed', type=int, default=276944566, help='-seed:N for the new world, 0 = random')
    r.add_argument('--start', default='module', choices=('module', 'menu'),
                   help='module: -module <gamestart>; menu: autostart calls NewGame from the start menu')
    r.add_argument('--alive', type=float, default=10.0, help='autostart ALIVE line interval (0 = off)')
    r.add_argument('--stall', type=float, default=240.0,
                   help='fail when no debug log line arrives for S s after INGAME (0 = off)')
    r.add_argument('--sound', action='store_true', help='keep game sound on (default: autostart mutes)')
    r.add_argument('--wait', type=float, default=0.0, help='wait up to S for quiet.lock/lanes/RAM (checks every 60 s)')
    r.add_argument('--timing', action='store_true',
                   help='hold rewrite-coord/quiet.lock for this run; refuses if any other X4 runs')
    r.add_argument('--min-free-gb', type=float, default=core.MIN_FREE_RAM_GB)
    r.add_argument('--close-grace', type=float, default=CLOSE_GRACE_S, help='seconds after WM_CLOSE before kill')
    r.add_argument('--allow-workshop', action='store_true')
    r.add_argument('--repin-profile', action='store_true',
                   help="re-pin the x4_lan_lab profile gates ('45841', /45841/save) to the lane profile")
    r.add_argument('--test-skip-release', action='store_true', help=argparse.SUPPRESS)  # lane self-test only
    q = sub.add_parser('session', help='2-3 clients (lanes) at once in one loopback session, optional server')
    q.add_argument('--clients', type=int, default=2)
    q.add_argument('--lanes', help='comma list of lanes, one per client (default: any free lanes)')
    q.add_argument('--agent', required=True)
    q.add_argument('--label', default='')
    q.add_argument('--collect', required=True, help='output directory (session result.json, clientN/, server.log)')
    q.add_argument('--overwrite', action='store_true')
    q.add_argument('--timeout', type=float, required=True)
    q.add_argument('--overlay', action='append', default=[], help='overlay for every client')
    q.add_argument('--client-overlay', action='append', default=[], help='N:DIR overlay for client N (after --overlay)')
    q.add_argument('--extension', action='append', default=[])
    q.add_argument('--client-extension', action='append', default=[], help='N:DIR')
    q.add_argument('--lua', action='append', default=[])
    q.add_argument('--client-lua', action='append', default=[], help='N:LINE')
    q.add_argument('--config', action='append', default=[], help='KEY=VALUE for every client config.xml')
    q.add_argument('--client-config', action='append', default=[], help='N:KEY=VALUE')
    q.add_argument('--ui-file', action='append', default=[])
    q.add_argument('--base')
    q.add_argument('--until', help='regex every client log must match (--until-mode all) or one (any)')
    q.add_argument('--until-count', type=int, default=1)
    q.add_argument('--until-mode', default='all', choices=('all', 'any'))
    q.add_argument('--settle', type=float, default=3.0)
    q.add_argument('--expect', action='append', default=[], help="'[N:]REGEX' for every client log")
    q.add_argument('--fail-on', action='append', default=[], help='regex on any client log that fails the session')
    q.add_argument('--server-cmd', help='server command line; placeholders {port} {token} {build} {bubble} '
                                        '{clients} {session} {outdir} {python} {repo}')
    q.add_argument('--server-cwd', help='server working directory (default: this worktree)')
    q.add_argument('--server-ready', help='regex on the server output that means ready (default: bound on the port)')
    q.add_argument('--server-ready-timeout', type=float, default=60.0)
    q.add_argument('--server-until', help='regex the server output must match for the session to pass')
    q.add_argument('--server-fail-on', action='append', default=[])
    q.add_argument('--build', default='lane-session', help='build label for @X4LANE_BUILD@ / {build}')
    q.add_argument('--bubble', default='bubble-01', help='bubble id for @X4LANE_BUBBLE@ / {bubble}')
    q.add_argument('--gamestart', default='custom_creative')
    q.add_argument('--seed', type=int, default=276944566)
    q.add_argument('--start', default='module', choices=('module', 'menu'))
    q.add_argument('--alive', type=float, default=10.0)
    q.add_argument('--stall', type=float, default=240.0)
    q.add_argument('--sound', action='store_true')
    q.add_argument('--wait', type=float, default=0.0)
    q.add_argument('--timing', action='store_true')
    q.add_argument('--min-free-gb', type=float, default=core.MIN_FREE_RAM_GB, help='per X4 instance')
    q.add_argument('--close-grace', type=float, default=CLOSE_GRACE_S)
    q.add_argument('--allow-workshop', action='store_true')
    q.add_argument('--no-repin', action='store_true', help='keep the 45841 profile gates (default: re-pin per lane)')
    s = sub.add_parser('canary', help='native self-test run (no overlay, until AUTOSTART READY)')
    s.add_argument('--lane', default='auto', choices=tuple(core.LANE_PROFILES) + ('auto',))
    s.add_argument('--agent', default='lane')
    s.add_argument('--timeout', type=float, default=300.0)
    s.add_argument('--wait', type=float, default=1800.0)
    s = sub.add_parser('doctor', help='self-check of all lanes; --fix repairs what is safe')
    s.add_argument('--fix', action='store_true')
    s.add_argument('--json', action='store_true')
    s = sub.add_parser('publish-release', help='lane owner: export a tested commit to X4Mods/lanes/release')
    s.add_argument('--commit', default='HEAD')
    s = sub.add_parser('stop', help='gracefully close lane X4 instances (lane exes only)')
    s.add_argument('--all-lanes', action='store_true')
    s.add_argument('--lane', help='comma list of lanes')
    s.add_argument('--by', default='lead', help='who stops (recorded for the runners)')
    s.add_argument('--reason', default='', help='why (recorded in the runners result.json)')
    s.add_argument('--grace', type=float, default=30.0, help='seconds to wait after WM_CLOSE')
    s.add_argument('--force', action='store_true', help='terminate lane X4s still running after --grace')
    s = sub.add_parser('queue', help='runs waiting for lanes (first come, first served)')
    s.add_argument('--json', action='store_true')
    s = sub.add_parser('restore', help='reset a lane to base after a crashed runner')
    s.add_argument('--lane', required=True, choices=tuple(core.LANE_PROFILES))
    s = sub.add_parser('unlock', help='remove a stale lane lock')
    s.add_argument('--lane', required=True, choices=tuple(core.LANE_PROFILES))
    m = sub.add_parser('make-overlay', help='build a ready overlay directory')
    m.add_argument('kind', choices=('probe', 'attnload', 'session-telemetry'))
    m.add_argument('--out', required=True)
    m.add_argument('--force', action='store_true')
    m.add_argument('--dll', help='DLL to use instead of the default build output')
    m.add_argument('--root', help='probe: worktree with .local/engine and dev/fixed_tick_probe.lua')
    m.add_argument('--mode', default='probe', choices=('probe', 'profile', 'fixed60', 'fixed60catchup'))
    m.add_argument('--attnload', help='attnload: the X4-Multiplayer-attnload worktree (read only)')
    m.add_argument('--steps', default='0,8')
    m.add_argument('--durations', default='60,180')
    m.add_argument('--warmup', type=int, default=20)
    m.add_argument('--set', action='append', default=[], help="attention_load_KEY=LUA")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    paths = core.Paths.from_env(ROOT)
    if args.command == 'make-overlay' and args.kind == 'attnload' and not args.attnload:
        print('make-overlay attnload needs --attnload WORKTREE', file=sys.stderr)
        return 1
    handlers = {'status': cmd_status, 'setup': cmd_setup, 'run': cmd_run, 'session': cmd_session,
                'stop': cmd_stop, 'queue': cmd_queue, 'restore': cmd_restore, 'unlock': cmd_unlock,
                'make-overlay': cmd_make_overlay, 'publish-release': cmd_publish_release, 'doctor': cmd_doctor,
                'canary': cmd_canary}
    try:
        return handlers[args.command](args, paths)
    except LaneError as error:
        print(f'x4lane: {type(error).__name__}: {error}', file=sys.stderr)
        return core.EXIT['usage'] if isinstance(error, UsageError) else core.EXIT['refused']


if __name__ == '__main__':
    sys.exit(main())
