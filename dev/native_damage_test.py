"""Native damage-probe harness (D-task, dev-only, unshipped). Windows guest; isolated profile only.

A synthetic HOST (a health generator on this PC's own LAN IPv4, not a second X4 client) publishes
scripted WORLD health to ONE native Windows guest that runs the installed internal package with
dev/damage_probe.lua/.xml overlaid. It proves request/readback correlation in the real engine;
it cannot prove two-native multiplayer.

  prepare --package DIR   install DIR's package with its own setup.ps1, pointed at THIS PC's own
                          LAN IPv4 (guest installs refuse loopback), back up profile files and the
                          package installation.json, hash saves, overlay the probe
  run [--minutes N]       start relay + synthetic host, launch X4 via setup.ps1 Start, play the
                          schedule, close X4, collect evidence and validate it
  restore                 remove the mod with setup.ps1 Remove, restore profile files exactly,
                          verify saves are unchanged

Evidence goes to .local/native-damage-tests/<utc>-<sha>/. Dispatch is never counted as completion.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
HERE = SOURCE / '.local/native-damage-tests'
STATE = HERE / 'state.json'
PROFILE_ID = '45841'
SECTOR = 'cluster_14_sector001_macro'
PROFILE_FILES = ('config.xml', 'content.xml', 'x4lan-debug.txt')
sys.path.insert(0, str(SOURCE))
spec = importlib.util.spec_from_file_location('validate_damage_probe', SOURCE / 'dev/validate_damage_probe.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

# Synthetic host schedule: seconds after the guest joined the relay -> (phase, ship1 hull, ship1 shield).
# ship1 is the host's ship as seen by the guest; the guest's own ship stays at 100/100.
SETTLE = 20.0


def schedule():
    phases = [('settle', SETTLE, None)]
    phases += [('identical', 6.0, (50.0, 50.0))]                       # re-sent unchanged every second
    phases += [('different', 2.0, (20.0, 20.0)), ('different', 2.0, (90.0, 90.0)),
               ('different', 2.0, (40.0, 40.0))]
    phases += [('rapid', 2.0, 'rapid')]                                 # new health every relay tick
    phases += [('burst', 4.0, 'burst')]                                 # 3 distinct frames flushed back-to-back
    phases += [('final', 4.0, (100.0, 100.0))]
    return phases


# Every BURST_EVERY seconds of the burst phase, these values reach the guest within ~1 ms,
# so several Apply requests are queued in one game frame (proved by the probe's frame field).
BURST_VALUES = ((31.0, 31.0), (32.0, 32.0), (33.0, 33.0))
BURST_EVERY = .5


def health_at(elapsed, tick):
    """Phase and ship1 health for a time since the guest joined; tick numbers rapid changes."""
    t = 0.0
    for name, length, value in schedule():
        if elapsed < t + length:
            if value is None:
                return name, (100.0, 100.0)
            if value == 'rapid':
                h = 60.0 + (tick % 30)
                return name, (h, h)
            if value == 'burst':
                return name, (30.0, 30.0)                               # held between bursts
            return name, value
        t += length
    return 'done', (100.0, 100.0)


def total_seconds():
    return sum(length for _, length, _ in schedule())


def world_fields(ship1):
    hull, shield = ship1
    return [hull, shield, 0, 100.0, 100.0, 0, 100, 100, 100, 100, 200, 1, 1, 0, 0, 0, 0]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hashes(root):
    root = Path(root)
    return {str(p.relative_to(root)).replace('\\', '/'): sha256(p) for p in sorted(root.rglob('*')) if p.is_file()}


def documents_folder():
    """The user's Documents via the Windows Known Folder API (honours redirection such as OneDrive)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                    ('Data4', ctypes.c_ubyte * 8)]
    # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
    folder = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
    pointer = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(pointer)) != 0:
        raise RuntimeError('The Windows Documents folder could not be located.')
    try:
        return Path(pointer.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pointer)


def git_head():
    status = subprocess.run(['git', 'status', '--porcelain', '--', 'dev', 'extension', 'x4lan', 'tests'],
                            cwd=SOURCE, capture_output=True, text=True, check=True).stdout.strip()
    if status:
        raise RuntimeError('Refusing: uncommitted changes in dev/extension/x4lan/tests. Pin a commit first.')
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, capture_output=True, text=True, check=True).stdout.strip()


