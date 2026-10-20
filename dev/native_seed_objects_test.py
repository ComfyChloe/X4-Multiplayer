"""Opt-in original-ship binding test, using only this run's pristine seed.

Capture/transfer first with native_seed_test. Native HOST and GUEST run
sequentially; the other peer is synthetic. This is not a joint playtest.
"""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dev import native_seed_play_test as play
from dev import native_seed_test as capture
from dev import native_world_test as native
from x4lan.host import Relay
from x4lan.universe import UniverseSession, Receiver, Baseline, Sender


def install(run, slot, trace_docked=False, skip_docked=False):
    if skip_docked and not trace_docked:
        raise ValueError('Diagnostic docked-write omission requires the native trace')
    play.install(run, slot)
    config = native.TARGET / 'ui/config.lua'
    config.write_text(config.read_text().replace('X4LAN_CONFIG={', 'X4LAN_CONFIG={\n seed_binding=true,'))
    if trace_docked:
        config.write_text(config.read_text().replace('X4LAN_CONFIG={',
            'X4LAN_CONFIG={\n seed_probe_trace_docked=true,seed_probe_skip_docked='+str(skip_docked).lower()+','))
    for suffix, sub in (('lua', 'ui'), ('xml', 'md')):
        shutil.copy2(ROOT / ('dev/seed_objects_probe.' + suffix), native.TARGET / sub / ('seed_objects_probe.' + suffix))
    tree = ET.parse(native.TARGET / 'ui.xml')
    ET.SubElement(tree.find('environment'), 'file', name='ui/seed_objects_probe.lua')
    tree.write(native.TARGET / 'ui.xml', encoding='utf-8', xml_declaration=True)
    (run / ('play-' + slot) / 'installed-hashes.json').write_text(json.dumps({
        p.relative_to(native.TARGET).as_posix(): capture.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()}, indent=2))


def peer(run, slot, duration, seed_only=False):
    cfg = json.loads((run / 'play-session.json').read_text())
    session = UniverseSession(cfg['token'], cfg['build'], cfg['bubble'])
    relay = Relay('127.0.0.1', cfg['port'], session)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(('127.0.0.1', cfg['port'])); sock.setblocking(False)
    receiver = Receiver(); sender = None; lease = None; nonce = secrets.token_hex(16)
    history = []; identities = {}; latest = None; seq = revision = 0
    start = time.monotonic(); next_hello = next_send = 0; ready_at = None
    replay = json.loads((run / 'host-objects.json').read_text()) if slot == 'p1' else None
    omitted=[]
    if seed_only:
        if slot!='p1': raise ValueError('Seed-only selection belongs to the synthetic host replay')
        omitted=[int(row.split(',')[0]) for row in replay['rows'] if int(row.split(',')[0])>=250000]
        replay['rows']=[row for row in replay['rows'] if int(row.split(',')[0])<250000]
        replay['identities']={id:value for id,value in replay['identities'].items() if int(id)<250000}
        if not replay['rows']: raise ValueError('No saved-reference ships available for this bounded experiment')
    phase = None; next_baseline = 0
    try:
        while time.monotonic() - start < duration:
            now = time.monotonic(); relay.step(now)
            if now >= next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{native.SECTOR}|{cfg['bubble']}|{slot}|{nonce}".encode())
                next_hello = now + 1
            for _ in range(512):
                try: f = sock.recv(769).decode().split('|')
                except BlockingIOError: break
                if f[0] == 'WELCOME': lease = f[3]
                elif f[0] in ('UBEGIN', 'UOBJECT', 'UCOMMIT') and lease:
                    ack, baseline = receiver.receive(f, now)
                    if ack is not None: sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
                    if baseline:
                        latest = baseline
                        history.append(dict(revision=baseline.revision, rows=list(baseline.rows)))
                        print('Captured native host baseline', baseline.revision, len(baseline.rows), flush=True)
                elif f[0] == 'IDENT' and f[5] == 'N':
                    identities[f[6]] = dict(code=f[7], name=f[8], known=f[9], contact=f[10])
                elif f[0] == 'UACK' and sender:
                    sender.acknowledge(f[3], int(f[4]), int(f[5]))
                elif f[0] == 'UINTEREST' and ready_at is None:
                    ready_at = now
            if lease and now >= next_send:
                seq += 1; next_send = now + .05
                sock.send(f'IDLE|1|{lease}|{seq}'.encode())
                sock.send(f'CREW|1|{lease}|{seq}|0|0'.encode())
                if slot == 'p2':
                    sock.send(f'UINTEREST|1|{lease}|{seq}|{native.SECTOR}'.encode())
                    if latest and all(row.split(',')[0] in identities for row in latest.rows if ',ship,' in row):
                        ready_at = ready_at or now
                        if now - ready_at > 8 and len(history) >= 3: break
                else:
                    world = [100,100,0,100,100,0,100,100,100,100,200,1,1,0,0,0,0]
                    sock.send(('WORLD|1|' + lease + '|' + str(seq) + '|' + '|'.join(map(str,world))).encode())
                    if ready_at:
                        elapsed = now - ready_at
                        desired = 'initial' if elapsed < 35 else 'absent' if elapsed < 43 else 'return'
                        if (not sender or sender.done) and now >= next_baseline:
                            revision += 1; next_baseline = now + 2
                            rows = [] if desired == 'absent' else replay['rows']
                            sender = Sender(Baseline.create(lease, revision, native.SECTOR, rows))
                            if desired != phase:
                                phase = desired; history.append(dict(phase=phase, at=now-start, revision=revision))
                                print('Native guest phase', phase, flush=True)
                        if desired != 'absent':
                            for id, row in replay['identities'].items():
                                sock.send(f"IDENT|1|{lease}|{lease}|{seq}|N|{id}|{row['code']}|{row['name']}|{row['known']}|{row['contact']}".encode())
                        if elapsed > 60: break
            if sender and lease:
                for packet in sender.packets(lease, now): sock.send(packet)
            time.sleep(.005)
    finally:
        sock.close(); relay.close()
        role = 'host' if slot == 'p2' else 'guest'
        result = dict(role=role, synthetic_peer=True, history=history, elapsed=time.monotonic()-start,
                      seed_only=seed_only, omitted_host_created_ids=omitted)
        if slot == 'p2' and latest:
            rows = [row for row in latest.rows if ',ship,' in row]
            result.update(rows=rows, identities={row.split(',')[0]:identities[row.split(',')[0]]
                          for row in rows if row.split(',')[0] in identities})
        (run / (role + '-objects.json')).write_text(json.dumps(result, indent=2))
        print('Recorded', role, 'objects evidence', flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('install', 'peer'))
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--slot', choices=('p1', 'p2'), required=True)
    p.add_argument('--duration', type=int, default=300)
    p.add_argument('--seed-only',action='store_true',help='Explicitly test original saved ships; retain omitted new-host IDs in the report')
    p.add_argument('--trace-docked',action='store_true',help='Dev-only trace of original docked/carrier pose writes')
    p.add_argument('--skip-docked',action='store_true',help='Dev-only controlled comparison omitting local docked-child pose writes; requires --trace-docked')
    a = p.parse_args()
    if a.action == 'install': install(a.run, a.slot,a.trace_docked,a.skip_docked)
    else: peer(a.run, a.slot, a.duration,a.seed_only)
