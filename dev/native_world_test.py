"""Local W01 engine harness. Isolated profile only; never opens a saved game.

Install/launch are explicit commands. The peer command exercises real UDP and
records evidence without claiming that synthetic peers replace a two-PC test.
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
import xml.etree.ElementTree as ET

SOURCE = Path(__file__).resolve().parents[1]
ROOT = SOURCE
HERE = SOURCE / '.local/native-world-tests'
GAME = Path.home() / '.local/share/Steam/steamapps/common/X4 Foundations'
PROFILE = Path.home() / '.config/EgoSoft/X4/45841'
TARGET = GAME / 'extensions/x4_lan_lab'
SECTOR = 'cluster_14_sector001_macro'
sys.path.insert(0, str(SOURCE))
spec = importlib.util.spec_from_file_location('internal_setup', SOURCE / 'distribution/linux_setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def saves():
    return {str(p): {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'mtime_ns': p.stat().st_mtime_ns}
            for p in PROFILE.parent.glob('*/save/*') if p.is_file()}


def install(slot, shared_ship=False):
    setup.ensure_closed()
    setup.check_game(GAME, '23660954')
    if TARGET.is_symlink() or not (TARGET/'.x4lan-package-id').is_file():
        raise RuntimeError('A known internal package must be installed first; refusing an unknown target.')
    if not (TARGET/'.x4lan-package-id').read_text().startswith('lan-'):
        raise RuntimeError('Only the internal installation may be replaced by this harness.')
    HERE.mkdir(parents=True, exist_ok=True)
    backup = HERE / 'native-before'
    if not backup.exists():
        backup.mkdir()
        shutil.copytree(TARGET, backup / 'extension')
        for name in ('config.xml', 'content.xml', 'x4lan-debug.txt'):
            if (PROFILE / name).exists(): shutil.copy2(PROFILE / name, backup / name)
        (backup / 'saves.json').write_text(json.dumps(saves(), indent=2))
    setup.configure_profile(PROFILE, backup / 'settings')
    # Disable non-project third-party extensions in this profile only.
    content = ET.Element('content')
    for path in sorted((GAME / 'extensions').glob('*/content.xml')):
        info = ET.parse(path).getroot()
        id = info.get('id')
        if id: ET.SubElement(content, 'extension', id=id,
                             enabled='true' if id == 'x4_lan_lab' or id.startswith('ego_dlc_') else 'false')
    ET.ElementTree(content).write(PROFILE / 'content.xml', encoding='utf-8', xml_declaration=True)
    cfg = dict(token=secrets.token_hex(16), build='world1-9.00-steam23660954', bubble='world-dev',
               port=45843, coop=True, fire2=True, world_sync=True, world_mode='replica')
    if shared_ship:
        cfg.update(ship_macro='ship_ter_s_xperimental_01_a_macro', ship_loadout='x4lan_coop_xperimental', ship_weapons=6)
    (HERE / 'native-session.json').write_text(json.dumps(cfg, indent=2))
    # copytree overlays production files; opt-in MD probes from an earlier run
    # must not survive and silently seed/mutate a later disposable test world.
    for probe in (SOURCE / 'dev').glob('*probe.*'):
        subdir = {'.lua': 'ui', '.xml': 'md'}.get(probe.suffix)
        if subdir:
            (TARGET / subdir / probe.name).unlink(missing_ok=True)
    shutil.copytree(SOURCE / 'extension/x4_lan_lab', TARGET, dirs_exist_ok=True)
    values = dict(enabled=True, bubble_actions=True, trading=True, walking_avatars=False,
                  test_profile='45841', host='127.0.0.1', slot=slot, mode='proxy',
                  proxy_macro='ship_arg_s_scout_01_a_macro', **cfg)
    (TARGET / 'ui/config.lua').write_text('X4LAN_CONFIG={\n' + ''.join(
        ' '+k+'='+json.dumps(v)+',\n' for k, v in values.items()) + '}\n')
    tree = ET.parse(TARGET / 'ui.xml')
    ET.SubElement(tree.find('environment'), 'file', name='ui/world_probe.lua')
    tree.write(TARGET / 'ui.xml', encoding='utf-8', xml_declaration=True)
    shutil.copy2(SOURCE / 'dev/world_probe.lua', TARGET / 'ui/world_probe.lua')
    (HERE / ('installed-'+slot+'-hashes.json')).write_text(json.dumps({
        p.relative_to(TARGET).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in TARGET.rglob('*') if p.is_file()},indent=2))
    old_log = PROFILE / 'x4lan-debug.txt'
    if old_log.exists():
        shutil.copy2(old_log, HERE / ('before-' + slot + '-' + str(time.time_ns()) + '.log'))
        old_log.write_text('')
    print('Installed W01 native probe for', slot, 'in isolated profile 45841; original backed up.')


def peer(slot, duration, replay=False, config=None, baselines=8, motion=False, fixtures=False):
    from x4lan.host import Relay
    from x4lan.universe import UniverseSession, Receiver, Baseline, Sender
    cfg = json.loads((config or HERE / 'native-session.json').read_text())
    session = UniverseSession(cfg['token'], cfg['build'], cfg['bubble'])
    relay = Relay('127.0.0.1', cfg['port'], session)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(('127.0.0.1', cfg['port'])); sock.setblocking(False)
    nonce = secrets.token_hex(16); lease = None
    seq = 0; next_send = next_hello = 0; start = time.monotonic()
    receiver = Receiver(); sender = None; history = []; metrics = []; next_metric = 0
    native_start = None; phase = None; next_baseline = 0; revision = 0
    replay_rows = json.loads((HERE/'native-host-result.json').read_text())['history'][-1]['rows'] if replay else None
    try:
        while time.monotonic() - start < duration:
            now = time.monotonic(); relay.step(now)
            if now >= next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|{slot}|{nonce}".encode())
                next_hello = now + 1
            for _ in range(256):
                try: f = sock.recv(769).decode().split('|')
                except BlockingIOError: break
                if f[0] == 'WELCOME': lease = f[3]
                elif f[0] in ('UBEGIN', 'UOBJECT', 'UCOMMIT') and lease:
                    ack, b = receiver.receive(f, now)
                    if ack is not None: sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
                    if b:
                        item = dict(at=round(now-start, 2), revision=b.revision, sector=b.sector,
                                    objects=len(b.rows), digest=b.digest, rows=b.rows)
                        history.append(item)
                        print('Native host baseline:', b.revision, b.sector, len(b.rows), flush=True)
                elif f[0] == 'UACK' and sender:
                    sender.acknowledge(f[3], int(f[4]), int(f[5]))
            native = session.peers.get('p1' if slot == 'p2' else 'p2')
            if native and native_start is None: native_start = now
            if lease and now >= next_send:
                seq += 1; next_send = now + .05
                sock.send(f'IDLE|1|{lease}|{seq}'.encode())
                sock.send(f'CREW|1|{lease}|{seq}|0|0'.encode())
                if slot == 'p2':
                    sock.send(f'UINTEREST|1|{lease}|{seq}|{SECTOR}'.encode())
                else:
                    world = [100,100,0,100,100,0,100,100,100,100,200,1,1,0,0,0,0]
                    sock.send(('WORLD|1|'+lease+'|'+str(seq)+'|'+'|'.join(map(str,world))).encode())
                    if native_start:
                        elapsed = now-native_start
                        new_phase = 'create' if elapsed < 22 else 'move' if elapsed < 44 else 'empty'
                        if replay and elapsed >= 62: new_phase = 'full-sector' if elapsed < 90 else 'final-empty'
                        if (not sender or sender.done) and now >= next_baseline:
                            revision += 1; next_baseline = now + 2
                            x = 1800 if new_phase == 'create' else 1800+100*(elapsed-22) if motion else 3800
                            rows = [] if new_phase in ('empty','final-empty') else replay_rows if new_phase == 'full-sector' else [
                                f'1,ship,ship_arg_s_scout_01_a_macro,argon,{x},10000,1200,0,0,0,none,0',
                                f'2,ship,ship_arg_m_trans_container_01_a_macro,argon,{x+800},10000,2400,0,0,0,none,0']
                            sender = Sender(Baseline.create(lease, revision, SECTOR, rows))
                            if phase != new_phase:
                                phase = new_phase; history.append(dict(at=round(now-start, 2), phase=phase))
                                print('Native guest phase:', phase, flush=True)
                        # Exercise identity/contact packets independently of baseline data.
                        if sender and new_phase not in ('empty','final-empty'):
                            for row in sender.baseline.rows[:2]:
                                entity = int(row.split(',')[0])
                                name = ('Shared native ship '+str(entity)).encode().hex()
                                sock.send(f'IDENT|1|{lease}|{lease}|{seq}|N|{entity}|TST-00{entity}|{name}|1|1'.encode())
                        if fixtures and sender:
                            for scope,count in (('P',2),('F',4)):
                                for entity in range(1,count+1):
                                    name=('Shared fixture '+scope+str(entity)).encode().hex()
                                    code=scope+'XT-00'+str(entity)
                                    sock.send(f'IDENT|1|{lease}|{lease}|{seq}|{scope}|{entity}|{code}|{name}|1|1'.encode())
                        if new_phase == 'move':
                            if motion:
                                # Deliberate 0.9s loss bursts test bounded prediction/freeze.
                                if (elapsed-22)%6 < 5.1:
                                    x=1800+100*(elapsed-22);yaw=.1*(elapsed-22)
                                    sock.send(f'UMOVE|1|{lease}|{lease}|1|{seq}|{SECTOR}|{now:.6f}|{x:.6f}|10000|1200|{yaw:.6f}|0|0|100|0|0|.1|0|0'.encode())
                                    sock.send(f'NAV2|1|{lease}|1|1|{seq}|{SECTOR}|{-1000+50*(elapsed-22):.6f}|10000|0|0|0|0|0|0|{now:.6f}|50|0|0|0|0|0'.encode())
                            else:
                                sock.send(f'UPOS|1|{lease}|{lease}|1|{seq}|{SECTOR}|3800|10000|1200|0|0|0'.encode())
                        if elapsed > (106 if replay else 62): break
            if sender and lease:
                for packet in sender.packets(lease, now): sock.send(packet)
            if now >= next_metric:
                metrics.extend(dict(v, slot=s) for s,p in session.peers.items() if (v:=session.game_metrics.get(p.lease)))
                next_metric = now+1
            if slot == 'p2' and len(history) >= baselines: break
            if native_start and now-native_start > 100 and not history: break
            time.sleep(.005)
    finally:
        report = dict(synthetic_slot=slot, motion=motion, duration=round(time.monotonic()-start,2), history=history,
                      metrics=metrics, guest_committed_revision=session.delivered,
                      known_positions=len(session.positions))
        name = 'host' if slot == 'p2' else 'guest'
        (HERE / ('native-'+name+'-result.json')).write_text(json.dumps(report, indent=2))
        if (PROFILE / 'x4lan-debug.txt').exists(): shutil.copy2(PROFILE/'x4lan-debug.txt', HERE/('native-'+name+'.log'))
        sock.close(); relay.close()
        print('Recorded native', name, 'evidence.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install','launch','peer','check-saves'])
    parser.add_argument('--slot', choices=['p1','p2'], default='p1')
    parser.add_argument('--duration', type=int, default=600)
    parser.add_argument('--fixtures',action='store_true',help='Exercise player/fixture display identities on a native guest.')
    parser.add_argument('--motion', action='store_true', help='Exercise UMOVE/NAV2 with velocity and loss bursts.')
    parser.add_argument('--shared-ship', action='store_true', help='Verify the matched Timelines shuttle loadout.')
    parser.add_argument('--replay', action='store_true', help='Also replay the recorded complete native host sector.')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--game', type=Path, help='Override the native X4 game directory on this computer.')
    parser.add_argument('--baselines', type=int, default=8)
    args = parser.parse_args()
    if args.game:
        GAME = args.game.expanduser().resolve()
        TARGET = GAME / 'extensions/x4_lan_lab'
    if args.action == 'install': install(args.slot,args.shared_ship)
    elif args.action == 'launch':
        setup.ensure_closed()
        subprocess.run(['steam'] + setup.GAME_ARGS, check=True)
    elif args.action == 'peer': peer(args.slot, args.duration,args.replay,args.config,args.baselines,args.motion,args.fixtures)
    else:
        before=json.loads((HERE/'native-before/saves.json').read_text())
        after=saves(); assert before.keys() == after.keys(), 'Save file inventory changed during native test'
        for path,value in before.items():
            assert value['sha256'] == after[path]['sha256'], 'Saved file bytes changed: '+path
            if Path(path).name != 'steam_autocloud.vdf':
                assert value['mtime_ns'] == after[path]['mtime_ns'], 'Save modification time changed: '+path
        (HERE/'save-preservation.json').write_text(json.dumps(dict(
            all_file_bytes_unchanged=True, actual_save_timestamps_unchanged=True,
            no_test_saves=True, steam_metadata_timestamp_changed=any(
                before[p]['mtime_ns'] != after[p]['mtime_ns'] for p in before)),indent=2))
        print('All save bytes and actual save timestamps unchanged; no test save created. Steam metadata may refresh its timestamp.')