def local_ipv4():
    """This PC's primary LAN IPv4 (route lookup only; no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('10.255.255.255', 1))
        address = probe.getsockname()[0]
    finally:
        probe.close()
    first = int(address.split('.')[0])
    if address.startswith('127.') or first == 0 or first >= 224:
        raise RuntimeError('No usable local LAN IPv4 found: ' + address)
    return address


def windows_paths(package):
    """Paths from the package's own installation.json, written by its setup.ps1 Install."""
    record = json.loads((package / 'installation.json').read_text(encoding='utf-8-sig'))
    profile = Path(record['profile'])
    if profile.name != PROFILE_ID:
        raise RuntimeError('Refusing: the package profile is not the isolated test profile ' + PROFILE_ID)
    return Path(record['game']), profile, record


def saves(profile):
    """Hashes of every save in every X4 profile next to the test profile (normal saves must not change)."""
    result = {}
    for p in profile.parent.glob('*/save/*'):
        if p.is_file():
            result[str(p)] = {'sha256': sha256(p), 'mtime_ns': p.stat().st_mtime_ns}
    return result


STEAM_METADATA = {'steam_autocloud.vdf'}  # Steam Cloud bookkeeping; Steam touches its timestamp on launch


def saves_changed(before, now):
    """Changed save paths. Real saves compare by content and mtime; Steam metadata by content only."""
    changed = sorted(set(before) ^ set(now))
    for path in set(before) & set(now):
        a, b = before[path], now[path]
        if a['sha256'] != b['sha256'] or (Path(path).name not in STEAM_METADATA and a['mtime_ns'] != b['mtime_ns']):
            changed.append(path)
    return sorted(changed)


def setup_ps1(package, *args):
    command = ['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', str(package / 'setup.ps1'), *args]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)


def x4_running():
    if os.name != 'nt':
        return False
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq X4.exe'], capture_output=True, text=True).stdout
    return 'X4.exe' in out


def overlay(target):
    """Copy the probe into the installed mod and register it after coop_game.lua."""
    shutil.copy2(SOURCE / 'dev/damage_probe.lua', target / 'ui/damage_probe.lua')
    shutil.copy2(SOURCE / 'dev/damage_probe.xml', target / 'md/damage_probe.xml')
    ui = target / 'ui.xml'
    text = ui.read_text(encoding='utf-8')
    anchor = '<file name="ui/coop_game.lua" />'
    if anchor not in text:
        raise RuntimeError('ui.xml has no coop_game.lua entry; unexpected package layout')
    if 'ui/damage_probe.lua' not in text:
        indent = text[:text.index(anchor)].rsplit('\n', 1)[-1]
        text = text.replace(anchor, anchor + '\n' + indent + '<file name="ui/damage_probe.lua" />', 1)
        ui.write_text(text, encoding='utf-8')


def prepare(package):
    if os.name != 'nt':
        raise RuntimeError('prepare targets the Windows guest; on Linux use dev/native_world_test.py install')
    if STATE.exists():
        raise RuntimeError('A previous run is not restored yet: run restore first.')
    if x4_running():
        raise RuntimeError('Close X4 first.')
    head = git_head()
    package = package.resolve()
    pkg = json.loads((package / 'package.json').read_text(encoding='utf-8-sig'))
    if not str(pkg.get('package_id', '')).startswith('lan-') or pkg.get('slot') != 'p2':
        raise RuntimeError('Only an internal guest (p2) package may be used by this harness.')
    # Profile files and saves before anything changes; Documents via the Known Folder API.
    profile = documents_folder() / 'Egosoft' / 'X4' / PROFILE_ID
    run = HERE / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + head[:7])
    backup = run / 'profile-before'
    backup.mkdir(parents=True)
    present = []
    for name in PROFILE_FILES:
        if (profile / name).is_file():
            shutil.copy2(profile / name, backup / name)
            present.append(name)
    before_saves = saves(profile)
    record_before = package / 'installation.json'
    had_record = record_before.is_file()          # sampled BEFORE Install rewrites it
    if had_record:
        shutil.copy2(record_before, backup / 'package-installation.json')
    address = local_ipv4()
    # Recovery state exists before anything is installed, so a failed prepare can be restored.
    state = dict(run=str(run), head=head, package=str(package), package_id=pkg['package_id'],
                 version=(package / 'VERSION.txt').read_text(encoding='utf-8-sig').strip(),
                 game=None, profile=str(profile), profile_files=present, saves=before_saves, address=address,
                 had_installation_json=had_record, stage='backed-up')
    HERE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))
    setup_ps1(package, '-Action', 'Install', '-ServerIP', address)
    game, profile2, record = windows_paths(package)
    state.update(game=str(game), stage='installed')
    STATE.write_text(json.dumps(state, indent=2))
    if profile2.resolve() != profile.resolve():
        raise RuntimeError('Installed profile differs from the backed-up test profile; run restore.')
    target = game / 'extensions' / 'x4_lan_lab'
    state['installed'] = tree_hashes(target)
    overlay(target)
    state.update(stage='prepared', overlaid=tree_hashes(target))
    STATE.write_text(json.dumps(state, indent=2))
    (run / 'prepare.json').write_text(json.dumps({k: v for k, v in state.items() if k != 'saves'}, indent=2))
    print('Prepared', state['version'], state['package_id'], 'with probe at', head[:7], '->', run)


