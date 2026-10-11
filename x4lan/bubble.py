"""Bounded host-authoritative action routing for the experimental test bubble.

The relay never invents cargo or damage. Only p1's X4 may publish world state
and finish commands. Disconnecting the host cancels commands, never retries
an ambiguous transaction in a new world.
"""
from collections import OrderedDict
import hmac
import math
from .host import Session, HEX, sequence

WORLD_FIELDS = 17
VERBS = {'PICKUP','TRADE'}
RESULTS = {'OK','REJECTED','BUSY','CANCELLED'}

class BubbleSession(Session):
    def __init__(self,*args):
        super().__init__(*args)
        self.world=None
        self.world_owner=None
        self.world_revision=-1
        self.commands=OrderedDict()
        self.fire={}
        self.bodies={}
        self.suit_kills={}
        self.walkers={}

    def expire(self,now):
        old_host=self.peers.get('p1')
        super().expire(now)
        if old_host and 'p1' not in self.peers:
            self.world=None;self.world_owner=None;self.world_revision=-1
            for cmd in self.commands.values():
                if cmd['result']=='PENDING':cmd['result']='CANCELLED'
        live={p.lease for p in self.peers.values()}
        retained=live | {p.lease for p,_ in self.resumable.values()}
        self.bodies={lease:v for lease,v in self.bodies.items() if lease in retained}
        self.suit_kills={lease:v for lease,v in self.suit_kills.items() if lease in retained}
        self.fire={lease:v for lease,v in self.fire.items() if lease in live}
        self.walkers={lease:v for lease,v in self.walkers.items() if lease in live}
        for cmd in self.commands.values():
            if cmd['lease'] not in live and cmd['result']=='PENDING':cmd['result']='CANCELLED'

    def _peer(self,lease,address):
        for slot,p in self.peers.items():
            if p.address==address and hmac.compare_digest(p.lease,lease):return slot,p
        return None,None

    def receive(self,raw,address,now):
        self.expire(now)
        if len(raw)>768:return []
        try:f=raw.decode('ascii').split('|')
        except UnicodeError:return []
        if len(f)<2 or f[1]!='1':return []
        try:
            if f[0]=='ACTION' and len(f)==7:
                _,_,token,slot,request,verb,target=f
                if not hmac.compare_digest(token,self.token) or not HEX.fullmatch(request):return []
                if slot not in ('p1','p2') or verb not in VERBS or target!='energycells':return []
                peer=self.peers.get(slot)
                old=self.commands.get(request)
                if old:
                    if (old['slot'],old['verb'],old['ip'])!=(slot,verb,address[0]):return []
                    return [f"RESULT|1|{request}|{old['result']}".encode()]
                if peer is None or peer.address[0]!=address[0]:return [f'RESULT|1|{request}|NO_PLAYER'.encode()]
                if not self.world or 'p1' not in self.peers:return [f'RESULT|1|{request}|NO_WORLD'.encode()]
                if sum(c['result']=='PENDING' for c in self.commands.values())>=8:return [f'RESULT|1|{request}|BUSY'.encode()]
                while len(self.commands)>=512:
                    key,c=next(iter(self.commands.items()))
                    if c['result']=='PENDING':return [f'RESULT|1|{request}|BUSY'.encode()]
                    # Never forget an ID in this host world: reject further work at bound.
                    return [f'RESULT|1|{request}|SESSION_FULL'.encode()]
                self.commands[request]={'slot':slot,'lease':peer.lease,'ip':address[0],'verb':verb,'result':'PENDING','created':now}
                return [f'RESULT|1|{request}|PENDING'.encode()]
            if f[0] not in ('WORLD','FIRE','DONE','BODY','SUITKILL','WALK'):return super().receive(raw,address,now)
            slot,peer=self._peer(f[2],address)
            if peer is None:return []
            if f[0]=='WALK' and len(f)==11:
                seq=sequence(f[3]);context=sequence(f[4]);pose=[float(v) for v in f[5:]]
                if context not in (0,1,2,3,4):return []
                if not all(math.isfinite(v) and abs(v)<=(10000 if i<3 else 360) for i,v in enumerate(pose)):return []
                old=self.walkers.get(peer.lease)
                if old and seq<=old[0]:return []
                self.walkers[peer.lease]=(seq,now,context,*f[5:])
            elif f[0]=='BODY' and len(f)==13:
                seq=sequence(f[3]);mode=f[4];dead=sequence(f[5]);generation=sequence(f[6])
                pose=[float(v) for v in f[7:]]
                if mode not in ('ship','foot','suit') or dead not in (0,1):return []
                if mode=='suit' and generation==0:return []
                if not all(math.isfinite(v) and abs(v)<=(50000 if i<3 else math.tau) for i,v in enumerate(pose)):return []
                old=self.bodies.get(peer.lease)
                if old and (seq<=old[0] or dead<int(old[2]) or generation<int(old[3])):return []
                self.bodies[peer.lease]=(seq,mode,str(dead),str(generation),*f[7:])
            elif f[0]=='SUITKILL' and slot=='p1' and len(f)==4:
                generation=sequence(f[3]);guest=self.peers.get('p2')
                body=self.bodies.get(guest.lease) if guest else None
                if body and body[1]=='suit' and int(body[3])==generation:
                    self.suit_kills[guest.lease]=(peer.lease,generation)
            elif f[0]=='WORLD' and slot=='p1' and len(f)==4+WORLD_FIELDS:
                rev=sequence(f[3])
                values=[float(x) for x in f[4:]]
                if not all(math.isfinite(v) and 0<=v<=1000000 for v in values):return []
                for i in (0,1,3,4,6,7,8,9):
                    if values[i]>100:return []
                for i in (11,12,13,14):
                    if values[i] not in (0,1):return []
                for i in (2,5,10,15,16):
                    if values[i]!=int(values[i]):return []
                if self.world_owner!=peer.lease:
                    self.world_revision=-1;self.world_owner=peer.lease
                if rev<=self.world_revision:return []
                self.world_revision=rev;self.world=tuple(values)
            elif f[0]=='FIRE' and len(f)==4:
                seq=sequence(f[3]);old=self.fire.get(peer.lease,(-1,0))
                if seq>old[0]:self.fire[peer.lease]=(seq,now)
            elif f[0]=='DONE' and slot=='p1' and len(f)==5 and f[4] in RESULTS:
                cmd=self.commands.get(f[3])
                if cmd and cmd['result']=='PENDING':cmd['result']=f[4]
        except (ValueError,OverflowError,IndexError):return []
        return []

    def snapshots(self,now):
        result=super().snapshots(now)
        host=self.peers.get('p1')
        for slot,peer in self.peers.items():
            if self.world and host:
                payload='|'.join(format(v,'.9g') for v in self.world)
                result.append((peer.address,f'WORLD|1|{peer.lease}|{host.lease}|{self.world_revision}|{payload}'.encode()))
            other=self.peers.get('p2' if slot=='p1' else 'p1')
            if other:
                walker=self.walkers.get(other.lease)
                if walker and now-walker[1]<1:
                    payload='|'.join(map(str,(walker[0],*walker[2:])))
                    result.append((peer.address,f'WALK|1|{peer.lease}|{other.lease}|{payload}'.encode()))
                body=self.bodies.get(other.lease)
                if body:
                    payload='|'.join(map(str,body))
                    result.append((peer.address,f'BODY|1|{peer.lease}|{other.lease}|{payload}'.encode()))
                fire=self.fire.get(other.lease)
                if fire and now-fire[1]<.5:
                    result.append((peer.address,f'FIRE|1|{peer.lease}|{other.lease}|{fire[0]}'.encode()))
            kill=self.suit_kills.get(peer.lease)
            body=self.bodies.get(peer.lease)
            if kill and host and kill[0]==host.lease and body and body[1]=='suit' and int(body[3])==kill[1]:
                result.append((peer.address,f'SUITKILL|1|{peer.lease}|{host.lease}|{kill[1]}'.encode()))
        if host:
            # One transaction at a time. Receiver deduplicates before dispatch to MD.
            for request,cmd in self.commands.items():
                if cmd['result']=='PENDING':
                    result.append((host.address,f"COMMAND|1|{host.lease}|{request}|{cmd['slot']}|{cmd['verb']}".encode()))
                    break
        return result
