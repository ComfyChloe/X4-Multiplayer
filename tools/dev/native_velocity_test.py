"""Native cooperative velocity/lifecycle experiment with a synthetic UDP peer."""
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

ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tools")]
from x4lan.host import Relay
from x4lan.universe import Receiver,UniverseSession

HERE=ROOT/'.local/native-world-tests';SECTOR='cluster_14_sector001_macro'
PHASES=('motion','freeze','resume','adapter_pause','watchdog','revoked','old_sequence','wrong_session','wrong_epoch','expired','wrong_parent','takeover')
NUMERIC=('elapsed','time','x','y','z','vx','vy','vz','rx','ry','rz','ux','uy','uz','fx','fy','fz','speed','generation','sequence','deadline','wantX','wantY','wantZ','wantVx','wantVy','wantVz','wantGeneration','wantSequence')
FLAGS=('managed','parentZone','topology','pilot','wantManaged')


def analyze(log):
    rows=[];malformed=0
    for line in log.splitlines():
        if 'NATIVE_VELOCITY sample=' not in line:continue
        f=dict(re.findall(r'(\w+)=([^\s]+)',line))
        try:
            r={k:float(f[k]) for k in NUMERIC};r.update(phase=f['sample'],tag=f['tag'])
            if r['phase'] not in PHASES or not all(math.isfinite(r[k]) for k in NUMERIC):raise ValueError('bad sample')
            for k in FLAGS:
                if f[k] not in ('0','1','true','false'):raise ValueError('bad boolean')
                r[k]=f[k] in ('1','true')
            r['sector_velocity']=[sum(r[b+a]*r['v'+axis] for b,axis in zip('ruf','xyz')) for a in 'xyz']
            r['pose_error']=math.sqrt(sum((r[a]-r['want'+a.upper()])**2 for a in 'xyz'))
            r['velocity_error']=max(abs(r['sector_velocity'][i]-r['wantV'+a]) for i,a in enumerate('xyz'))
            rows.append(r)
        except (KeyError,ValueError):malformed+=1
    phases={}
    for phase in PHASES:
        samples=[r for r in rows if r['phase']==phase and r['tag']=='settled' and 2<=r['elapsed']<=5.5]
        result=dict(samples=len(samples),valid=False)
        if len(samples)>=10:
            result.update(max_native_speed=max(r['speed'] for r in samples),
                max_pose_error=max(r['pose_error'] for r in samples),
                max_velocity_error=max(r['velocity_error'] for r in samples),
                managed_values=sorted(set(r['managed'] for r in samples)),
                free_zone_topology=all(r['parentZone'] and r['topology'] for r in samples))
            ordered=all(b['time']>a['time'] for a,b in zip(samples,samples[1:]))
            valid=ordered and result['free_zone_topology']
            if phase in ('motion','freeze','resume'):
                displacements=[[(b[a]-r[a])/(b['time']-r['time']) for a in 'xyz'] for r,b in zip(samples,samples[1:]) if b['time']>r['time']]
                displacement=[statistics.median(v[i] for v in displacements) for i in range(3)] if displacements else [math.inf]*3
                expected=[statistics.median(r['wantV'+a] for r in samples) for a in 'xyz']
                result['median_displacement_velocity']=displacement
                result['displacement_velocity_error']=max(abs(a-b) for a,b in zip(displacement,expected))
                valid &= result['managed_values']==[True] and result['max_velocity_error']<.5 and result['max_pose_error']<5
                valid &= result['displacement_velocity_error']<2
                valid &= all(not r['pilot'] for r in samples)
                if phase=='freeze':valid &= result['max_native_speed']<.05
                else:valid &= min(r['speed'] for r in samples)>100
            elif phase in ('adapter_pause','watchdog','revoked'):
                valid &= result['managed_values']==[False] and result['max_native_speed']<.05
            elif phase=='takeover':
                valid &= result['managed_values']==[False] and all(r['pilot'] for r in samples) and max(r['speed'] for r in samples)>50
            result['valid']=bool(valid)
        phases[phase]=result
    attacks={}
    for phase,tag in (('revoked','old_generation'),)+tuple((p,p) for p in PHASES[6:11]):
        samples=[r for r in rows if r['phase']==phase and r['tag']==tag]
        expected=phase not in ('revoked','expired','wrong_parent')
        attacks[tag]=dict(samples=len(samples),valid=len(samples)>=10 and all(
            r['speed']<.05 and r['managed']==expected and r['managed']==r['wantManaged']
            and r['generation']==r['wantGeneration'] and r['sequence']==r['wantSequence'] for r in samples))
    window=[r for r in rows if r['phase']=='watchdog' and r['tag']=='watchdog_window']
    first_zero=next((r for r in window if not r['managed'] and r['speed']<.05),None)
    watchdog_valid=bool(first_zero and any(r['managed'] and r['speed']>100 for r in window)
                        and 0<=first_zero['time']-first_zero['deadline']<.12)
    errors=[line for line in log.splitlines() if ('nativevelocity' in line.lower().replace('_','') or 'x4lan_coop.velocity' in line.lower())
            and any(word in line.lower() for word in ('error in','error:','failed','not found'))]
    complete=(all(p['valid'] for p in phases.values()) and all(a['valid'] for a in attacks.values())
              and watchdog_valid and not malformed and not errors and 'NATIVE_VELOCITY complete=1' in log)
    return dict(measurement_complete=bool(complete),phases=phases,negative_commands=attacks,
        watchdog_valid=watchdog_valid,watchdog_release_after_expiry=first_zero['time']-first_zero['deadline'] if first_zero else None,
        malformed_readbacks=malformed,errors=errors,two_native_acceptance=False,
        pause_scope='Adapter pause branch injected while the native world continues, not an engine menu-pause scheduling test.')


