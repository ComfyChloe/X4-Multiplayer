"""Measure projectile trajectories from a synthetic pilot in a real Linux host.

Requires an explicitly installed/started disposable host with projectile_probe
enabled. Does not launch the game, change the installed mod, or claim native
player hit/aim acceptance. All native evidence is retained under .local/.
"""
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from x4lan.host import Relay
from x4lan.universe import Receiver, UniverseSession

HERE = ROOT / '.local/native-world-tests'
SECTOR = 'cluster_14_sector001_macro'


def analyze(log):
    rows = []; malformed = 0
    orientation = re.search(r'PROJECTILE_PROBE orientation_control=(all|native_only)',log)
    ship_rows = []; readbacks = {}
    for line in log.splitlines():
        if 'PROJECTILE_PROBE ship phase=' in line:
            values = dict(re.findall(r'(\w+)=([^\s]+)', line))
            try:
                ship_rows.append(dict(phase=values['phase'], **{k:float(values[k]) for k in ('gameTime','x','y','z','engineSpeed','yaw','pitch','roll')}))
            except (KeyError,ValueError): malformed += 1
        if 'PROJECTILE_PROBE readback=' in line:
            values = dict(re.findall(r'(\w+)=([^\s]+)', line))
            try:
                serial = int(values['readback'])
                if serial in readbacks: malformed += 1
                readbacks[serial] = {k:float(values[k]) for k in ('shipVx','shipVy','shipVz','shipSpeed','bulletVx','bulletVy','bulletVz')}
            except (KeyError,ValueError): malformed += 1
        if 'PROJECTILE_PROBE sample=' not in line:
            continue
        values = dict(re.findall(r'(\w+)=([^\s]+)', line))
        try:
            row = dict(phase=values['phase'], **{
                key: float(values[key]) for key in
                ('sample', 'configuredVx', 'dt', 'dx', 'dy', 'dz')})
            # X4 uses distinct length/time types. Log raw metre/second values;
            # derive speed here rather than relying on unsupported MD m/s division.
            for axis in ('x','y','z'):
                row['v'+axis] = row['d'+axis]/row['dt'] if row['dt']>0 else float('nan')
            rows.append(row)
        except (KeyError, ValueError):
            malformed += 1
    phases = {}
    for phase, native in (('stationary', 0), ('rendered', 0), ('physical', 100), ('native', 100)):
        samples = [r for r in rows if r['phase'] == phase]
        phases[phase] = dict(samples=len(samples),
            valid=len(samples) == 6 and all(
                all(math.isfinite(v) for k,v in r.items() if k != 'phase')
                and .18 <= r['dt'] <= .4 and 1000 < r['vz'] < 20000
                and abs(r['vx']) < 1000
                and abs(r['vy']) < 100 and r['configuredVx'] == native
                for r in samples))
        if samples:
            phases[phase]['median_velocity'] = [statistics.median(r[k] for r in samples) for k in ('vx','vy','vz')]
    errors = [line for line in log.splitlines() if
              ('projectileprobe' in line.lower().replace('_','') and any(v in line.lower() for v in ('error:', 'error in', 'failed', 'not found')))
              or 'PROJECTILE_PROBE' in line and 'unknown' in line]
    result = dict(phases=phases, samples=rows, errors=errors, malformed_readbacks=malformed,
                  orientation_control=orientation.group(1) if orientation else 'unrecorded',
                  projectile_samples_complete=all(p['valid'] for p in phases.values()) and not errors and malformed == 0
                  and len({r['sample'] for r in rows}) == len(rows),
                  two_native_acceptance=False, damage_or_aim_acceptance=False)
    controls = {}
    for phase in phases:
        points = [r for r in ship_rows if r['phase']==phase]
        velocities = [[(b[k]-a[k])/(b['gameTime']-a['gameTime']) for k in ('x','y','z')]
                      for a,b in zip(points,points[1:]) if .05 <= b['gameTime']-a['gameTime'] <= .5]
        native = [readbacks.get(int(r['sample'])) if math.isfinite(r['sample']) else None
                  for r in rows if r['phase']==phase]
        complete = len(velocities)>=10 and len(native)==6 and all(
            r and all(math.isfinite(v) for v in r.values()) for r in native)
        control = dict(position_intervals=len(velocities), native_readbacks=sum(r is not None for r in native), valid=False)
        if complete:
            v = [statistics.median(r[i] for r in velocities) for i in range(3)]
            control['position_derived_sector_velocity'] = v
            control['native_component_velocity'] = [statistics.median(r[k] for r in native) for k in ('shipVx','shipVy','shipVz')]
            control['native_speed'] = statistics.median(r['shipSpeed'] for r in native)
            expected = 0 if phase=='stationary' else 100
            control['max_orientation_error'] = max(abs(r[k]) for r in points for k in ('yaw','pitch','roll'))
            control['valid'] = (abs(v[0]-expected)<8 and abs(v[1])<2 and abs(v[2])<2
                                and control['max_orientation_error']<.01)
            # A pose-driven ship may itself acquire native velocity; measure it
            # instead of baking the hypothesis that its physical velocity is zero
            # into acceptance. The other controls require their intended state.
            if phase!='rendered':
                nv=control['native_component_velocity']
                control['valid'] &= abs(nv[0]-expected)<2 and abs(nv[1])<2 and abs(nv[2])<2
        controls[phase]=control
    result['controls']=controls
    result['native_readbacks']=readbacks
    result['controls_complete']=all(v['valid'] for v in controls.values())
    result['measurement_complete']=result['projectile_samples_complete'] and result['controls_complete']
    if result['measurement_complete']:
        stationary, rendered, physical = (phases[p]['median_velocity'][0] for p in ('stationary','rendered','physical'))
        result['rendered_lateral_delta'] = rendered-stationary
        result['physical_lateral_delta'] = physical-stationary
        result['native_lateral_delta'] = phases['native']['median_velocity'][0]-stationary
    return result


