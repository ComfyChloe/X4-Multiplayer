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
  restore                 remove the mod with setup.ps1 Remove ONLY if this run installed it (and
                          it is unchanged), restore profile files exactly, verify saves unchanged
  bundle --run DIR        sanitized review ZIP: manifest, probe-only trace, schedule, report and
                          restore summary (no saves, credentials, full logs or profile backups)

The recovery journal (state.json) is replaced atomically on every update. It records whether the
mod folder is run-owned: only after this run's Install returned and the installed folder carries
this package's marker. A mod that existed before prepare is refused up front or, when Install
refuses it, left untouched.

Evidence goes to .local/native-damage-tests/<utc>-<sha>/. Dispatch is never counted as completion.
"""
from pathlib import Path
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
# 31, 32, 31: identical AND distinct requests outstanding together. Production bubble_game.lua
# drops an unchanged consecutive WORLD key, so equal values coexist only with a distinct one between.
BURST_VALUES = ((31.0, 31.0), (32.0, 32.0), (31.0, 31.0))
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
        name = re.split(r'[\\/]', path)[-1]   # Windows paths are also compared on Linux test hosts
        if a['sha256'] != b['sha256'] or (name not in STEAM_METADATA and a['mtime_ns'] != b['mtime_ns']):
            changed.append(path)
    return sorted(changed)


def is_windows():
    return os.name == 'nt'


def write_state(state):
    """Replace the recovery journal atomically: a complete temporary file, flushed, then renamed."""
    HERE.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_name(STATE.name + '.tmp')
    with open(temp, 'w', encoding='utf-8') as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, STATE)


def setup_ps1(package, *args):
    command = ['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', str(package / 'setup.ps1'), *args]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)


def x4_running():
    if not is_windows():
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
    if not is_windows():
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
    record_before = package / 'installation.json'
    had_record = record_before.is_file()          # sampled BEFORE Install rewrites it
    if had_record:
        # An earlier installation record names the game: a mod already there is not this run's,
        # so refuse before anything is changed (a damaged record also refuses).
        earlier = Path(json.loads(record_before.read_text(encoding='utf-8-sig'))['game']) / 'extensions' / 'x4_lan_lab'
        if earlier.exists():
            raise RuntimeError('Refusing: a mod is already installed at ' + str(earlier) +
                               '; this harness removes only a mod it installed itself.')
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
    # The pre-install save inventory also stays in the run folder: the journal holding it is
    # deleted once restore succeeds, and the evidence must still show both sides.
    (run / 'saves-before.json').write_text(json.dumps(before_saves, indent=2))
    if had_record:
        shutil.copy2(record_before, backup / 'package-installation.json')
    address = local_ipv4()
    # Recovery state exists before anything is installed, so a failed prepare can be restored.
    # owned_target stays None until this run's own Install is proven (see below).
    state = dict(run=str(run), head=head, package=str(package), package_id=pkg['package_id'],
                 version=(package / 'VERSION.txt').read_text(encoding='utf-8-sig').strip(),
                 game=None, profile=str(profile), profile_files=present, saves=before_saves, address=address,
                 had_installation_json=had_record, owned_target=None, stage='backed-up')
    write_state(state)
    try:
        setup_ps1(package, '-Action', 'Install', '-ServerIP', address)
    except Exception:
        # Install refused (e.g. a mod folder already exists) or rolled back: nothing at the mod
        # path belongs to this run, and restore must never remove it.
        state.update(stage='install-failed')
        write_state(state)
        raise
    game, profile2, record = windows_paths(package)
    target = game / 'extensions' / 'x4_lan_lab'
    marker = target / '.x4lan-package-id'
    if not marker.is_file() or marker.read_text(encoding='utf-8-sig') != pkg['package_id']:
        raise RuntimeError('Install returned but ' + str(target) + ' does not carry this package marker; '
                           'ownership unproven, nothing will be removed automatically.')
    state.update(game=str(game), owned_target=str(target), installed=tree_hashes(target),
                 installed_record_sha256=sha256(package / 'installation.json'), stage='installed')
    write_state(state)
    if profile2.resolve() != profile.resolve():
        raise RuntimeError('Installed profile differs from the backed-up test profile; run restore.')
    overlay(target)
    state.update(stage='prepared', overlaid=tree_hashes(target))
    write_state(state)
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
    for name, r in runs.items():
        if r.get('frameless'):
            reasons.append(f'run {name}: lines without a probe frame number {r["frameless"][:10]}')
    requested = [row for row in rows if row.get('reason') == 'apply_requested']
    phases = {p for p, _, v in schedule() if v is not None}
    seen = set()
    for item in sent:
        if item['phase'] in phases:
            seen.add(item['phase'])
    if phases - seen:
        reasons.append('schedule phases not sent: ' + ', '.join(sorted(phases - seen)))
    # Observed coverage: a phase counts only when the guest requested a WORLD revision that the
    # synthetic host sent in that phase, with exactly the health sent with that revision.
    by_revision = {str(item['revision']): item for item in sent}
    observed = set()
    for row in requested:
        m = re.search(r'wrev=(\d+) requested\[ship1=(\S+)', row.get('detail_text', ''))
        item = by_revision.get(m.group(1)) if m else None
        if item and m.group(2) == '%.2f/%.2f' % tuple(item['ship1']):
            observed.add(item['phase'])
    if phases - observed:
        reasons.append('schedule phases never observed in guest Apply requests: ' + ', '.join(sorted(phases - observed)))
    wanted = {f'{h:.2f}/{s:.2f}' for item in sent if item['phase'] in ('identical', 'different')
              for h, s in [item['ship1']]}
    got = set()
    for row in requested:
        detail = row.get('detail_text', '')
        if 'requested[ship1=' in detail:
            got.add(detail.split('requested[ship1=')[1].split(' ')[0])
    if wanted - got:
        reasons.append('scheduled health never requested by the guest: ' + ', '.join(sorted(wanted - got)))
    # Coverage gate: when bursts were sent, one numbered probe frame (explicit Lua frame counter)
    # must carry identical AND distinct Apply requests outstanding together, every one confirmed,
    # unpaused, read back in one Lua frame, AND sharing one MD 'seen' and one MD 'read' marker (MD
    # game clock, constant within one MD update). Lua frames alone are not an MD update boundary.
    batches = [b for r in runs.values() for b in r.get('batches', [])]
    most = max((len(b['requests']) for b in batches), default=1 if requested else 0)
    covered = [b for b in batches if b['identical_values'] and b['distinct_values'] and b.get('confirmed') and
               b.get('unpaused') and len(b['readback_frames']) == 1 and 'None' not in b['readback_frames'] and
               len(b.get('md_seen', [])) == 1 and len(b.get('md_read', [])) == 1 and
               'None' not in b['md_seen'] + b['md_read']]
    if any(item['phase'] == 'burst' for item in sent) and not covered:
        reasons.append('no frame with identical and distinct requests outstanding together whose readbacks '
                       'share one MD update (MD seen/read markers): queued Mark/Apply pairs not demonstrated')
    return dict(result='PASS' if not reasons else 'FAIL', reasons=reasons, requests=len(requested),
                max_requests_per_frame=most, covered_batches=len(covered),
                later_in_frame=sum(len(r.get('later_in_frame', [])) for r in runs.values()),
                raw_mismatched=sum(len(r.get('raw_mismatched', [])) for r in runs.values()),
                observed_mismatched=sum(len(r['mismatched']) for r in runs.values()),
                observed_phases=sorted(observed), confirmed=sum(r['confirmed'] for r in runs.values()), runs=runs,
                note=('later_in_frame: readback consistent with a later request in the same recorded frame whose own '
                      'readback matches; raw_mismatched counts every readback that differs from its request; '
                      'observed_mismatched (raw minus later_in_frame) is reported, not a correlation failure'))


def restore():
    if not STATE.exists():
        raise RuntimeError('Nothing to restore.')
    state = json.loads(STATE.read_text())
    if x4_running():
        raise RuntimeError('Close X4 first.')
    package, profile = Path(state['package']), Path(state['profile'])
    problems, summary = [], dict(stage=state.get('stage'), owned_target=state.get('owned_target'), mod='untouched')
    owned = Path(state['owned_target']) if state.get('owned_target') else None
    keep_record = False
    if owned is not None:
        # Remove only the folder this run installed, only while it is still what was installed,
        # and only while setup.ps1 Remove will select exactly that folder: Remove takes its
        # target from the CURRENT installation.json, so that record must be the one this run's
        # Install wrote (same bytes, game, profile and target). Until the owned mod is gone, the
        # current record is kept so the package's normal Remove can still recover it.
        if not owned.exists():
            summary['mod'] = 'already absent'
        else:
            keep_record = True
            reason = removal_mismatch(state, package, owned)
            if reason:
                problems.append(reason + '; nothing removed, records kept')
                summary['mod'] = 'not removed: ' + reason
            else:
                setup_ps1(package, '-Action', 'Remove')
                if owned.exists():
                    problems.append('mod still installed at ' + str(owned))
                    summary['mod'] = 'Remove left the mod installed'
                else:
                    keep_record = False
                    summary['mod'] = 'removed by the package setup.ps1'
    else:
        # Ownership was never proven (Install refused, failed part-way or was interrupted). A mod
        # folder at the journaled game or at the game of the CURRENT record may be a partial
        # install of this run or someone else's: never remove it, and keep the current record
        # (the package's 5-REMOVE-MOD needs it) and this journal until that folder is gone.
        present = unproven_targets(state, package)
        if present:
            keep_record = True
            problems.append('Install outcome unproven and a mod folder exists at ' + ', '.join(present) +
                            ': nothing removed; check it, remove the test install with the package 5-REMOVE-MOD.cmd, '
                            'then run restore again')
            summary['mod'] = 'unproven, kept'
    backup = Path(state['run']) / 'profile-before'
    for name in PROFILE_FILES:
        if name in state['profile_files']:
            shutil.copy2(backup / name, profile / name)
        elif (profile / name).exists() and name != 'x4lan-debug.txt':
            (profile / name).unlink()
    record = package / 'installation.json'
    if keep_record:
        summary['installation_json'] = 'kept: run-owned mod unresolved' if owned else 'kept: Install outcome unproven'
    elif state.get('had_installation_json'):
        shutil.copy2(backup / 'package-installation.json', record)
        summary['installation_json'] = 'restored'
        if sha256(record) != sha256(backup / 'package-installation.json'):
            problems.append('package installation.json not restored')
    else:
        if record.exists():
            record.unlink()
        summary['installation_json'] = 'absent as before'
    mismatched = [name for name in state['profile_files'] if sha256(profile / name) != sha256(backup / name)]
    problems += ['profile file not restored: ' + name for name in mismatched]
    summary['profile_files_exact'] = [name for name in state['profile_files'] if name not in mismatched]
    current = saves(profile)
    (Path(state['run']) / 'saves-after.json').write_text(json.dumps(current, indent=2))
    changed = saves_changed(state['saves'], current)
    summary.update(saves_checked=len(state['saves']), saves_changed=len(changed),
                   save_inventories=['saves-before.json', 'saves-after.json'])
    if changed:
        problems.append('saves changed: ' + ', '.join(changed))
    summary['problems'] = problems
    (Path(state['run']) / 'restore.json').write_text(json.dumps(summary, indent=2))
    if problems:
        raise RuntimeError('Restore incomplete: ' + '; '.join(problems))
    STATE.unlink()
    print('Restored: mod', summary['mod'] + '; profile files exact, saves unchanged.')


def unproven_targets(state, package):
    """Existing mod folders that may stem from an Install whose outcome this run never proved."""
    games = {state.get('game')}
    try:
        games.add(str(windows_paths(package)[0]))
    except (OSError, KeyError, ValueError, RuntimeError):
        pass
    return sorted(str(Path(g) / 'extensions' / 'x4_lan_lab') for g in games
                  if g and (Path(g) / 'extensions' / 'x4_lan_lab').exists())


def removal_mismatch(state, package, owned):
    """Why delegating to setup.ps1 Remove could touch anything but this run's mod (None if safe)."""
    if tree_hashes(owned) not in (state.get('installed'), state.get('overlaid')):
        return 'mod at ' + str(owned) + ' differs from what this run installed'
    record = package / 'installation.json'
    if not record.is_file() or sha256(record) != state.get('installed_record_sha256'):
        return 'package installation.json is not the record this run installed'
    try:
        game, profile, _ = windows_paths(package)
    except (OSError, KeyError, ValueError, RuntimeError):
        return 'package installation.json is unreadable'
    if (str(game) != state.get('game') or str(profile) != state.get('profile')
            or game / 'extensions' / 'x4_lan_lab' != owned):
        return 'package installation.json names another game, profile or mod folder'
    return None


