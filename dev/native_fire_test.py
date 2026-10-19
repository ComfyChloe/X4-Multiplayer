"""Measure native host firing from a synthetic pilot; never launch X4 here.

Install the shared-ship host with fire2=true first. Two six-weapon tap counters
and a sustained burst are sent on the NAV2 clock. WORLD records native shots
and damage. Synthetic source counters do not prove real guest trigger capture.
"""
import json
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


def run():
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
    previous_shots = 0
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
                shots = (6 if elapsed >= 5 else 0) + (6 if elapsed >= 10 else 0)
                if elapsed >= 14:
                    shots += 6 * min(13, 1 + int((elapsed-14) / .25))
                source = 500 + now-start
                if shots != previous_shots:
                    evidence.append(dict(time=round(elapsed, 4), primary=shots))
                    previous_shots = shots
                packets = [f'IDLE|1|{lease}|{seq}', f'CREW|1|{lease}|{seq}|2|2',
                           f'UINTEREST|1|{lease}|{seq}|{SECTOR}',
                           f'NAV2|1|{lease}|2|1|{seq}|{SECTOR}|1000|10000|0|0|0|0|0|{shots}|{source:.6f}|0|0|0|0|0|0',
                           f'FIRE2|1|{lease}|2|1|{seq}|{source:.6f}|1|1|{shots}|0']
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
        result = dict(source='synthetic FIRE2 pilot, real Linux host projectile simulation',
                      sent=evidence, native_shot_readings=readings, history=history,
                      native_source_capture=captured[-1] if captured else None,
                      native_local_shots=history[-1]['values'][15] if history else None)
        result['completed'] = all(v is not None for v in readings.values())
        if result['completed']:
            result['tap1_native_shots'] = readings['8'] - readings['4']
            result['tap2_native_shots'] = readings['13'] - readings['8']
            result['sustained_native_shots'] = readings['20'] - readings['13']
            result['shots_after_stop'] = readings['26'] - readings['20']
        (HERE / 'fire2-native-host-result.json').write_text(json.dumps(result, indent=2))
        shutil.copy2(Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt', HERE/'fire2-native-host.log')
        sock.close()
        relay.close()
        print(json.dumps({k: v for k, v in result.items() if k not in ('history', 'sent')}), flush=True)
    if not result['completed']:
        raise SystemExit(1)


if __name__ == '__main__':
    run()
