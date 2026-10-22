"""Explicit full native HOST adapter probe with a synthetic guest; no package activation."""
from pathlib import Path
import argparse,json,secrets,shutil,socket,sys,time,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dev import native_seed_play_test as play,native_seed_test as seed,native_world_test as native
from x4lan.host import Relay
from x4lan.universe import UniverseSession,Receiver
from x4lan.attachment import decode


def install(run):
    play.install(run,'p1')
    config=native.TARGET/'ui/config.lua'
    config.write_text(config.read_text().replace('X4LAN_CONFIG={',
        'X4LAN_CONFIG={\n seed_binding=true,attachment_sync=true,attachment_execute=true,attachment_world_probe=true,'))
    tree=ET.parse(native.TARGET/'ui.xml');env=tree.find('environment')
    def before(anchor,names):
        index=next(i for i,e in enumerate(env) if e.get('name')=='ui/'+anchor+'.lua')
        for name in names:
            env.insert(index,ET.Element('file',name='ui/'+name+'.lua'));index+=1
    before('seed_game',('seed_docks','seed_dock_import'))
    before('bubble_game',('seed_dock_game',))
    before('universe_game',('attachment_protocol','attachment_controller','attachment_execution','attachment_driver','attachment_world'))
    ET.SubElement(env,'file',name='ui/attachment_world_probe.lua')
    tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
    for suffix,folder in (('lua','ui'),('xml','md')):
        shutil.copy2(ROOT/('dev/attachment_world_probe.'+suffix),native.TARGET/folder/('attachment_world_probe.'+suffix))
    folder=run/'play-p1'
    (folder/'installed-hashes.json').write_text(json.dumps({p.relative_to(native.TARGET).as_posix():seed.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()},indent=2))
    (run/'play-session.json').chmod(0o600)


def peer(run,duration=300):
    cfg=json.loads((run/'play-session.json').read_text())
    session=UniverseSession(cfg['token'],cfg['build'],cfg['bubble'],attachments=True)
    relay=Relay('127.0.0.1',cfg['port'],session)
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('127.0.0.1',cfg['port']));sock.setblocking(False)
    receiver=Receiver();nonce=secrets.token_hex(16);lease=None;sequence=0;history=[];complete=False
    start=time.monotonic();hello=send=scan=0;offset=0;carry='';native_complete=False;baselines=0
    try:
        while time.monotonic()-start<duration:
            now=time.monotonic();relay.step(now)
            if now>=hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{native.SECTOR}|{cfg['bubble']}|p2|{nonce}".encode());hello=now+1
            for _ in range(1024):
                try:packet=sock.recv(769).decode();fields=packet.split('|')
                except BlockingIOError:break
                if fields[0]=='WELCOME':lease=fields[3]
                elif fields[0] in ('UBEGIN','UOBJECT','UCOMMIT') and lease:
                    ack,baseline=receiver.receive(fields,now)
                    if ack is not None:sock.send(f'UACK|1|{lease}|{fields[3]}|{fields[4]}|{ack}'.encode())
                    if baseline:
                        baselines+=1;history.append(dict(event='baseline',at=now-start,epoch=baseline.epoch,
                            revision=baseline.revision,sector=baseline.sector,rows=list(baseline.rows)))
                elif fields[0]=='UATTACH':
                    decode(packet);history.append(dict(event='attachment',at=now-start,packet=packet))
                elif fields[0]=='IDENT' and len(fields)==11 and fields[5]=='N':
                    history.append(dict(event='identity',at=now-start,packet=packet))
            if lease and now>=send:
                sequence+=1;send=now+.05
                for packet in (f'IDLE|1|{lease}|{sequence}',f'CREW|1|{lease}|{sequence}|0|0',
                               f'UINTEREST|1|{lease}|{sequence}|{native.SECTOR}'):
                    sock.send(packet.encode())
            if now>=scan:
                scan=now+.1
                with (native.PROFILE/'x4lan-debug.txt').open(errors='replace') as stream:
                    stream.seek(offset);new=stream.read();offset=stream.tell()
                lines=(carry+new).splitlines(keepends=True);carry=''
                if lines and not lines[-1].endswith('\n'):carry=lines.pop()
                for line in lines:
                    if '[X4LAN]' in line and ('STOPPED' in line or 'ATTACH_FULL failed' in line):
                        raise RuntimeError('Native full adapter stopped')
                    if '[X4LAN] ATTACH_FULL complete ' in line:native_complete=True
            if native_complete and baselines>=3:
                complete=True;break
            time.sleep(.005)
        if not complete:raise RuntimeError('Full native host did not complete')
    finally:
        (run/'peer-history.json').write_text(json.dumps(history,indent=2))
        (run/'peer-stats.json').write_text(json.dumps(dict(complete=complete,native_host=True,native_guest=False,
            received=relay.received_packets,sent=relay.sent_packets,send_errors=relay.send_errors,
            peers={slot:dict(lease=p.lease,address=p.address) for slot,p in session.peers.items()}),indent=2))
        sock.close();relay.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('install','peer'));p.add_argument('--run',type=Path,required=True)
    a=p.parse_args();install(a.run) if a.action=='install' else peer(a.run)
