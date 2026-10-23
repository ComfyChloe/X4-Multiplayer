"""Build INTERNAL Windows/Linux host-or-guest packages without session secrets."""
from pathlib import Path
import argparse, hashlib, json, re, secrets, shutil, tarfile, tempfile, zipfile
SOURCE = Path(__file__).resolve().parents[1]
DIST = SOURCE / 'distribution'
BUILD = '23660954'
SHIP_MACRO = 'ship_ter_s_xperimental_01_a_macro'
SHIP_LOADOUT = 'x4lan_coop_xperimental'
SHIP_WEAPONS = 6

def version():
    value = (SOURCE / 'VERSION.txt').read_text().strip()
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('VERSION.txt must contain major.minor.patch.')
    return value

def write(path, text):
    path.write_text(text, encoding='utf-8')

def document(source, destination, release):
    write(destination, source.read_text().replace('{{VERSION}}', release))

def build(destination: Path, runtime_cache: Path | None = None):
    release = version()
    reports = [SOURCE / f'reports/{name}-{release}-{suffix}.txt'
               for name, suffix in [('automated', 'tests'), ('release', 'checks')]]
    for report in reports:
        if not report.is_file():
            raise RuntimeError('Record exact release checks before packaging: ' + str(report))
    runtimes = json.loads((DIST / 'public/runtimes.json').read_text())
    if runtime_cache:
        for platform in ('Linux', 'Windows'):
            record = runtimes[platform]
            if hashlib.sha256((runtime_cache / record['file']).read_bytes()).hexdigest() != record['sha256']:
                raise ValueError('Runtime checksum mismatch: ' + platform)
        if not (runtime_cache / 'licenses').is_dir(): raise ValueError('Runtime licenses are required.')
    destination.mkdir(parents=True, exist_ok=False)
    pair_id = 'internal-dist-' + secrets.token_hex(8)
    folders = []
    for platform in ('Linux', 'Windows11'):
        root = destination / f'X4-LAN-{release}-{platform}'
        root.mkdir(); folders.append(root)
        package = dict(package_id=None, pair_id=pair_id, slot=None, platform=platform,
                       release=7, version=release, channel='internal', steam_build=BUILD,
                       game_version='9.00 (611726)', test_profile='45841',
                       runtime_bundled=bool(runtime_cache), roles=['host', 'guest'])
        write(root / 'package.json', json.dumps(package, indent=2) + '\n')
        write(root / 'VERSION.txt', release + '\n')
        extension = root / 'extension/x4_lan_lab'
        shutil.copytree(SOURCE / 'extension/x4_lan_lab', extension)
        content = extension / 'content.xml'
        write(content, content.read_text().replace('name="X4 LAN Lab (experimental)"',
              f'name="X4 LAN Lab {release} (experimental)"'))
        for name in ('aiscripts/x4lan.interaction.fire.xml', 'aiscripts/x4lan.interaction.trade.xml', 'md/x4lan_interactions.xml'):
            (extension / name).unlink()
        (extension / 'native' / ('x4lan_net_64.dll' if platform == 'Linux' else 'x4lan_net.so')).unlink()
        write(extension / 'ui/config.lua', '-- Run 1-INSTALL before using this package.\nX4LAN_CONFIG = {\n'
              '    enabled = false,\n' + f'    ship_macro = "{SHIP_MACRO}",\n'
              + f'    ship_loadout = "{SHIP_LOADOUT}",\n    ship_weapons = {SHIP_WEAPONS},\n' + '}\n')
        for name in ('INSTALL.html', 'README.txt', 'CHANGELOG.txt', 'NEXT-TEST.txt'):
            document(DIST / name, root / name, release)
        shutil.copy2(SOURCE / 'VERSIONING.txt', root / 'VERSIONING.txt')
        shutil.copy2(DIST / 'WORLD-SYNC-TEST.txt', root / 'WORLD-SYNC-TEST.txt')
        for name in ('TEST-REPORT.txt', 'RELEASE-CHECKS.txt'): shutil.copy2(reports[1], root / name)
        shutil.copy2(reports[0], root / 'AUTOMATED-TESTS.txt')
        shutil.copy2(DIST / 'session_launcher.py', root / 'launcher.py')
        shutil.copy2(DIST / 'profile_settings.py', root / 'profile_settings.py')
        shutil.copy2(DIST / 'world_launcher.py', root / 'world_launcher.py')
        shutil.copy2(DIST / 'action.py', root / 'action.py')
        (root / 'x4lan').mkdir()
        for name in ('__init__', 'host', 'bubble', 'economy', 'coop', 'quests', 'monitor', 'universe', 'cargo', 'identity', 'seed'):
            shutil.copy2(SOURCE / 'x4lan' / (name + '.py'), root / 'x4lan' / (name + '.py'))
        if platform == 'Linux': shutil.copy2(DIST / 'linux_setup.py', root / 'setup_linux.py')
        else: shutil.copy2(DIST / 'windows_setup.ps1', root / 'setup_windows.ps1')
        if runtime_cache:
            runtime = root / 'runtime'
            if platform == 'Linux':
                with tempfile.TemporaryDirectory(prefix='x4-internal-runtime-') as temp:
                    with tarfile.open(runtime_cache / runtimes['Linux']['file']) as archive:
                        archive.extractall(temp, filter='data')
                    shutil.copytree(Path(temp) / 'python', runtime, symlinks=True)
            else:
                with zipfile.ZipFile(runtime_cache / runtimes['Windows']['file']) as archive:
                    archive.extractall(runtime)
                write(runtime / 'python314._pth', 'python314.zip\n.\n..\n')
            shutil.copytree(runtime_cache / 'licenses', root / 'licenses/python')
            write(root / 'RUNTIMES.json', json.dumps(runtimes, indent=2) + '\n')
        actions = {'1-INSTALL': 'setup', '2-START-SERVER': 'server', '2-TEST-CONNECTION': 'test',
                   '3-START-X4-TEST': 'start', '4-DIAGNOSTICS': 'diagnose', '5-REMOVE-MOD': 'remove',
                   '6-COLLECT-CONTAINER': 'collect', '7-VERIFY-PACKAGE': 'verify',
                   '9-IMPORT-SETTINGS': 'settings', '2-PREPARE-WORLD': 'prepare-world'}
        for title, action in actions.items():
            if platform == 'Linux':
                command = '"./runtime/bin/python3"' if runtime_cache else 'python3'
                path = root / (title + '.sh')
                write(path, '#!/usr/bin/env bash\nset -u\ncd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1\n'
                      + command + ' -I -B launcher.py ' + action + ' "$@"\nresult=$?\n'
                      'if [ -t 0 ]; then read -r -p "Press Enter to close... " answer; fi\nexit "$result"\n')
                path.chmod(0o755)
            else:
                command = '"%~dp0runtime\\python.exe"' if runtime_cache else 'python'
                (root / (title + '.cmd')).write_bytes(('@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
                    + command + ' -I -B "%~dp0launcher.py" ' + action + ' %*\r\n'
                    'set "X4LAN_RESULT=%ERRORLEVEL%"\r\necho.\r\npause\r\nexit /b %X4LAN_RESULT%\r\n').encode('ascii'))
        for cache in root.rglob('__pycache__'): shutil.rmtree(cache)
        files = sorted(p for p in root.rglob('*') if p.is_file())
        write(root / 'SHA256SUMS.txt', ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(root).as_posix() + '\n' for p in files))
        archive = destination / (root.name + '.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for path in sorted(root.rglob('*')):
                if path.is_file(): z.write(path, path.relative_to(destination))
    write(destination / 'PACKAGES-SHA256.txt', ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in sorted(destination.glob('*.zip'))))
    for name in ('INSTALL.html', 'README.txt', 'NEXT-TEST.txt'): document(DIST / name, destination / name, release)
    shutil.copy2(DIST / 'WORLD-SYNC-TEST.txt', destination / 'WORLD-SYNC-TEST.txt')
    shutil.copy2(SOURCE / 'VERSIONING.txt', destination / 'VERSIONING.txt')
    shutil.copy2(reports[1], destination / 'RELEASE-CHECKS.txt')
    write(destination / 'VERSION.txt', release + '\n')
    print('Internal host-or-guest packages created:', destination)
    return folders

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    parser.add_argument('--runtime-cache', type=Path, required=True)
    args = parser.parse_args()
    build(args.destination, args.runtime_cache)
