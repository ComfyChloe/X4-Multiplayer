"""Opt-in Windows guest for the shared pristine native seed (dev-only, unshipped).

Prepare and receive the seed first with dev/shared_seed_session.py (unchanged). This harness
then installs the known internal p2 package with its own setup.ps1 into the empty isolated
profile 45841, overlays the EXACT agreed source plus dev/seed_play_probe.lua/.xml, and stages
only the verified received seed as the single run-owned save_010.

  check   --run RUN --package DIR [--game G]   read-only preflight (seed, source, DLC, profile)
  install --run RUN --package DIR --host IP    back up, install, overlay, stage the seed
  launch  --run RUN --confirm-rendezvous       direct Steam launch (setup Start refuses saves)
  finish  --run RUN [--close]                  native readiness, save preservation, owned save
  restore --run RUN                            remove only run-owned data, restore exactly

The recovery journal (.local/native-seed-windows/state.json) is replaced atomically before and
after every mutation. A mod folder is run-owned only after this run's Install returned and the
folder carries this package's marker; setup.ps1 Remove is delegated only while the package
record is byte-identical to the one that Install wrote. Normal saves are hashed before and
compared after; the normal installer guards are not changed or bypassed.
"""
from pathlib import Path
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dev import shared_seed_session as shared
from x4lan import seed

HERE = ROOT / '.local/native-seed-windows'
STATE = HERE / 'state.json'
PROFILE_ID = '45841'
SOURCE = ROOT / 'extension/x4_lan_lab'
PROBES = ('ui/seed_play_probe.lua', 'md/seed_play_probe.xml')
# Files the harness itself adds beside the agreed production bytes.
LOCAL = {'.x4lan-package-id', 'ui/config.lua'} | set(PROBES)
STEAM_METADATA = 'steam_autocloud.vdf'
GAME_ARGS = ['-applaunch', '392160', '-personalfolderid', PROFILE_ID, '-module', 'custom_creative', '-skipintro',
             '-debug', 'scripts', '-logfile', 'x4lan-debug.txt', '-nocputhrottle', '-noworkshopsync']


def is_windows():
    return os.name == 'nt'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hashes(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob('*')) if p.is_file()}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_state(state):
    """Replace the recovery journal atomically: a complete temporary file, flushed, then renamed."""
    HERE.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_name(STATE.name + '.tmp')
    with open(temp, 'w', encoding='utf-8') as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, STATE)


