"""Replay a real host health trace into a native guest, plus a local-drift check.

No game launch; install p2 with dev/health_probe.xml in the disposable profile.
This is receiver evidence, not simultaneous two-native combat acceptance.
"""
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from x4lan.host import Relay
from x4lan.universe import UniverseSession, Baseline, Sender

HERE = ROOT/'.local/native-world-tests'
PROFILE = Path.home()/'.config/EgoSoft/X4/45841'
SECTOR = 'cluster_14_sector001_macro'


def analyze(log, recorded):
    """Require a real nonconstant trace state, not only the synthetic plateau."""
    fields = (0,1,3,4,6,7,8,9)
    trace_states = {tuple(row['values'][i] for i in fields) for row in recorded
                    if any(row['values'][i] < 99 for i in fields)}
    rows = []; malformed = 0
    for m in re.finditer(r'HEALTH_PROBE expected=([0-9.,-]+) actual=([0-9.,-]+)', log):
        expected = list(map(float,m[1].split(','))); actual = list(map(float,m[2].split(',')))
        if len(expected) != 8 or len(actual) != 8:
            malformed += 1; continue
        rows.append(dict(expected=expected,actual=actual,max_error=max(abs(a-b) for a,b in zip(expected,actual))))
    perturb = log.find('HEALTH_PROBE perturb actual=42,5')
    recovered = perturb >= 0 and bool(re.search(r'HEALTH_PROBE expected=65,45,.* actual=65,45[,.]',log[perturb:]))
    matches = sum(tuple(r['expected']) in trace_states for r in rows)
    result = dict(samples=len(rows),malformed_readbacks=malformed,
                  max_error=max((r['max_error'] for r in rows),default=None),
                  recorded_trace_readbacks=matches,replayed_damage=matches>0,
                  local_drift_recovered=recovered,readbacks=rows)
    result['success'] = len(rows)>30 and not malformed and result['max_error']<=1 and matches>0 and recovered
    return result


def run():
    cfg = json.loads((HERE/'native-session.json').read_text())
    trace = HERE/'fire2-native-host-4f1075c-result.json'
    trace_bytes = trace.read_bytes()
    recorded = json.loads(trace_bytes)['history']
    assert recorded and any(r['values'][9] < 100 for r in recorded)
    session = UniverseSession(cfg['token'], cfg['build'], cfg['bubble'])
    relay = Relay('127.0.0.1', cfg['port'], session)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(('127.0.0.1', cfg['port'])); sock.setblocking(False)
    lease = native_at = sender = None
    nonce = secrets.token_hex(16)
    seq = next_send = next_hello = 0
    started = time.monotonic()
    try:
        while time.monotonic()-started < 240:
            now = time.monotonic(); relay.step(now)
            if now >= next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|p1|{nonce}".encode())
                next_hello = now+1
            for _ in range(512):
                try: f = sock.recv(769).decode().split('|')
                except BlockingIOError: break
                if f[0] == 'WELCOME': lease = f[3]
                elif f[0] == 'UACK' and sender: sender.acknowledge(f[3], int(f[4]), int(f[5]))
            if 'p2' in session.peers and native_at is None: native_at = now
            elapsed = now-native_at if native_at else 0
            if lease and now >= next_send:
                next_send = now+.1; seq += 1
                world = [100,100,0,100,100,0,100,100,100,100,300,1,1,0,0,0,0]
                if 5 <= elapsed < 33:
                    samples = [r for r in recorded if r['time'] <= elapsed-5]
                    if samples: world = list(samples[-1]['values'])
                elif 33 <= elapsed < 40:
                    world = [65,45,0,70,50,0,75,55,80,60,300,1,1,0,0,0,0]
                for packet in (f'IDLE|1|{lease}|{seq}', f'CREW|1|{lease}|{seq}|1|1',
                               'WORLD|1|'+lease+'|'+str(seq)+'|'+'|'.join(map(str, world))):
                    sock.send(packet.encode())
                if sender is None: sender = Sender(Baseline.create(lease,1,SECTOR,[]))
            if sender:
                for packet in sender.packets(lease,now): sock.send(packet)
            if native_at and elapsed > 44: break
            time.sleep(.005)
    finally:
        log = (PROFILE/'x4lan-debug.txt').read_text(errors='replace')
        result = dict(source='recorded real-host health trace plus synthetic plateau; native Linux guest',
                      input_trace=trace.name, input_sha256=hashlib.sha256(trace_bytes).hexdigest(),
                      health_fields={'ship1':[1,2],'ship2':[4,5],'target1':[7,8],'target2':[9,10]},
                      **analyze(log,recorded))
        (HERE/'health-replay-native-guest-result.json').write_text(json.dumps(result,indent=2))
        shutil.copy2(PROFILE/'x4lan-debug.txt',HERE/'health-replay-native-guest.log')
        sock.close(); relay.close()
        print(json.dumps({k:v for k,v in result.items() if k!='readbacks'}),flush=True)
    if not result['success']: raise SystemExit(1)


if __name__ == '__main__': run()