def run(minutes):
    if not STATE.exists():
        raise RuntimeError('Run prepare first.')
    state = json.loads(STATE.read_text())
    if state.get('stage') != 'prepared':
        raise RuntimeError('prepare did not complete; run restore first.')
    if state['head'] != git_head():
        raise RuntimeError('The checked-out commit differs from the prepared probe commit.')
    from x4lan.host import Relay
    from x4lan.universe import UniverseSession
    package = Path(state['package'])
    cfg = json.loads((package / 'session.json').read_text(encoding='utf-8-sig'))
    session = UniverseSession(cfg['token'], cfg['build'], cfg['bubble'])
    relay = Relay(state['address'], cfg['port'], session)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((state['address'], cfg['port']))
    sock.setblocking(False)
    nonce, lease, seq = secrets.token_hex(16), None, 0
    sent, joined, start, next_send, next_hello, tick = [], None, time.monotonic(), 0.0, 0.0, 0
    next_burst = 0.0
    setup_ps1(package, '-Action', 'Start')
    print('SYNTHETIC HOST running; X4 guest is starting. Health below is scripted, not read from a host X4.')
    try:
        while time.monotonic() - start < minutes * 60:
            now = time.monotonic()
            relay.step(now)
            if now >= next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|p1|{nonce}".encode())
                next_hello = now + 1
            for _ in range(256):
                try:
                    f = sock.recv(769).decode('ascii', 'replace').split('|')
                except (BlockingIOError, ConnectionResetError):
                    break
                if f[0] == 'WELCOME' and len(f) > 3 and f[2] == nonce:
                    lease = f[3]
            if joined is None and 'p2' in session.peers:
                joined = now
                print('Native guest joined the relay; schedule starts after', SETTLE, 's settle')
            if lease and now >= next_send:
                seq += 1
                tick += 1
                next_send = now + .05
                sock.send(f'IDLE|1|{lease}|{seq}'.encode())
                sock.send(f'CREW|1|{lease}|{seq}|0|0'.encode())
                phase, ship1 = health_at(now - joined, tick) if joined else ('waiting', (100.0, 100.0))
                fields = world_fields(ship1)
                sock.send(('WORLD|1|' + lease + '|' + str(seq) + '|' + '|'.join(f'{v:g}' for v in fields)).encode())
                sent.append(dict(rt=round(now - start, 3), revision=seq, phase=phase, ship1=ship1))
                if phase == 'burst' and now >= next_burst:
                    # Distinct revisions reach the guest within ~1 ms, inside one game frame: each
                    # is accepted by the relay and force-flushed to the guest before the next.
                    next_burst = now + BURST_EVERY
                    for values in BURST_VALUES:
                        seq += 1
                        sock.send(('WORLD|1|' + lease + '|' + str(seq) + '|' +
                                   '|'.join(f'{v:g}' for v in world_fields(values))).encode())
                        time.sleep(.0003)
                        relay.step(time.monotonic())
                        relay.next_tick = 0
                        relay.step(time.monotonic())
                        sent.append(dict(rt=round(time.monotonic() - start, 3), revision=seq, phase='burst', ship1=values))
                if joined and now - joined > total_seconds():
                    break
            time.sleep(.005)
    finally:
        sock.close()
        relay.close()
    finish(state, sent, joined is not None)


def finish(state, sent, joined):
    run_dir = Path(state['run'])
    if x4_running():
        subprocess.run(['taskkill', '/IM', 'X4.exe', '/F'], capture_output=True)
        for _ in range(60):
            if not x4_running():
                break
            time.sleep(1)
    log = Path(state['profile']) / 'x4lan-debug.txt'
    if log.is_file():
        shutil.copy2(log, run_dir / 'x4lan-debug.txt')
    (run_dir / 'schedule.json').write_text(json.dumps(dict(joined=joined, schedule=schedule(), sent=sent), indent=1))
    report = evaluate(run_dir / 'x4lan-debug.txt' if log.is_file() else None, sent, joined)
    report.update(head=state['head'], package_id=state['package_id'], version=state['version'])
    (run_dir / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ('result', 'reasons')}, indent=2))


