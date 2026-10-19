"""Synthetic host for the isolated native guest's ordinary drop/magnet round trip.

Run only after native_world_test.py install --slot p2. It does not launch X4.
The simulated host is a protocol test double, not evidence of native host physics.
"""
import json
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from x4lan.universe import UniverseSession, Baseline, Sender
from x4lan.host import Relay

HERE=ROOT/'.local/native-world-tests'
PROFILE=Path.home()/'.config/EgoSoft/X4/45841'
SECTOR='cluster_14_sector001_macro'

def run():
 cfg=json.loads((HERE/'native-session.json').read_text())
 session=UniverseSession(cfg['token'],cfg['build'],cfg['bubble'])
 relay=Relay('127.0.0.1',cfg['port'],session)
 sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('127.0.0.1',cfg['port']));sock.setblocking(False)
 lease=None;nonce=secrets.token_hex(16);start=time.monotonic();next_send=next_hello=0
 seq=0;revision=1;published=0;sender=None;cargo=20;drop=False;executed={};completed=None
 try:
  while time.monotonic()-start<240:
   now=time.monotonic();relay.step(now)
   if now>=next_hello:
    sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{SECTOR}|{cfg['bubble']}|p1|{nonce}".encode());next_hello=now+1
   for _ in range(256):
    try: f=sock.recv(769).decode().split('|')
    except BlockingIOError: break
    if f[0]=='WELCOME': lease=f[3]
    elif f[0]=='UACK' and sender: sender.acknowledge(f[3],int(f[4]),int(f[5]))
    elif f[0]=='CARGOEXEC':
     request,verb=f[3],f[6]
     if request not in executed:
      assert f[4]=='2' and f[8]=='energycells' and f[9]=='20',f
      if verb=='DROP': assert cargo==20 and not drop;cargo=0;drop=True
      else: assert verb=='PICKUP' and f[7]=='1' and drop and cargo==0;cargo=20;drop=False
      revision+=1;executed[request]={'verb':verb,'revision':revision,'attempts':0}
      print('Synthetic host executed',verb,'inventory',cargo,'revision',revision,flush=True)
     item=executed[request];item['attempts']+=1
     # Lose the first completion deliberately: a retry must not mutate twice.
     if item['attempts']>1:
      sock.send(f"CARGODONE|1|{lease}|{request}|OK|{item['revision']}".encode())
      if verb=='PICKUP' and completed is None: completed=now
   if lease and now>=next_send:
    next_send=now+.1;seq+=1
    sock.send(f'IDLE|1|{lease}|{seq}'.encode());sock.send(f'CREW|1|{lease}|{seq}|1|1'.encode())
    world=[100,100,0,100,100,cargo,100,100,100,100,200,0,0,0,0,0,0]
    sock.send(('WORLD|1|'+lease+'|'+str(seq)+'|'+'|'.join(map(str,world))).encode())
    sock.send(f'CINV|1|{lease}|{lease}|{seq}|energycells|0|{cargo}'.encode())
    if published!=revision and (not sender or sender.done):
     rows=['1,drop,sm_gen_pickup_container_01_macro,ownerless,1000,10000,180,0,0,0,energycells,20'] if drop else []
     sender=Sender(Baseline.create(lease,revision,SECTOR,rows));published=revision
   if sender and lease:
    for packet in sender.packets(lease,now): sock.send(packet)
   if completed and now-completed>12: break
   time.sleep(.005)
 finally:
  result=dict(duration=time.monotonic()-start,executed=executed,cargo=cargo,drop=drop,guest_committed=session.delivered,
              success=len(executed)==2 and cargo==20 and not drop and session.delivered==3)
  (HERE/'cargo-native-guest-result.json').write_text(json.dumps(result,indent=2))
  shutil.copy2(PROFILE/'x4lan-debug.txt',HERE/'cargo-native-guest.log')
  sock.close();relay.close();print('Guest cargo round trip:',result['success'],flush=True)
 if not result['success']: raise SystemExit(1)

if __name__=='__main__':run()
