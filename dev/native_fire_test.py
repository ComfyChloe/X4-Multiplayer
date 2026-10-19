"""Measure native host firing from a synthetic pilot; never launch X4 here.

Install the shared-ship host with fire2=true first. Two six-weapon tap counters
and a sustained burst are sent on the NAV2 clock. WORLD records native shots
and damage. Synthetic source counters do not prove real guest trigger capture.
"""
import argparse
import json
import re
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from x4lan.host import Relay
from x4lan.universe import UniverseSession, Receiver

HERE = ROOT / '.local/native-world-tests'
SECTOR = 'cluster_14_sector001_macro'


def run(echo_source=False, dual_trigger=False, unequal=False):
    dual_trigger = dual_trigger or unequal
    echo_source = (echo_source or dual_trigger) and not unequal
    cfg = json.loads((HERE / 'native-session.json').read_text())
    assert cfg.get('fire2') is True
    session = UniverseSession(cfg['token'], cfg['build'], cfg['bubble'])
    relay = Relay('127.0.0.1', cfg['port'], session)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(('127.0.0.1', cfg['port']))
    sock.setblocking(False)
    nonce = secrets.token_hex(16)
    lease = native_at = None
    start = time.monotonic()
    seq = next_hello = next_send = 0
    receiver = Receiver()
    history, evidence, captured = [], [], []
    identities = set()
    previous_shots = (0,0)
    try:
        while time.monotonic() - start < 240:
            now = time.monotonic()
            relay.step(now)
            if now >= next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|p2|{nonce}".encode())
                next_hello = now + 1
            for _ in range(512):
                try:
                    f = sock.recv(769).decode().split('|')
                except BlockingIOError:
                    break
                if f[0] == 'WELCOME':
                    lease = f[3]
                elif f[0] == 'WORLD':
                    if native_at is None:
                        native_at = now
                    values = list(map(float, f[5:]))
                    history.append(dict(time=round(now-native_at, 4), values=values))
                elif f[0] == 'IDENT':
                    identities.add((f[5], f[6]))
                elif f[0] == 'FIRE2' and f[4] == '1' and native_at:
                    captured.append(dict(time=round(now-native_at, 4), primary=int(f[10]), secondary=int(f[11])))
                elif f[0] in ('UBEGIN', 'UOBJECT', 'UCOMMIT') and lease:
                    ack, _ = receiver.receive(f, now)
                    if ack is not None:
                        sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
            if lease and now >= next_send:
                next_send = now + .05
                seq += 1
                elapsed = now-native_at if native_at else 0
                shots = (4 if elapsed >= 5 else 0) + (2 if elapsed >= 5.10 else 0) + (6 if elapsed >= 10 else 0)
                if elapsed >= 14:
                    shots += 6 * min(13, 1 + int((elapsed-14) / .25))
                secondary = 0
                if echo_source:
                    shots = captured[-1]['primary'] if captured else 0
                    secondary = captured[-1]['secondary'] if captured else 0
                if unequal:
                    shots = (3 if elapsed>=5 else 0)+(9 if elapsed>=10 else 0)+(3 if elapsed>=15 else 0)
                    secondary = (9 if elapsed>=5 else 0)+(3 if elapsed>=10 else 0)+(9 if elapsed>=15.05 else 0)
                source = 500 + now-start
                if (shots,secondary) != previous_shots:
                    evidence.append(dict(time=round(elapsed, 4), primary=shots,secondary=secondary))
                    previous_shots = (shots,secondary)
                packets = [f'IDLE|1|{lease}|{seq}', f'CREW|1|{lease}|{seq}|2|2',
                           f'UINTEREST|1|{lease}|{seq}|{SECTOR}',
                           f'NAV2|1|{lease}|2|1|{seq}|{SECTOR}|1000|10000|0|0|0|0|0|{shots+secondary}|{source:.6f}|0|0|0|0|0|0',
                           f'FIRE2|1|{lease}|2|1|{seq}|{source:.6f}|1|1|{shots}|{secondary}']
                for packet in packets:
                    sock.send(packet.encode())
            if native_at and now-native_at > 27:
                break
            time.sleep(.005)
    finally:
        def at(t):
            samples = [row for row in history if row['time'] <= t]
            return samples[-1]['values'][16] if samples else None
        readings = {str(t): at(t) for t in (4, 8, 13, 20, 26)}
        result = dict(source='native local capture echoed to a synthetic peer' if echo_source else 'synthetic FIRE2 pilot, real Linux host projectile simulation',
                      identities=sorted(identities),
                      sent=evidence, native_shot_readings=readings, history=history, native_source_captures=captured,
                      native_source_capture=captured[-1] if captured else None,
                      native_local_shots=history[-1]['values'][15] if history else None)
        result['completed'] = all(v is not None for v in readings.values())
        if result['completed']:
            result['tap1_native_shots'] = readings['8'] - readings['4']
            result['tap2_native_shots'] = readings['13'] - readings['8']
            result['sustained_native_shots'] = readings['20'] - readings['13']
            result['shots_after_stop'] = readings['26'] - readings['20']
        result['cadence_pass'] = result['completed'] and (
            (readings['26'] == result['native_local_shots'] == 6) if echo_source else
            (result['tap1_native_shots'] == 6 and result['tap2_native_shots'] == 6 and result['sustained_native_shots'] == 78 and result['shots_after_stop'] == 0))
        result['source_capture_pass'] = bool(captured and captured[-1]['primary'] == result['native_local_shots'] == 6 and captured[-1]['secondary'] == 0)
        if dual_trigger:
            def capture_at(t):
                before = [r for r in captured if r['time'] <= t]
                return (before[-1]['primary'],before[-1]['secondary']) if before else None
            log = (Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt').read_text(errors='replace')
            remote = re.findall(r'FIRE_PROBE remote primary=(\d+) secondary=(\d+)',log)
            result['remote_trigger_capture'] = tuple(map(int,remote[-1])) if remote else None
            result['source_capture_pass'] = (capture_at(8),capture_at(13),capture_at(20)) == ((3,0),(3,3),(6,6))
            result['cadence_pass'] = (readings['8'],readings['13'],readings['20'],readings['26']) == (3,6,12,12) and result['remote_trigger_capture'] == (6,6)
        if unequal:
            timeline = [dict(time=float(t),primary=int(p),secondary=int(s)) for t,p,s in re.findall(
                r'\[Scripts\]\s+([0-9.]+).*FIRE_PROBE remote primary=(\d+) secondary=(\d+)',log)]
            result['remote_trigger_timeline'] = timeline
            first_secondary_continues = any(r['primary']==3 and 3<r['secondary']<9 for r in timeline)
            second_primary_continues = any(r['secondary']==12 and 6<r['primary']<12 for r in timeline)
            result['independent_exhaustion_pass'] = first_secondary_continues and second_primary_continues
            result['source_capture_pass'] = result['native_local_shots']==0 and captured[-1]['primary']==captured[-1]['secondary']==0
            result['cadence_pass'] = (readings['8'],readings['13'],readings['20'],readings['26']) == (12,24,36,36) and result['remote_trigger_capture'] == (15,21) and result['independent_exhaustion_pass']
        prefix = 'fire2-native-unequal' if unequal else 'fire2-native-dual'  if dual_trigger else 'fire2-native-echo' if echo_source else 'fire2-native-host'
        (HERE / (prefix+'-result.json')).write_text(json.dumps(result, indent=2))
        shutil.copy2(Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt', HERE/(prefix+'.log'))
        sock.close()
        relay.close()
        print(json.dumps({k: v for k, v in result.items() if k not in ('history', 'sent', 'identities', 'native_source_captures', 'remote_trigger_timeline')}), flush=True)
    if not result['cadence_pass'] or not result['source_capture_pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--echo-source',action='store_true')
    parser.add_argument('--dual-trigger',action='store_true')
    parser.add_argument('--unequal',action='store_true',help='Synthetic 3P/9S,9P/3S and staggered3P then9S on identically configured fixture groups.')
    args=parser.parse_args()
    run(args.echo_source,args.dual_trigger,args.unequal)