BUNDLE_FILES = ('prepare.json', 'schedule.json', 'report.json', 'restore.json')
TRACE = 'damage-probe-trace.txt'
SECRET = re.compile(r'\b[0-9a-f]{32}\b')   # session token and leases are 32 lowercase hex characters


def sanitize(text, token, address, home=None):
    """Remove the session token, any 32-hex lease/token-shaped value, this PC's LAN address and
    the home folder path (plain and JSON-escaped)."""
    if token:
        text = text.replace(token, '<token>')
    text = SECRET.sub('<hex32>', text)
    if address:
        text = text.replace(address, '<lan-ipv4>')
    home = str(Path.home()) if home is None else home
    if home:
        for form in {home, home.replace('\\', '\\\\'), home.replace('\\', '/')}:
            text = text.replace(form, '<home>')
    return text


def bundle(run_dir, package):
    """Sanitized review ZIP: manifest, probe-only trace, schedule, report and restore summary."""
    import zipfile
    run_dir = Path(run_dir).resolve()
    if run_dir.parent != HERE.resolve():
        raise RuntimeError('Only a run folder under ' + str(HERE) + ' can be bundled.')
    if not (run_dir / 'restore.json').is_file():
        raise RuntimeError('Restore first: a review bundle includes the restore summary.')
    token = ''
    if package and (Path(package) / 'session.json').is_file():
        token = json.loads((Path(package) / 'session.json').read_text(encoding='utf-8-sig')).get('token', '')
    prepare_record = run_dir / 'prepare.json'
    address = json.loads(prepare_record.read_text()).get('address', '') if prepare_record.is_file() else ''
    files = {}
    for name in BUNDLE_FILES:
        if (run_dir / name).is_file():
            files[name] = (run_dir / name).read_text(encoding='utf-8', errors='replace')
    log = run_dir / 'x4lan-debug.txt'
    if log.is_file():
        # Only probe lines leave this PC, never the full game log.
        with open(log, encoding='utf-8', errors='replace') as handle:
            files[TRACE] = ''.join(line for line in handle if 'DAMAGE_PROBE' in line)
    archive = run_dir.parent / (run_dir.name + '-review.zip')
    entries = []
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            data = sanitize(text, token, address).encode('utf-8')
            if token and token.encode() in data:
                raise RuntimeError('token still present in ' + name)
            z.writestr(run_dir.name + '/' + name, data)
            entries.append(dict(name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
        manifest = dict(run=run_dir.name, files=entries,
                        sanitized=['session token', 'any 32-hex token or lease', 'this PC LAN IPv4', 'home folder path'],
                        excluded=['saves', 'session.json and credentials', 'full x4lan-debug.txt (only DAMAGE_PROBE lines)',
                                  'profile backups (profile-before)', 'state.json'])
        manifest_data = json.dumps(manifest, indent=2).encode('utf-8')
        z.writestr(run_dir.name + '/manifest.json', manifest_data)
        sums = [e['sha256'] + '  ' + e['name'] for e in entries]
        sums.append(hashlib.sha256(manifest_data).hexdigest() + '  manifest.json')
        z.writestr(run_dir.name + '/SHA256SUMS.txt', '\n'.join(sums) + '\n')
    print('Sanitized review bundle:', archive)
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--package', type=Path, required=True)
    r = sub.add_parser('run'); r.add_argument('--minutes', type=float, default=4)
    sub.add_parser('restore')
    b = sub.add_parser('bundle'); b.add_argument('--run', type=Path, required=True); b.add_argument('--package', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        prepare(args.package)
    elif args.command == 'run':
        run(args.minutes)
    elif args.command == 'bundle':
        bundle(args.run, args.package)
    else:
        restore()


if __name__ == '__main__':
    main()