def run():
    cfg = json.loads((HERE / 'native-session.json').read_text())
    assert cfg.get('fire2') is True
    relay = Relay('127.0.0.1', cfg['port'], UniverseSession(cfg['token'], cfg['build'], cfg['bubble']))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(('127.0.0.1', cfg['port'])); sock.setblocking(False)
    nonce = secrets.token_hex(16)
    receiver = Receiver()
    start = time.monotonic(); native_at = lease = None
    seq = next_send = next_hello = 0
    history = []
    try:
        while time.monotonic()-start < 240:
            now = time.monotonic(); relay.step(now)
            if now >= next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|p2|{nonce}".encode())
                next_hello = now+1
            for _ in range(512):
                try: f = sock.recv(769).decode().split('|')
                except BlockingIOError: break
                if f[0] == 'WELCOME': lease = f[3]
                elif f[0] == 'WORLD':
                    if native_at is None: native_at = now
                    history.append(dict(time=now-native_at, values=list(map(float,f[5:]))))
                elif f[0] in ('UBEGIN','UOBJECT','UCOMMIT') and lease:
                    ack, _ = receiver.receive(f, now)
                    if ack is not None: sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
            if lease and now >= next_send:
                seq += 1; next_send = now+.05
                elapsed = now-native_at if native_at else 0
                shots = 6*sum(elapsed >= t for t in (6,18,30,42))
                vx = 100 if 12 <= elapsed < 48 else 0
                x = 1000+100*max(0,min(36,elapsed-12))
                source = 500+elapsed
                for packet in (
                    f'IDLE|1|{lease}|{seq}', f'CREW|1|{lease}|{seq}|2|2',
                    f'UINTEREST|1|{lease}|{seq}|{SECTOR}',
                    f'NAV2|1|{lease}|2|1|{seq}|{SECTOR}|{x:.6f}|10000|-2000|0|0|0|0|{shots}|{source:.6f}|{vx}|0|0|0|0|0',
                    f'FIRE2|1|{lease}|2|1|{seq}|{source:.6f}|1|1|{shots}|0'):
                    sock.send(packet.encode())
            if native_at and now-native_at > 51: break
            time.sleep(.005)
    finally:
        sock.close(); relay.close()
        log_path = Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt'
        log = log_path.read_text(errors='replace') if log_path.exists() else ''
        result = analyze(log)
        result.update(source='synthetic NAV2/FIRE2 pilot; real native host projectiles', history=history,
                      source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                      production_tree=subprocess.check_output(['git','rev-parse','HEAD:extension/x4_lan_lab'],cwd=ROOT,text=True).strip())
        result['native_remote_shots'] = history[-1]['values'][16] if history else None
        result['measurement_complete'] &= result['native_remote_shots'] == 24
        if log_path.exists(): shutil.copy2(log_path, HERE/'projectile-native-host.log')
        (HERE/'projectile-native-host-result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k not in ('history','samples','native_readbacks')},indent=2),flush=True)
    if not result['measurement_complete']: raise SystemExit(1)


if __name__ == '__main__':
    run()
