"""Native host capacity/partial collection, with synthetic authenticated pilot.

Install --slot p1 --shared-ship and opt into cargo_capacity_probe.xml/.lua first.
This script never launches X4 and is not an automated unit test.
"""
import json
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools")]
from x4lan.universe import UniverseSession, Receiver
from x4lan.host import Relay

HERE = ROOT / '.local/native-world-tests'
SECTOR = 'cluster_14_sector001_macro'


def run():
    cfg = json.loads((HERE / 'native-session.json').read_text())
    session = UniverseSession(cfg['token'], cfg['build'], cfg['bubble'])
    relay = Relay('127.0.0.1', cfg['port'], session)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(('127.0.0.1', cfg['port']))
    sock.setblocking(False)
    nonce = secrets.token_hex(16)
    lease = baseline = inventory = pending = done_at = None
    start = time.monotonic()
    next_send = next_hello = next_request = seq = 0
    receiver = Receiver()
    completed = []
    # Before each request, wait for this actual host inventory and drop quantity.
    stages = [(1090, 20, 20, 'CAPACITY'), (1090, 20, 10, 'OK'),
              (1100, 10, 1, 'CAPACITY'), (1090, 10, 10, 'OK')]
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
                elif f[0] in ('UBEGIN', 'UOBJECT', 'UCOMMIT') and lease:
                    ack, result = receiver.receive(f, now)
                    if ack is not None:
                        sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
                    if result:
                        baseline = result
                elif f[0] == 'CINV' and f[5] == 'energycells':
                    inventory = int(f[7])
                elif f[0] == 'CARGOREPLY' and pending and f[3] == pending['id'] and f[4] != 'PENDING':
                    assert f[4] == stages[len(completed)][3], f
                    completed.append(dict(amount=pending['amount'], status=f[4], fence=int(f[5])))
                    print('Capacity stage', len(completed), completed[-1], flush=True)
                    pending = None
                    if len(completed) == len(stages):
                        done_at = now
            if lease and now >= next_send:
                next_send = now + .05
                seq += 1
                for packet in (f'IDLE|1|{lease}|{seq}', f'CREW|1|{lease}|{seq}|2|2',
                               f'UINTEREST|1|{lease}|{seq}|{SECTOR}'):
                    sock.send(packet.encode())
            if baseline and lease and not pending and not done_at:
                count, quantity, amount, _ = stages[len(completed)]
                drops = [r.split(',') for r in baseline.rows if ',drop,' in r]
                fence = completed[-1]['fence'] if completed else 0
                if inventory == count and baseline.revision >= fence and len(drops) == 1 and int(drops[0][-1]) == quantity:
                    request = secrets.token_hex(16)
                    pending = dict(id=request, amount=amount, packet=(
                        f'CARGOREQ|1|{lease}|{request}|{baseline.epoch}|2|1|PICKUP|{drops[0][0]}|energycells|{amount}').encode())
            if pending and now >= next_request:
                next_request = now + .25
                sock.send(pending['packet'])
                sock.send(pending['packet'])
            if done_at and now - done_at > 8:
                break
            time.sleep(.005)
    finally:
        result = dict(completed=completed, inventory=inventory,
                      drops=[r for r in baseline.rows if ',drop,' in r] if baseline else None)
        log = Path.home() / '.config/EgoSoft/X4/45841/x4lan-debug.txt'
        result['md_errors'] = 'Error in MD cue md.X4LAN_Cargo' in log.read_text()
        result['success'] = len(completed) == 4 and inventory == 1100 and result['drops'] == [] and not result['md_errors']
        (HERE / 'cargo-capacity-native-result.json').write_text(json.dumps(result, indent=2))
        shutil.copy2(log, HERE / 'cargo-capacity-native.log')
        sock.close()
        relay.close()
        print('Native host capacity:', result['success'], flush=True)
    if not result['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    run()