def documents_folder():
    """The user's Documents via the Windows Known Folder API (honours redirection such as OneDrive)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                    ('Data4', ctypes.c_ubyte * 8)]
    folder = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
    pointer = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(pointer)) != 0:
        raise RuntimeError('The Windows Documents folder could not be located.')
    try:
        return Path(pointer.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pointer)


def profile_folder():
    return documents_folder() / 'Egosoft' / 'X4' / PROFILE_ID


def x4_running():
    if not is_windows():
        return False
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq X4.exe'], capture_output=True, text=True).stdout
    return 'X4.exe' in out


def setup_ps1(package, *args):
    command = ['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', str(Path(package) / 'setup.ps1'), *args]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)


def git_head():
    status = subprocess.run(['git', '-c', 'core.autocrlf=false', 'status', '--porcelain', '--', 'dev', 'extension', 'x4lan'],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if status:
        raise RuntimeError('Refusing: uncommitted changes in dev/extension/x4lan. Pin a commit first.')
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def saves(profile):
    """Hashes of every save in every X4 profile next to the test profile."""
    return {str(p): {'sha256': sha256(p), 'mtime_ns': p.stat().st_mtime_ns}
            for p in Path(profile).parent.glob('*/save/*') if p.is_file()}


def run_saves(state):
    """Current save inventory without this run's own seed and Steam's new metadata for that folder."""
    own = Path(state['profile']) / 'save'
    ignore = {state.get('owned_save'), str(own / STEAM_METADATA) if str(own / STEAM_METADATA) not in state['saves'] else None}
    return {p: v for p, v in saves(state['profile']).items() if p not in ignore}


def saves_changed(before, now):
    """Changed save paths. Real saves compare by content and mtime; Steam metadata by content only."""
    changed = sorted(set(before) ^ set(now))
    for path in set(before) & set(now):
        a, b = before[path], now[path]
        if a['sha256'] != b['sha256'] or (re.split(r'[\\/]', path)[-1] != STEAM_METADATA and a['mtime_ns'] != b['mtime_ns']):
            changed.append(path)
    return sorted(changed)


def seed_identity(run):
    """Pinned manifest and play session (exact checkout source digest) plus the verified received seed."""
    manifest, session = shared.prepared(run, SOURCE)
    received, _ = seed.read_bundle(Path(run) / 'received')
    result = read_json(Path(run) / 'receive-result.json')
    if received != manifest or result.get('received_verified') is not True or result.get('sha256') != manifest['sha256']:
        raise RuntimeError('The received seed is not the verified pinned snapshot.')
    return manifest, session


def enabled_extensions(profile, game):
    """Enabled extensions with their versions, as the seed manifest lists them."""
    enabled = {e.get('id'): e.get('enabled') == 'true'
               for e in ET.parse(Path(profile) / 'content.xml').getroot().findall('extension')}
    rows = []
    for path in (Path(game) / 'extensions').glob('*/content.xml'):
        info = ET.parse(path).getroot()
        if enabled.get(info.get('id'), False):
            rows.append({'id': info.get('id'), 'version': info.get('version', '0')})
    return sorted(rows, key=lambda row: row['id'])


def content_problems(manifest, profile, game):
    """Differences between the enabled game content here and the seed manifest (x4_lan_lab from source)."""
    wanted = {row['id']: row['version'] for row in manifest['extensions']}
    here = {row['id']: row['version'] for row in enabled_extensions(profile, game) if row['id'] != 'x4_lan_lab'}
    here['x4_lan_lab'] = ET.parse(SOURCE / 'content.xml').getroot().get('version', '0')
    problems = [f'{key}: seed {wanted.get(key, "absent")}, here {here.get(key, "absent/disabled")}'
                for key in sorted(set(wanted) | set(here)) if wanted.get(key) != here.get(key)]
    return problems


def production_digest(target):
    """The shared_seed_session digest over the installed folder, without the harness's own files."""
    digest = hashlib.sha256()
    for path in sorted(Path(target).rglob('*')):
        name = path.relative_to(target).as_posix()
        if path.is_file() and name not in LOCAL and path.name != 'config.lua':
            digest.update(('extension/x4_lan_lab/' + name).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def package_info(package):
    pkg = read_json(Path(package) / 'package.json')
    if not str(pkg.get('package_id', '')).startswith('lan-') or pkg.get('slot') != 'p2':
        raise RuntimeError('Only an internal guest (p2) package may be used by this harness.')
    return pkg


def record_paths(package):
    record = read_json(Path(package) / 'installation.json')
    profile = Path(record['profile'])
    if profile.name != PROFILE_ID:
        raise RuntimeError('Refusing: the package profile is not the isolated test profile ' + PROFILE_ID)
    return Path(record['game']), profile, record


def preflight(run, package, game=None):
    """Everything checkable without changing anything; returns the facts install relies on."""
    if not is_windows():
        raise RuntimeError('This harness targets the Windows guest; Linux uses dev/native_seed_play_test.py.')
    if x4_running():
        raise RuntimeError('Close X4 first.')
    manifest, session = seed_identity(run)
    package = Path(package).resolve()
    pkg = package_info(package)
    if game is None:
        if not (package / 'installation.json').is_file():
            raise RuntimeError('Pass --game: the package has no earlier installation record naming the game.')
        game = record_paths(package)[0]
    game = Path(game)
    target = game / 'extensions' / 'x4_lan_lab'
    if target.exists():
        raise RuntimeError('Refusing: a mod is already installed at ' + str(target) +
                           '; this harness removes only a mod it installed itself.')
    profile = profile_folder()
    if profile.is_symlink() or not (profile / 'content.xml').is_file():
        raise RuntimeError('Profile 45841 needs its content.xml (enabled DLC) and must not be a link.')
    if (profile / 'save').is_dir() and list((profile / 'save').glob('*.xml*')):
        raise RuntimeError('Profile 45841 contains saves; only an empty isolated profile can be used.')
    problems = content_problems(manifest, profile, game)
    if problems:
        raise RuntimeError('Enabled content differs from the seed: ' + '; '.join(problems))
    return dict(manifest=manifest, session=session, package=package, pkg=pkg, game=game, target=target, profile=profile)


def write_config(target, session, manifest, host):
    """Same role configuration as dev/native_seed_play_test.py, for the guest p2."""
    config = dict(enabled=True, bubble_actions=True, trading=True, walking_avatars=False,
                  test_profile=PROFILE_ID, host=host, slot='p2', mode='proxy',
                  proxy_macro='ship_arg_s_scout_01_a_macro', seed_id=manifest['seed_id'],
                  seed_verified=True, **session)
    (target / 'ui/config.lua').write_text('X4LAN_CONFIG={\n' + ''.join(
        f' {k}={json.dumps(v)},\n' for k, v in config.items()) + '}\n', encoding='utf-8', newline='\n')


def overlay(target, manifest, session, host):
    """Replace the package files with the exact agreed source, then add role config and probes."""
    keep = {'.x4lan-package-id'}
    source = {p.relative_to(SOURCE).as_posix() for p in SOURCE.rglob('*') if p.is_file()}
    for path in sorted(Path(target).rglob('*'), reverse=True):
        name = path.relative_to(target).as_posix()
        if path.is_file() and name not in source and name not in keep:
            path.unlink()                                   # stale package file (e.g. an MD script)
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    shutil.copytree(SOURCE, target, dirs_exist_ok=True)
    # The installed production bytes must be exactly the pinned ones before anything is added.
    if production_digest(target) != manifest['mod_sha256']:
        raise RuntimeError('Installed production bytes differ from the pinned seed digest.')
    write_config(target, session, manifest, host)
    for name in PROBES:
        shutil.copy2(ROOT / 'dev' / Path(name).name, target / name)
    tree = ET.parse(target / 'ui.xml')
    ET.SubElement(tree.find('environment'), 'file', name='ui/seed_play_probe.lua')
    tree.write(target / 'ui.xml', encoding='utf-8', xml_declaration=True)


def install(run, package, host, game=None):
    if STATE.exists():
        raise RuntimeError('A previous run is not restored yet: run restore first.')
    host = str(ipaddress.IPv4Address(host))
    if ipaddress.IPv4Address(host).is_loopback:
        raise RuntimeError('The guest needs the agreed host address, not loopback.')
    run = Path(run).resolve()
    facts = preflight(run, package, game)
    head = git_head()
    package, profile = facts['package'], facts['profile']
    backup = run / 'windows-before'
    backup.mkdir()
    present = []
    for path in sorted(profile.iterdir()):
        if path.is_file():
            shutil.copy2(path, backup / path.name)
            present.append(path.name)
    had_record = (package / 'installation.json').is_file()
    if had_record:
        shutil.copy2(package / 'installation.json', backup / 'package-installation.json')
    state = dict(run=str(run), head=head, package=str(package), package_id=facts['pkg']['package_id'],
                 game=str(facts['game']), profile=str(profile), host=host, profile_files=present,
                 save_dir_existed=(profile / 'save').is_dir(), saves=saves(profile), had_installation_json=had_record,
                 owned_target=None, owned_save=None, stage='backed-up')
    # The pre-install save inventory also stays in the run folder: the journal holding it is
    # deleted once restore succeeds, and the evidence must still show both sides.
    (run / 'saves-before.json').write_text(json.dumps(state['saves'], indent=2))
    write_state(state)
    try:
        # The preflight-selected game is passed explicitly, so the content checks and the actual
        # Install use the same location; a record naming another game still fails closed below.
        setup_ps1(package, '-Action', 'Install', '-GamePath', str(facts['game']), '-ServerIP', host)
    except Exception:
        state.update(stage='install-failed')            # nothing at the mod path belongs to this run
        write_state(state)
        raise
    game, profile2, _ = record_paths(package)
    target = game / 'extensions' / 'x4_lan_lab'
    marker = target / '.x4lan-package-id'
    if (game != facts['game'] or profile2.resolve() != profile.resolve() or not marker.is_file()
            or marker.read_text(encoding='utf-8-sig') != facts['pkg']['package_id']):
        raise RuntimeError('Install returned but the installed folder or record is not this run\'s; nothing will be '
                           'removed automatically (stage stays backed-up).')
    state.update(owned_target=str(target), installed=tree_hashes(target),
                 installed_record_sha256=sha256(package / 'installation.json'), stage='installed')
    write_state(state)
    overlay(target, facts['manifest'], facts['session'], host)
    state.update(overlaid=tree_hashes(target), stage='overlaid')
    write_state(state)
    staged = seed.stage_test_save(run / 'received', profile)
    state.update(owned_save=str(staged), owned_save_sha256=sha256(staged), stage='prepared')
    write_state(state)
    (run / 'windows-install.json').write_text(json.dumps({k: v for k, v in state.items() if k != 'saves'}, indent=2))
    print('Prepared Windows p2 for seed', facts['manifest']['seed_id'], 'at', head[:7], '; host', host)


def owned_save_only(state):
    """The isolated save folder holds exactly the run-owned verified seed (plus Steam metadata)."""
    profile = Path(state['profile'])
    files = sorted(p for p in (profile / 'save').iterdir() if p.is_file() and p.name != STEAM_METADATA)
    if [str(p) for p in files] != [state['owned_save']] or sha256(files[0]) != state['owned_save_sha256']:
        raise RuntimeError('Only the run-owned verified seed may be loaded or removed.')
    return files[0]


def launch(confirmed):
    state = read_json(STATE) if STATE.exists() else {}
    if state.get('stage') != 'prepared':
        raise RuntimeError('install did not complete; nothing to launch.')
    if not confirmed:
        raise RuntimeError('Launch only at an agreed rendezvous: pass --confirm-rendezvous.')
    if x4_running():
        raise RuntimeError('Close X4 first.')
    if state['head'] != git_head():
        raise RuntimeError('The checked-out commit differs from the installed one.')
    target, profile = Path(state['owned_target']), Path(state['profile'])
    if tree_hashes(target) != state['overlaid']:
        raise RuntimeError('The installed mod changed after install.')
    owned_save_only(state)
    if ET.parse(profile / 'config.xml').getroot().findtext('autosave') != 'false':
        raise RuntimeError('Isolated autosave must remain disabled.')
    steam = read_json(Path(state['package']) / 'installation.json')['steam']
    log = profile / 'x4lan-debug.txt'
    if log.exists():
        shutil.move(log, Path(state['run']) / 'before-launch.log')    # the prior bytes are also in windows-before
    state.update(stage='launched')
    write_state(state)
    subprocess.Popen([steam] + GAME_ARGS)
    print('Launched X4 for the seed; run finish after the session.')


def readiness(log, seed_id):
    """Native p2 admission and role readback, as dev/native_seed_play_test.py finish checks it."""
    problems = []
    expected = f'SEED_PLAY_READY seed={seed_id} slot=p2 controlled=true ownAlive=true proxyAlive=true'
    if log.count(expected) != 1 or log.count('SEED_GATE READY seed=' + seed_id) != 1:
        problems.append('native admission/player readiness absent or duplicated')
    if any(term in log for term in ('STOPPED', 'Error in MD cue md.X4LAN', 'Lua error')):
        problems.append('native adapter/MD error')
    match = re.search(r'SEED_PLAY_NATIVE seed=(\w+) slot=(\d) host=(\w+) tradeHost=(\w+) '
                      r'cargoHost=(\w+) questHost=(\w+) shuttles=(\d+) markets=(\d+)', log)
    if (not match or match[1] != seed_id or match[2] != '2'
            or any(v not in ('0', 'false') for v in match.groups()[2:6]) or match.groups()[6:] != ('2', '2')):
        problems.append('native role/fixture readback differs')
    return problems


def finish(close=False):
    state = read_json(STATE) if STATE.exists() else {}
    if state.get('stage') != 'launched':
        raise RuntimeError('Nothing launched by this harness.')
    if close and x4_running():
        subprocess.run(['taskkill', '/IM', 'X4.exe', '/F'], capture_output=True)
        for _ in range(60):
            if not x4_running():
                break
            time.sleep(1)
    if x4_running():
        raise RuntimeError('Close X4 first.')
    run, profile = Path(state['run']), Path(state['profile'])
    log = profile / 'x4lan-debug.txt'
    text = log.read_text(encoding='utf-8', errors='replace') if log.is_file() else ''
    if log.is_file():
        shutil.copy2(log, run / 'native.log')
    seed_id = read_json(run / 'manifest.json')['seed_id']
    problems = readiness(text, seed_id)
    owned = Path(state['owned_save'])
    changed = saves_changed(state['saves'], run_saves(state))
    if changed:
        problems.append('preexisting or new saves changed: ' + ', '.join(changed))
    try:
        owned_save_only(state)
    except RuntimeError as error:
        problems.append(str(error))
    result = dict(slot='p2', seed_id=seed_id, seed_sha256=state['owned_save_sha256'], head=state['head'],
                  native_seed_loaded=not problems, normal_saves_unchanged=not changed, problems=problems,
                  two_native_acceptance=False, ongoing_world_authority=False)
    if not problems:
        owned.unlink()                                      # exact run-owned bytes, verified above
        state.update(owned_save=None, owned_save_removed=True)
        result['owned_save_removed'] = True
    state.update(stage='finished')
    write_state(state)
    (run / 'windows-result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def removal_mismatch(state, package, owned):
    """Why delegating to setup.ps1 Remove could touch anything but this run's mod (None if safe)."""
    if tree_hashes(owned) not in (state.get('installed'), state.get('overlaid')):
        return 'mod at ' + str(owned) + ' differs from what this run installed'
    record = Path(package) / 'installation.json'
    if not record.is_file() or sha256(record) != state.get('installed_record_sha256'):
        return 'package installation.json is not the record this run installed'
    try:
        game, profile, _ = record_paths(package)
    except (OSError, KeyError, ValueError, RuntimeError):
        return 'package installation.json is unreadable'
    if str(game) != state['game'] or str(profile) != state['profile'] or game / 'extensions' / 'x4_lan_lab' != owned:
        return 'package installation.json names another game, profile or mod folder'
    return None


def unproven_targets(state, package):
    """Existing mod folders that may stem from an Install whose outcome this run never proved."""
    games = {state.get('game')}
    try:
        games.add(str(record_paths(package)[0]))
    except (OSError, KeyError, ValueError, RuntimeError):
        pass
    return sorted(str(Path(g) / 'extensions' / 'x4_lan_lab') for g in games
                  if g and (Path(g) / 'extensions' / 'x4_lan_lab').exists())


def record_changed(state, package):
    """Whether the package installation.json differs from the one backed up before Install."""
    record = Path(package) / 'installation.json'
    if not state['had_installation_json']:
        return record.exists()
    return not record.is_file() or sha256(record) != sha256(Path(state['run']) / 'windows-before/package-installation.json')


def restore():
    if not STATE.exists():
        raise RuntimeError('Nothing to restore.')
    state = read_json(STATE)
    if x4_running():
        raise RuntimeError('Close X4 first.')
    package, profile, run = Path(state['package']), Path(state['profile']), Path(state['run'])
    problems, summary = [], dict(stage=state['stage'], mod='untouched',
                                 save='removed by finish' if state.get('owned_save_removed') else 'none staged')
    if state.get('owned_save'):
        owned_save = Path(state['owned_save'])
        if not owned_save.exists():
            summary['save'] = 'already absent'
        elif sha256(owned_save) == state['owned_save_sha256']:
            owned_save.unlink()
            summary['save'] = 'run-owned seed removed'
        else:
            problems.append('staged seed changed; not removed')
            summary['save'] = 'changed, kept'
    owned = Path(state['owned_target']) if state.get('owned_target') else None
    keep_record = False
    if owned is not None and owned.exists():
        keep_record = True
        reason = removal_mismatch(state, package, owned)
        if reason:
            problems.append(reason + '; nothing removed, records kept')
            summary['mod'] = 'not removed: ' + reason
        else:
            setup_ps1(package, '-Action', 'Remove')
            if owned.exists():
                problems.append('mod still installed at ' + str(owned))
            else:
                keep_record = False
                summary['mod'] = 'removed by the package setup.ps1'
    elif owned is not None:
        summary['mod'] = 'already absent'
    else:
        # Ownership was never proven (Install refused, failed part-way or was interrupted). A mod
        # folder at the journaled game or at the game of the CURRENT record may be a partial
        # install of this run or someone else's: never remove it, and keep the current record
        # (the package's 5-REMOVE-MOD needs it) and this journal until that folder is gone.
        present = unproven_targets(state, package)
        changed = record_changed(state, package)
        if present:
            keep_record = True
            problems.append('Install outcome unproven and a mod folder exists at ' + ', '.join(present) +
                            ': nothing removed; check it, remove the test install with the package 5-REMOVE-MOD.cmd, '
                            'then run restore again')
            summary['mod'] = 'unproven, kept'
        # The real installer copies the mod before it writes its record, so each outcome differs.
        summary['install_outcome'] = (('partial: mod folder and new record' if changed else
                                       'partial: mod folder without a new record') if present else
                                      ('record changed without a mod folder' if changed else 'no install effects found'))
    backup = run / 'windows-before'
    log = profile / 'x4lan-debug.txt'
    if log.is_file() and not (run / 'native.log').is_file():
        shutil.copy2(log, run / 'native.log')               # keep this run's raw log before restoring
    for name in state['profile_files']:
        shutil.copy2(backup / name, profile / name)
    added = sorted(p.name for p in profile.iterdir() if p.is_file() and p.name not in state['profile_files'])
    summary['profile_files_added_by_game'] = added          # reported, never deleted
    save_dir = profile / 'save'
    if not state['save_dir_existed'] and save_dir.is_dir() and not any(save_dir.iterdir()):
        save_dir.rmdir()
    record = package / 'installation.json'
    if keep_record:
        summary['installation_json'] = 'kept: run-owned mod unresolved' if owned else 'kept: Install outcome unproven'
    elif state['had_installation_json']:
        shutil.copy2(backup / 'package-installation.json', record)
        summary['installation_json'] = 'restored'
    else:
        if record.exists():
            record.unlink()
        summary['installation_json'] = 'absent as before'
    mismatched = [n for n in state['profile_files'] if sha256(profile / n) != sha256(backup / n)]
    problems += ['profile file not restored: ' + n for n in mismatched]
    summary['profile_files_exact'] = len(state['profile_files']) - len(mismatched)
    current = run_saves(state)
    (run / 'saves-after.json').write_text(json.dumps(current, indent=2))
    changed = saves_changed(state['saves'], current)
    summary.update(saves_checked=len(state['saves']), saves_changed=changed,
                   save_inventories=['saves-before.json', 'saves-after.json'])
    if changed:
        problems.append('saves changed: ' + ', '.join(changed))
    summary['problems'] = problems
    (run / 'windows-restore.json').write_text(json.dumps(summary, indent=2))
    if problems:
        raise RuntimeError('Restore incomplete: ' + '; '.join(problems))
    STATE.unlink()
    print('Restored: mod', summary['mod'] + '; save', summary['save'] + '; profile files exact, saves unchanged.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('check'); c.add_argument('--run', type=Path, required=True)
    c.add_argument('--package', type=Path, required=True); c.add_argument('--game', type=Path)
    i = sub.add_parser('install'); i.add_argument('--run', type=Path, required=True)
    i.add_argument('--package', type=Path, required=True); i.add_argument('--host', required=True)
    i.add_argument('--game', type=Path)
    l = sub.add_parser('launch'); l.add_argument('--confirm-rendezvous', action='store_true')
    f = sub.add_parser('finish'); f.add_argument('--close', action='store_true')
    sub.add_parser('restore')
    args = parser.parse_args(argv)
    if args.command == 'check':
        facts = preflight(args.run, args.package, args.game)
        print('Preflight OK: seed', facts['manifest']['seed_id'], 'content matches; nothing changed.')
    elif args.command == 'install':
        install(args.run, args.package, args.host, args.game)
    elif args.command == 'launch':
        launch(args.confirm_rendezvous)
    elif args.command == 'finish':
        finish(args.close)
    else:
        restore()


if __name__ == '__main__':
    main()
