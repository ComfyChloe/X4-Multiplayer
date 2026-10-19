"""Build a matched English-language pair; never modify a game installation."""
from pathlib import Path
import argparse, hashlib, json, re, secrets, shutil, zipfile

SOURCE = Path(__file__).resolve().parents[1]
DIST = SOURCE / 'distribution'
BUILD = '23660954'
SHIP_MACRO = 'ship_ter_s_xperimental_01_a_macro'  # Xperimental Shuttle, X4: Timelines
SHIP_LOADOUT = 'x4lan_coop_xperimental'  # extension/x4_lan_lab/libraries/loadouts.xml
# Exact equipped weapon count. The 7th slot (con_missilelauncher_01) stays empty:
# the macro has <storage missile="0"/>, matching vanilla Timelines start loadouts.
SHIP_WEAPONS = 6

def version():
    value = (SOURCE / 'VERSION.txt').read_text().strip()
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('VERSION.txt must contain major.minor.patch, for example 5.0.1.')
    return value

def document(source, destination, release):
    write(destination, source.read_text().replace('{{VERSION}}', release))


def write(path, text):
    path.write_text(text, encoding='utf-8')


def build(destination: Path):
    release = version()
    automated_report = SOURCE / ('reports/automated-' + release + '-tests.txt')
    release_report = SOURCE / ('reports/release-' + release + '-checks.txt')
    for report in (automated_report, release_report):
        if not report.is_file():
            raise RuntimeError('Record checks for this exact release before packaging: ' + str(report))
    destination.mkdir(parents=True, exist_ok=False)
    session = {'token': secrets.token_hex(16), 'build': 'world1-' + release + '-9.00-steam' + BUILD,
               'bubble': 'world-01', 'port': 45841, 'coop': True,
               'world_sync': True, 'world_mode': 'replica'}
    pair_id = 'lan-' + secrets.token_hex(8)
    folders = []
    for platform, slot in [('Linux', 'p1'), ('Windows11', 'p2')]:
        name = 'X4-LAN-' + release + '-' + platform
        root = destination / name
        root.mkdir(); folders.append(root)
        package = {'package_id': pair_id + '-' + slot, 'pair_id': pair_id,
                   'slot': slot, 'platform': platform, 'release': 7, 'version': release, 'channel': 'internal',
                   'steam_build': BUILD, 'game_version': '9.00 (611726)', 'test_profile': '45841'}
        write(root / 'package.json', json.dumps(package, indent=2) + '\n')
        write(root / 'VERSION.txt', release + '\n')
        write(root / 'session.json', json.dumps(session, indent=2) + '\n')
        extension = root / 'extension/x4_lan_lab'
        shutil.copytree(SOURCE / 'extension/x4_lan_lab', extension)
        content = extension / 'content.xml'
        write(content, content.read_text().replace('name="X4 LAN Lab (experimental)"',
              'name="X4 LAN Lab ' + release + ' (experimental)"'))
        for path in ['aiscripts/x4lan.interaction.fire.xml', 'aiscripts/x4lan.interaction.trade.xml', 'md/x4lan_interactions.xml']:
            (extension / path).unlink()
        (extension / 'native' / ('x4lan_net_64.dll' if slot == 'p1' else 'x4lan_net.so')).unlink()
        cfg = {'enabled': True, 'interaction_test': False, 'bubble_actions': True, 'trading': True, 'walking_avatars': False,
               'test_profile': '45841', 'host': '127.0.0.1' if slot == 'p1' else 'SET_HOST_IP_DURING_INSTALL',
               **session, 'slot': slot, 'mode': 'proxy', 'proxy_macro': 'ship_arg_s_scout_01_a_macro',
               # One shared ship spec for both players and proxies (requires X4: Timelines).
               'ship_macro': SHIP_MACRO, 'ship_loadout': SHIP_LOADOUT, 'ship_weapons': SHIP_WEAPONS}
        write(extension / 'ui/config.lua', '-- Paired test package. Configure with the installer.\nX4LAN_CONFIG = {\n' +
              ''.join('    ' + k + ' = ' + json.dumps(v) + ',\n' for k, v in cfg.items()) + '}\n')
        for name in ['INSTALL.html', 'README.txt', 'CHANGELOG.txt']:
            document(DIST / name, root / name, release)
        shutil.copy2(SOURCE / 'VERSIONING.txt', root / 'VERSIONING.txt')
        shutil.copy2(DIST / 'WORLD-SYNC-TEST.txt', root / 'WORLD-SYNC-TEST.txt')
        shutil.copy2(release_report, root / 'TEST-REPORT.txt')
        shutil.copy2(automated_report, root / 'AUTOMATED-TESTS.txt')
        shutil.copy2(release_report, root / 'RELEASE-CHECKS.txt')
        if slot == 'p1':
            shutil.copy2(DIST / 'linux_setup.py', root / 'setup.py')
            (root / 'x4lan').mkdir()
            for file in ['__init__.py', 'host.py', 'bubble.py', 'economy.py', 'coop.py', 'quests.py', 'monitor.py', 'universe.py', 'cargo.py', 'identity.py']:
                shutil.copy2(SOURCE / 'x4lan' / file, root / 'x4lan' / file)
            shutil.copy2(DIST / 'action.py', root / 'action.py')
            actions = {'1-INSTALL': 'setup.py install', '2-START-SERVER': 'setup.py server',
                       '3-START-X4-TEST': 'setup.py start', '4-DIAGNOSTICS': 'setup.py diagnose',
                       '5-REMOVE-MOD': 'setup.py uninstall', '6-COLLECT-CONTAINER': 'action.py PICKUP'}
            for title, action in actions.items():
                path = root / (title + '.sh')
                write(path, '#!/usr/bin/env bash\nset -u\ncd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1\n'
                      'if ! command -v python3 >/dev/null; then\n  echo "Python 3 is missing. See INSTALL.html."\n  exit 1\nfi\n'
                      'python3 ' + action + '\nresult=$?\n'
                      'if [ -t 0 ]; then read -r -p "Press Enter to close... " answer; fi\nexit "$result"\n')
                path.chmod(0o755)
        else:
            shutil.copy2(DIST / 'windows_setup.ps1', root / 'setup.ps1')
            shutil.copy2(DIST / 'action.ps1', root / 'action.ps1')
            actions = {'1-INSTALL': 'setup.ps1" -Action Install', '2-TEST-CONNECTION': 'setup.ps1" -Action Test',
                       '3-START-X4-TEST': 'setup.ps1" -Action Start', '4-DIAGNOSTICS': 'setup.ps1" -Action Diagnose',
                       '5-REMOVE-MOD': 'setup.ps1" -Action Remove', '6-COLLECT-CONTAINER': 'action.ps1" -Verb PICKUP'}
            for title, action in actions.items():
                (root / (title + '.cmd')).write_bytes(('@echo off\r\nsetlocal\r\n'
                    'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0' + action + '\r\n'
                    'set "X4LAN_RESULT=%ERRORLEVEL%"\r\necho.\r\necho Press any key to close...\r\npause >nul\r\n'
                    'exit /b %X4LAN_RESULT%\r\n').encode('ascii'))
        for cache in root.rglob('__pycache__'): shutil.rmtree(cache)
        files = sorted(p for p in root.rglob('*') if p.is_file())
        write(root / 'SHA256SUMS.txt', ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(root).as_posix() + '\n' for p in files))
        archive = destination / (root.name + '.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in sorted(root.rglob('*')):
                if p.is_file(): z.write(p, p.relative_to(destination))
        with zipfile.ZipFile(archive) as z: assert z.testzip() is None
    write(destination / 'PACKAGES-SHA256.txt', ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in sorted(destination.glob('*.zip'))))
    document(DIST / 'INSTALL.html', destination / 'INSTALL.html', release)
    document(DIST / 'README.txt', destination / 'README.txt', release)
    write(destination / 'VERSION.txt', release + '\n')
    shutil.copy2(SOURCE / 'VERSIONING.txt', destination / 'VERSIONING.txt')
    release_report = SOURCE / ('reports/release-' + release + '-checks.txt')
    if release_report.is_file(): shutil.copy2(release_report, destination / 'RELEASE-CHECKS.txt')
    print('Matched English packages created:', destination)
    return folders


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    build(parser.parse_args().destination)
