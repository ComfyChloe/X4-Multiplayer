"""Native setter-frame diagnostic. Requires an explicitly installed fresh world.

The synthetic peer sends no NAV, so production pose application has no remote
track. The opt-in probe preserves actual translation while controlling rotation.
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from x4lan.host import Relay
from x4lan.universe import Receiver,UniverseSession

HERE=ROOT/'.local/native-world-tests'
SECTOR='cluster_14_sector001_macro'
INPUT=(100,40,-25)
FIELDS=('gameTime','x','y','z','vx','vy','vz','rx','ry','rz','ux','uy','uz','fx','fy','fz',
        'pfx','pfy','pfz','pux','puy','puz','sfx','sfy','sfz','sux','suy','suz')


def vector(row,prefix):
    return [row[prefix+a] for a in 'xyz']


def rotate(row,v):
    return [sum(row[b+a]*v[i] for i,b in enumerate('ruf')) for a in 'xyz']


def analyze(log):
    rows=[];sets={};malformed=0
    for line in log.splitlines():
        if 'VELOCITY_FRAME sample=' in line or 'VELOCITY_FRAME set=' in line:
            f=dict(re.findall(r'(\w+)=([^\s]+)',line))
            try:
                if 'sample' in f:
                    r={k:float(f[k]) for k in FIELDS};r['phase']=int(f['sample'])
                    if not all(math.isfinite(v) for v in r.values()):raise ValueError('nonfinite sample')
                    if r['phase'] not in range(1,7):raise ValueError('unknown phase')
                    rows.append(r)
                else:
                    phase=int(f['set'])
                    if phase not in range(1,8):raise ValueError('unknown setter phase')
                    if phase in sets:raise ValueError('duplicate setter acknowledgement')
                    sets[phase]={k:float(f[k]) for k in ('gameTime','forwardX','forwardY','forwardZ','upX','upY','upZ')}
                    if not all(math.isfinite(v) for v in sets[phase].values()):raise ValueError('nonfinite setter')
            except (KeyError,ValueError):malformed+=1
    phases={}
    for phase in range(1,7):
        samples=[r for r in rows if r['phase']==phase]
        measured=[[(b[k]-a[k])/(b['gameTime']-a['gameTime']) for k in ('x','y','z')]
                  for a,b in zip(samples,samples[1:]) if .05<=b['gameTime']-a['gameTime']<=.5]
        native=[rotate(r,vector(r,'v')) for r in samples]
        control=dict(samples=len(samples),position_intervals=len(measured),valid=False)
        ack=sets.get(phase)
        if len(measured)>=20 and ack:
            displacement=[statistics.median(v[i] for v in measured) for i in range(3)]
            transformed=[statistics.median(v[i] for v in native) for i in range(3)]
            body_prediction=[statistics.median(rotate(r,INPUT)[i] for r in samples) for i in range(3)]
            norms=max(abs(sum(r[b+a]**2 for a in 'xyz')-1) for r in samples for b in 'ruf')
            orthogonal=max(abs(sum(r[a+x]*r[b+x] for x in 'xyz')) for r in samples for a,b in (('r','u'),('r','f'),('u','f')))
            fx,fy,fz=(ack['forward'+a] for a in 'XYZ')
            ux,uy,uz=(ack['up'+a] for a in 'XYZ')
            heading_ok={1:abs(fx)<.01 and abs(fy)<.01 and fz>.99 and uy>.99,
                        2:fx>.99 and abs(fy)<.01 and abs(fz)<.01,
                        3:fz<-.99 and abs(fx)<.01 and abs(fy)<.01,
                        4:abs(fx)<.01 and abs(fy)>.6 and fz>.6,
                        5:abs(ux)>.6 and uy>.6 and fz>.99,
                        6:abs(fx)>.2 and abs(fy)>.1 and abs(ux)>.05}[phase]
            setter_precedes_samples=ack['gameTime']<samples[0]['gameTime']
            settled=all(1.8<=r['gameTime']-ack['gameTime']<=7.7 for r in samples)
            ordered=all(b['gameTime']>a['gameTime'] for a,b in zip(samples,samples[1:]))
            basis_drift=max(abs(r[p+a.lower()]-ack[name+a]) for r in samples
                            for p,name in (('f','forward'),('u','up')) for a in 'XYZ')
            control.update(position_velocity=displacement,native_velocity_in_sector=transformed,
                component_velocity=[statistics.median(r['v'+a] for r in samples) for a in 'xyz'],
                sector_input_error=max(abs(displacement[i]-INPUT[i]) for i in range(3)),
                body_input_error=max(abs(displacement[i]-body_prediction[i]) for i in range(3)),
                native_vs_position_error=max(abs(displacement[i]-transformed[i]) for i in range(3)),
                heading_verified=heading_ok,basis_norm_error=norms,basis_dot_error=orthogonal,
                basis_drift=basis_drift,settled_samples=settled,ordered_samples=ordered,
                parent_basis={k:samples[0][k] for k in FIELDS if k.startswith(('pf','pu','sf','su'))})
            control['valid']=(heading_ok and setter_precedes_samples and settled and ordered
                and basis_drift<.005 and norms<.01 and orthogonal<.01 and control['native_vs_position_error']<.8)
        phases[str(phase)]=control
    errors=[line for line in log.splitlines() if 'velocityframe' in line.lower().replace('_','') and
            any(word in line.lower() for word in ('error in','error:','failed','not found'))]
    sets_ordered=all(sets[b]['gameTime']>sets[a]['gameTime'] for a,b in zip(range(1,7),range(2,8))) if len(sets)==7 else False
    complete=(len(sets)==7 and 7 in sets and sets_ordered and 'VELOCITY_FRAME complete=1' in log
              and all(p['valid'] for p in phases.values()) and malformed==0 and not errors)
    sector=complete and all(p['sector_input_error']<.8 for p in phases.values())
    body=complete and all(p['body_input_error']<.8 for p in phases.values())
    return dict(phases=phases,set_readbacks=sets,measurement_complete=complete,
                input_matches_sector_vector=sector,input_matches_component_vector=body,
                coordinate_limit='Parent and sector/cluster bases are reported; coincident axes do not distinguish those reference frames.',
                malformed_readbacks=malformed,errors=errors,two_native_acceptance=False)


def run():
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    cfg=json.loads((HERE/'native-session.json').read_text())
    relay=Relay('127.0.0.1',cfg['port'],UniverseSession(cfg['token'],cfg['build'],cfg['bubble']))
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('127.0.0.1',cfg['port']));sock.setblocking(False)
    receiver=Receiver();nonce=secrets.token_hex(16)
    start=time.monotonic();lease=done_at=None;seq=next_hello=next_send=next_log=0
    log_path=Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt'
    try:
        while time.monotonic()-start<240:
            now=time.monotonic();relay.step(now)
            if now>=next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|p2|{nonce}".encode());next_hello=now+1
            for _ in range(512):
                try:f=sock.recv(769).decode().split('|')
                except BlockingIOError:break
                if f[0]=='WELCOME':lease=f[3]
                elif f[0] in ('UBEGIN','UOBJECT','UCOMMIT') and lease:
                    ack,_=receiver.receive(f,now)
                    if ack is not None:sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
            if lease and now>=next_send:
                seq+=1;next_send=now+.05
                for p in (f'IDLE|1|{lease}|{seq}',f'CREW|1|{lease}|{seq}|2|2',f'UINTEREST|1|{lease}|{seq}|{SECTOR}'):
                    sock.send(p.encode())
            if now>=next_log and log_path.exists():
                next_log=now+.5
                if 'VELOCITY_FRAME complete=1' in log_path.read_text(errors='replace'):done_at=done_at or now
            if done_at and now-done_at>1:break
            time.sleep(.005)
    finally:
        sock.close();relay.close()
        log=log_path.read_text(errors='replace') if log_path.exists() else ''
        result=analyze(log);result['source_commit']=commit
        result['source_unchanged']=commit==subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        result['measurement_complete'] &= result['source_unchanged']
        if log_path.exists():shutil.copy2(log_path,HERE/'velocity-frame-native.log')
        (HERE/'velocity-frame-native-result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k!='set_readbacks'},indent=2),flush=True)
    if not result['measurement_complete']:raise SystemExit(1)


if __name__=='__main__':run()