def run():
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    cfg=json.loads((HERE/'native-session.json').read_text())
    relay=Relay('127.0.0.1',cfg['port'],UniverseSession(cfg['token'],cfg['build'],cfg['bubble']))
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('127.0.0.1',cfg['port']));sock.setblocking(False)
    receiver=Receiver();nonce=secrets.token_hex(16);lease=done_at=world_start=None
    start=time.monotonic();seq=next_hello=next_send=next_log=0;takeover=False
    log_path=Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt'
    try:
        while time.monotonic()-start<300:
            now=time.monotonic();relay.step(now)
            if now>=next_hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|p2|{nonce}".encode());next_hello=now+1
            for _ in range(512):
                try:f=sock.recv(769).decode().split('|')
                except BlockingIOError:break
                if f[0]=='WELCOME':lease=f[3]
                elif f[0]=='WORLD':world_start=world_start or now
                elif f[0] in ('UBEGIN','UOBJECT','UCOMMIT') and lease:
                    ack,_=receiver.receive(f,now)
                    if ack is not None:sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
            if lease and now>=next_send:
                seq+=1;next_send=now+.05;controlled=0 if takeover else 2
                for p in (f'IDLE|1|{lease}|{seq}',f'CREW|1|{lease}|{seq}|{controlled}|{controlled}',f'UINTEREST|1|{lease}|{seq}|{SECTOR}'):
                    sock.send(p.encode())
                if world_start and not takeover:
                    t=now-world_start;x=1000+100*t;y=10000+40*t;z=-2000-25*t
                    sock.send(f'NAV2|1|{lease}|2|1|{seq}|{SECTOR}|{x:.6f}|{y:.6f}|{z:.6f}|{math.pi/2}|0|0|0|0|{500+t:.6f}|100|40|-25|0|0|0'.encode())
            if now>=next_log and log_path.exists():
                next_log=now+.1;log=log_path.read_text(errors='replace')
                takeover='NATIVE_VELOCITY phase=takeover' in log
                if 'NATIVE_VELOCITY complete=1' in log:done_at=done_at or now
            if done_at and now-done_at>1:break
            time.sleep(.005)
    finally:
        sock.close();relay.close();log=log_path.read_text(errors='replace') if log_path.exists() else ''
        result=analyze(log);result['source_commit']=commit
        result['source_unchanged']=commit==subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        result['measurement_complete'] &= result['source_unchanged']
        if log_path.exists():shutil.copy2(log_path,HERE/'native-velocity.log')
        (HERE/'native-velocity-result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2),flush=True)
    if not result['measurement_complete']:raise SystemExit(1)


if __name__=='__main__':run()
