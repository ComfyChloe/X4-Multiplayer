"""Offline Public Test launcher. No downloads, game assets or global installation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
VERSION = '6.0.1'
TEST_ID = 'PT01'
PROFILE = '45842'
PORT = 45842
BUILD = 'public-pt01-6.0.1-9.00-steam23660954'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def public_ip(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or str(ip) == '255.255.255.255':
        raise ValueError('Use the host LAN or ZeroTier IPv4, not localhost or a broadcast address.')
    return str(ip)


def validate_invite(value):
    keys = {'format', 'channel', 'version', 'test_id', 'steam_build', 'build',
            'bubble', 'port', 'coop', 'token', 'server'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('Not a Public Test 01 session file.')
    expected = dict(format=1, channel='public', version=VERSION, test_id=TEST_ID,
                    steam_build='23660954', build=BUILD, port=PORT, coop=True)
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items()):
        raise ValueError('Session version/channel does not match Public Test 01.')
    if not isinstance(value['token'], str) or not re.fullmatch('[0-9a-f]{32}', value['token']):
        raise ValueError('Invalid session key.')
    if not isinstance(value['bubble'], str) or not re.fullmatch('public-pt01-[0-9a-f]{16}', value['bubble']):
        raise ValueError('Invalid session identity.')
    public_ip(value['server'])
    return value


@contextmanager
def session_lock():
    # Shared by configure and server. A stale file after a crash holds no OS lock.
    with (ROOT / '.session.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('The server or another setup action is running in this folder. Close it first.') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def backend_module(name):
    spec = importlib.util.spec_from_file_location('public_' + name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_closed():
    if sys.platform == 'win32':
        result = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq X4.exe', '/FO', 'CSV', '/NH'],
                                capture_output=True, text=True, check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if '"x4.exe"' in result.stdout.lower():
            raise RuntimeError('Close X4 before changing the session or installation.')
    else:
        backend_module('setup_linux').ensure_closed()


def configured():
    package = read_json(ROOT / 'package.json')
    if package.get('channel') != 'public' or package.get('version') != VERSION:
        raise RuntimeError('This launcher requires a Public Test 01 package.')
    if package.get('slot') not in ('p1', 'p2') or not (ROOT / 'session.json').is_file():
        raise RuntimeError('Run 1-SETUP first to create or join a session.')
    session = read_json(ROOT / 'session.json')
    validate_invite({k: v for k, v in session.items() if k != 'host'})
    expected_host = '127.0.0.1' if package['slot'] == 'p1' else session['server']
    if session.get('host') != expected_host:
        raise RuntimeError('Session role/address mismatch. Remove the mod and run setup again.')
    return package, session


def configure(role, server=None, invite=None):
    # Only uninstalled package-local templates change here. Server-only setup is
    # safe while an unrelated X4 instance runs; installation still checks X4.
    with session_lock():
        if (ROOT / 'installation.json').exists():
            raise RuntimeError('Remove the installed mod with 5-REMOVE-MOD before changing roles or sessions.')
        if (ROOT / 'session.json').exists():
            raise RuntimeError('This folder already has a session. Reuse it, or use a fresh extracted package for a new session.')
        if role == 'host':
            session = dict(format=1, channel='public', version=VERSION, test_id=TEST_ID,
                           steam_build='23660954', build=BUILD, bubble='public-pt01-' + secrets.token_hex(8),
                           port=PORT, coop=True, token=secrets.token_hex(16), server=public_ip(server))
        elif role == 'guest':
            path = Path(invite).expanduser()
            if path.stat().st_size > 8192: raise ValueError('Session file is too large.')
            session = validate_invite(read_json(path))
        else:
            raise ValueError('Choose host or guest.')
        package = read_json(ROOT / 'package.json')
        if package.get('channel') != 'public' or package.get('version') != VERSION:
            raise RuntimeError('Not a matching public package.')
        package['slot'] = 'p1' if role == 'host' else 'p2'
        package['package_id'] = 'public-pt01-' + hashlib.sha256(session['token'].encode()).hexdigest()[:16] + '-' + package['slot']
        host = '127.0.0.1' if role == 'host' else session['server']
        cfg = dict(enabled=True, interaction_test=False, bubble_actions=True, trading=True,
                   walking_avatars=False, test_profile=PROFILE, host=host, port=PORT,
                   token=session['token'], build=BUILD, bubble=session['bubble'], coop=True,
                   slot=package['slot'], mode='proxy', proxy_macro='ship_arg_s_scout_01_a_macro')
        lua = '-- Public Test 01. Private session configuration; do not publish.\nX4LAN_CONFIG = {\n'
        lua += ''.join('    ' + k + ' = ' + json.dumps(v) + ',\n' for k, v in cfg.items()) + '}\n'
        (ROOT / 'extension/x4_lan_lab/ui/config.lua').write_text(lua, encoding='utf-8')
        write_json(ROOT / 'package.json', package)
        if role == 'host': write_json(ROOT / 'JOIN-SESSION.json', session)
        write_json(ROOT / 'session.json', {**session, 'host': host})
    print('Session configured as ' + role.upper() + ' (' + package['slot'] + ').')
    if role == 'host':
        print('Send JOIN-SESSION.json privately to your other player. Keep it out of public downloads.')


def platform_action(action, game=None):
    package, session = configured()
    if action in ('install', 'start', 'uninstall'): ensure_closed()
    if action != 'install':
        state = read_json(ROOT / 'installation.json')
        if state['package_id'] != package['package_id'] or Path(state['profile']).name != PROFILE:
            raise RuntimeError('Installation does not belong to this public session/profile.')
        target = Path(state['game']) / 'extensions/x4_lan_lab'
        marker = target / '.x4lan-package-id'
        if action in ('start', 'uninstall') and (not marker.is_file() or marker.read_text(encoding='utf-8-sig') != package['package_id']):
            raise RuntimeError('The installed mod belongs to another package. Use its original launcher.')
    if sys.platform == 'win32':
        names = dict(install='Install', start='Start', diagnose='Diagnose', uninstall='Remove')
        cmd = ['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', str(ROOT / 'setup_windows.ps1'), '-Action', names[action]]
        if action == 'install':
            cmd += ['-ServerIP', session['host']]
            if game: cmd += ['-GamePath', game]
        subprocess.run(cmd, check=True)
    else:
        module = backend_module('setup_linux')
        if action == 'install': module.install(argparse.Namespace(game=game, steam_kind=None))
        else: getattr(module, action)()
    if action == 'uninstall':
        # The backend has archived the owned mod. Retain its state as evidence.
        destination = ROOT / 'backups' / ('installation-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
        (ROOT / 'installation.json').replace(destination)


def test_connection():
    _, cfg = configured()
    nonce = secrets.token_hex(16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.connect((cfg['host'], cfg['port']))
        for _ in range(3):
            try:
                sock.send(f'PING|1|{cfg["token"]}|{nonce}'.encode())
                if sock.recv(769) == f'PONG|1|{nonce}|{cfg["build"]}|{cfg["bubble"]}'.encode():
                    print('CONNECTION OK. Matching Public Test 01 session; no player slot occupied.')
                    return
            except OSError:
                continue
    raise RuntimeError('No matching server reply. Check the host address, session file, server and UDP 45842 firewall rule.')


def server():
    package, cfg = configured()
    if package['slot'] != 'p1': raise RuntimeError('Only the HOST starts the server. The GUEST uses 2-TEST-CONNECTION.')
    with session_lock():
        from x4lan.monitor import run
        print('Public Test 01 server. Keep this window open. Ctrl+C stops it.', flush=True)
        run(cfg, ROOT / 'logs', version=VERSION + ' Public Test 01')


def diagnostics():
    package, cfg = configured()
    if (ROOT / 'installation.json').exists(): platform_action('diagnose')
    logs = ROOT / 'logs'; logs.mkdir(exist_ok=True)
    write_json(logs / 'public-test-info.json', dict(version=VERSION, test_id=TEST_ID,
        channel='public', platform=package['platform'], role=package['slot'],
        python=sys.version.split()[0], steam_build=package['steam_build'],
        collected_at=datetime.now(timezone.utc).isoformat()))
    # Engine logs can include diagnostic payloads; scrub this session's key.
    for path in logs.iterdir():
        if path.is_file() and path.suffix in ('.txt', '.log', '.json', '.jsonl'):
            text = path.read_text(encoding='utf-8', errors='replace')
            if cfg['token'] in text: path.write_text(text.replace(cfg['token'], '[SESSION KEY REDACTED]'), encoding='utf-8')
    print('Diagnostics:', logs)
    print('Include TEST-REPORT-TEMPLATE.txt. Logs contain local paths/IPs; review before sharing.')


def verify():
    configured_files = {'package.json', 'extension/x4_lan_lab/ui/config.lua'} if (ROOT / 'session.json').exists() else set()
    count = 0
    for row in (ROOT / 'SHA256SUMS.txt').read_text().splitlines():
        expected, name = row.split('  ', 1)
        path = ROOT / name
        if not path.resolve().is_relative_to(ROOT): raise ValueError('Invalid checksum path.')
        if name in configured_files: continue
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Missing or changed package file: ' + name)
        count += 1
    if configured_files: configured()
    print(f'PASS: {count} package files verified.' + (' Session-specific configuration excluded.' if configured_files else ''))


def setup(args):
    if not (ROOT / 'session.json').exists():
        role = args.role or input('Role: [H]ost or [G]uest? ').strip().lower()
        role = {'h': 'host', 'g': 'guest'}.get(role, role)
        if role == 'host':
            from x4lan.monitor import addresses
            for row in addresses(): print(row['kind'] + ': ' + row['ip'] + ' (' + row['interface'] + ')')
            configure(role, server=args.server or input('Your host LAN/ZeroTier IPv4: ').strip())
        elif role == 'guest':
            configure(role, invite=args.invite or input('Path to the host JOIN-SESSION.json: ').strip().strip('"'))
        else: raise ValueError('Choose H for host or G for guest.')
    package, _ = configured()
    if not args.server_only and not (ROOT / 'installation.json').exists(): platform_action('install', args.game)
    print('Next: ' + ('2-START-SERVER, then ' if package['slot'] == 'p1' else '2-TEST-CONNECTION, then ') + '3-START-X4-TEST.')


def main():
    parser = argparse.ArgumentParser(description='X4 Multiplayer Public Test 01 - Windows/Linux host or guest')
    parser.add_argument('action', choices=['setup', 'server', 'test', 'start', 'diagnose', 'remove', 'collect', 'verify', 'version'])
    parser.add_argument('--role', choices=['host', 'guest'])
    parser.add_argument('--server', help='Host LAN/ZeroTier IPv4 when creating a session')
    parser.add_argument('--invite', help='Path to JOIN-SESSION.json when joining')
    parser.add_argument('--game', help='Override detected game installation')
    parser.add_argument('--server-only', action='store_true', help='Configure files without installing or launching X4')
    args = parser.parse_args()
    try:
        print('X4 Multiplayer ' + VERSION + ' - PUBLIC TEST 01', flush=True)
        if args.action == 'setup': setup(args)
        elif args.action == 'server': server()
        elif args.action == 'test': test_connection()
        elif args.action == 'start': platform_action('start')
        elif args.action == 'remove': platform_action('uninstall')
        elif args.action == 'diagnose': diagnostics()
        elif args.action == 'collect':
            configured()
            return backend_module('action').run('PICKUP')
        elif args.action == 'verify': verify()
        return 0
    except KeyboardInterrupt:
        print('\nStopped.'); return 130
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr); return 1


if __name__ == '__main__':
    raise SystemExit(main())