def evaluate(log, sent, joined):
    """PASS only when every native Apply request has exactly one exact-echo readback."""
    reasons = []
    if not joined:
        reasons.append('native guest never joined the relay')
    if log is None:
        return dict(result='FAIL', reasons=reasons + ['no game log'], runs={})
    with open(log, encoding='utf-8', errors='replace') as handle:
        rows = validator.parse(handle)
    runs = validator.summarise(rows)
    if not runs:
        reasons.append('no DAMAGE_PROBE lines (probe not loaded?)')
    for name, r in runs.items():
        for key in ('missing', 'orphan', 'echo_mismatch'):
            if r[key]:
                reasons.append(f'run {name}: {key} {r[key][:10]}')
        for key in ('unmatched', 'invalid'):
            if r[key]:
                reasons.append(f'run {name}: {key}={r[key]}')
    requested = [row for row in rows if row.get('reason') == 'apply_requested']
    phases = {p for p, _, v in schedule() if v is not None}
    seen = set()
    for item in sent:
        if item['phase'] in phases:
            seen.add(item['phase'])
    if phases - seen:
        reasons.append('schedule phases not sent: ' + ', '.join(sorted(phases - seen)))
    wanted = {f'{h:.2f}/{s:.2f}' for item in sent if item['phase'] in ('identical', 'different')
              for h, s in [item['ship1']]}
    got = set()
    for row in requested:
        detail = row.get('detail_text', '')
        if 'requested[ship1=' in detail:
            got.add(detail.split('requested[ship1=')[1].split(' ')[0])
    if wanted - got:
        reasons.append('scheduled health never requested by the guest: ' + ', '.join(sorted(wanted - got)))
    # Coverage gate: when bursts were sent, at least one co-op frame must carry several Apply
    # requests, i.e. several Mark/Apply pairs queued in one native update.
    per_frame = {}
    for row in requested:
        key = (row.get('run'), row.get('frame'))
        per_frame[key] = per_frame.get(key, 0) + 1
    most = max(per_frame.values(), default=0)
    if any(item['phase'] == 'burst' for item in sent) and most < 2:
        reasons.append('no frame with >=2 apply_requested: queued Mark/Apply pairs in one update not demonstrated')
    mismatched = sum(len(r['mismatched']) for r in runs.values())
    return dict(result='PASS' if not reasons else 'FAIL', reasons=reasons, requests=len(requested),
                max_requests_per_frame=most,
                confirmed=sum(r['confirmed'] for r in runs.values()), observed_mismatched=mismatched,
                runs=runs, note='observed_mismatched is reported, not a correlation failure')


def restore():
    if not STATE.exists():
        raise RuntimeError('Nothing to restore.')
    state = json.loads(STATE.read_text())
    if x4_running():
        raise RuntimeError('Close X4 first.')
    package, profile = Path(state['package']), Path(state['profile'])
    game = state.get('game')
    if not game:
        # prepare stopped before or during Install: the package's own record names the game.
        try:
            game = str(windows_paths(package)[0])
        except (OSError, KeyError, ValueError, RuntimeError):
            game = None
    target = Path(game) / 'extensions' / 'x4_lan_lab' if game else None
    if target is not None and target.exists():
        setup_ps1(package, '-Action', 'Remove')
    backup = Path(state['run']) / 'profile-before'
    for name in PROFILE_FILES:
        if name in state['profile_files']:
            shutil.copy2(backup / name, profile / name)
        elif (profile / name).exists() and name != 'x4lan-debug.txt':
            (profile / name).unlink()
    record = package / 'installation.json'
    if state.get('had_installation_json'):
        shutil.copy2(backup / 'package-installation.json', record)
    elif record.exists():
        record.unlink()
    problems = [name for name in state['profile_files'] if sha256(profile / name) != sha256(backup / name)]
    if state.get('had_installation_json') and sha256(record) != sha256(backup / 'package-installation.json'):
        problems.append('package installation.json not restored')
    changed = saves_changed(state['saves'], saves(profile))
    if changed:
        problems.append('saves changed: ' + ', '.join(changed))
    if target is not None and target.exists():
        problems.append('mod still installed at ' + str(target))
    (Path(state['run']) / 'restore.json').write_text(json.dumps(dict(problems=problems), indent=2))
    if problems:
        raise RuntimeError('Restore incomplete: ' + '; '.join(problems))
    STATE.unlink()
    print('Restored: mod removed by its own setup.ps1, profile files exact, saves unchanged.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--package', type=Path, required=True)
    r = sub.add_parser('run'); r.add_argument('--minutes', type=float, default=4)
    sub.add_parser('restore')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        prepare(args.package)
    elif args.command == 'run':
        run(args.minutes)
    else:
        restore()


if __name__ == '__main__':
    main()
