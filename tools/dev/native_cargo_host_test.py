"""Synthetic pilot asks real host X4 to drop and collect cargo. Disposable 45841 only."""
import json
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tools")]
from x4lan.universe import UniverseSession, Receiver
from x4lan.host import Relay
HERE=ROOT/'.local/native-world-tests';SECTOR='cluster_14_sector001_macro'

def run():
 cfg=json.loads((HERE/'native-session.json').read_text());s=UniverseSession(cfg['token'],cfg['build'],cfg['bubble'])
 relay=Relay('127.0.0.1',cfg['port'],s)
 sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('127.0.0.1',cfg['port']));sock.setblocking(False)
 nonce=secrets.token_hex(16);lease=None;start=time.monotonic();next_send=next_hello=0;seq=0
 receiver=Receiver();baseline=None;inventory=None;pending=None;completed=[];next_request=0;done_at=None
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
     ack,b=receiver.receive(f,now)
     if ack is not None:sock.send(f'UACK|1|{lease}|{f[3]}|{f[4]}|{ack}'.encode())
     if b:baseline=b
    elif f[0]=='CINV' and f[5]=='energycells':inventory=int(f[7])
    elif f[0]=='CARGOREPLY' and pending and f[3]==pending['id'] and f[4]!='PENDING':
     assert f[4]=='OK',f
     completed.append(dict(verb=pending['verb'],id=pending['id'],fence=int(f[5])))
     print('Real host completed',completed[-1],flush=True);pending=None
     if len(completed)==2:done_at=now
   if lease and now>=next_send:
    next_send=now+.05;seq+=1
    sock.send(f'IDLE|1|{lease}|{seq}'.encode());sock.send(f'CREW|1|{lease}|{seq}|2|2'.encode())
    sock.send(f'UINTEREST|1|{lease}|{seq}|{SECTOR}'.encode())
   if baseline and lease and inventory is not None and not pending and not done_at:
    verb=None;obj=0
    if not completed and inventory==20:verb='DROP'
    elif len(completed)==1 and baseline.revision>=completed[0]['fence'] and inventory==0:
     drops=[r.split(',') for r in baseline.rows if ',drop,' in r]
     if len(drops)==1:verb='PICKUP';obj=int(drops[0][0])
    if verb:
     rid=secrets.token_hex(16);pending=dict(id=rid,verb=verb)
     pending['packet']=f'CARGOREQ|1|{lease}|{rid}|{baseline.epoch}|2|1|{verb}|{obj}|energycells|20'.encode()
   if pending and now>=next_request:
    next_request=now+.25
    sock.send(pending['packet']);sock.send(pending['packet'])  # Intentional duplicate datagram.
   if done_at and now-done_at>8:break
   time.sleep(.005)
 finally:
  result=dict(completed=completed,inventory=inventory,identities_received=len(s.identities.rows),baseline_revision=baseline.revision if baseline else None,
              drops=[r for r in baseline.rows if ',drop,' in r] if baseline else None)
  native_log=(Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt').read_text()
  result['md_errors']='Error in MD cue md.X4LAN_Cargo' in native_log
  result['success']=len(completed)==2 and inventory==20 and result['drops']==[] and not result['md_errors']
  (HERE/'cargo-authority-native-host-result.json').write_text(json.dumps(result,indent=2))
  shutil.copy2(Path.home()/'.config/EgoSoft/X4/45841/x4lan-debug.txt',HERE/'cargo-authority-native-host.log')
  sock.close();relay.close();print('Real host cargo authority:',result['success'],flush=True)
 if not result['success']:raise SystemExit(1)
if __name__=='__main__':run()
