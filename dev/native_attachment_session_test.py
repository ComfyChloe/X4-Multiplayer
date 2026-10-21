"""Opt-in real UDP/native attachment component experiment, not a full game session."""
from pathlib import Path
import argparse,json,secrets,shutil,socket,sys,time,re,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dev import native_seed_admission_test as admission,native_seed_test as seed,native_world_test as native
from x4lan.host import Relay
from x4lan.universe import UniverseSession,Baseline,Sender


def install(run,mode):
    admission.install(run,mode)
    if mode=='load':
        state=json.loads((run/'state.json').read_text())
        session=dict(token=secrets.token_hex(16),build=state['game_build'],bubble=state['seed_id'],port=45843)
        (run/'session.json').write_text(json.dumps(session,indent=2));(run/'session.json').chmod(0o600)
        config=native.TARGET/'ui/config.lua'
        config.write_text(config.read_text().replace('X4LAN_CONFIG={','X4LAN_CONFIG={\n'+''.join(
            f' {k}={json.dumps(v)},\n' for k,v in dict(attachment_session_probe=True,attachment_execute=True,
                world_mode='replica',host='127.0.0.1',sector=native.SECTOR,**{k:v for k,v in session.items() if k!='bubble'}).items())))
        tree=ET.parse(native.TARGET/'ui.xml');env=tree.find('environment')
        for name in ('transport','client','universe_protocol','attachment_protocol','attachment_controller',
                     'attachment_execution','attachment_driver','attachment_world','attachment_session_probe'):
            ET.SubElement(env,'file',name=f'ui/{name}.lua')
        tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
        shutil.copy2(ROOT/'dev/attachment_session_probe.lua',native.TARGET/'ui/attachment_session_probe.lua')
        shutil.copy2(ROOT/'dev/attachment_session_probe.xml',native.TARGET/'md/attachment_session_probe.xml')
    (run/(mode+'-installed-hashes.json')).write_text(json.dumps({p.relative_to(native.TARGET).as_posix():seed.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()},indent=2))


def peer(run,duration=240):
    cfg=json.loads((run/'session.json').read_text())
    session=UniverseSession(cfg['token'],cfg['build'],cfg['bubble'],attachments=True)
    relay=Relay('127.0.0.1',cfg['port'],session)
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('127.0.0.1',cfg['port']));sock.setblocking(False)
    nonce=secrets.token_hex(16);lease=None;sender=None;selected=None;phase=0;sequence=0;history=[]
    start=time.monotonic();hello=send=scan=0;offset=0;log="";carry="";interest=False
    try:
        while time.monotonic()-start<duration:
            now=time.monotonic();relay.step(now)
            if now>=hello:
                sock.send(f"HELLO|1|{cfg['token']}|{cfg['build']}|{native.SECTOR}|{cfg['bubble']}|p1|{nonce}".encode());hello=now+1
            for _ in range(256):
                try:f=sock.recv(769).decode().split('|')
                except BlockingIOError:break
                if f[0]=='WELCOME':lease=f[3]
                elif f[0]=='UACK' and sender:sender.acknowledge(f[3],int(f[4]),int(f[5]))
                elif f[0]=='UINTEREST':interest=True
            if now>=scan:
                scan=now+.1
                with (native.PROFILE/'x4lan-debug.txt').open(errors='replace') as stream:
                    stream.seek(offset);new=stream.read();offset=stream.tell()
                lines=(carry+new).splitlines(keepends=True);carry=''
                if lines and not lines[-1].endswith('\n'):carry=lines.pop()
                log+=''.join(line for line in lines if 'ATTACH_SESSION ' in line or 'ATTACH_WORLD STOPPED' in line)
                if 'ATTACH_SESSION STOPPED' in log or 'ATTACH_WORLD STOPPED' in log:raise RuntimeError('native component stopped')
                if selected is None:
                    match=re.search(r'ATTACH_SESSION selected ship=(\d+) carrier=(\d+) inside=(\d+) outside=(\d+) code=(\S+) macro=(\S+) owner=(\S+)',log)
                    if match:
                        selected=dict(zip(('ship','carrier','inside','outside','code','macro','owner'),match.groups()))
                        history.append(dict(event='selected',at=now-start,**selected))
                completed=[int(v) for v in re.findall(r'ATTACH_SESSION phase-complete phase=(\d+)',log)]
                if completed:
                    assert completed==list(range(len(completed))),'nonsequential native phase completion'
                    phase=len(completed)
                    if phase==8:
                        history.append(dict(event='complete',at=now-start));break
            if lease and now>=send:
                send=now+.05;sequence+=1
                sock.send(f'IDLE|1|{lease}|{sequence}'.encode())
                if selected and interest:
                    if sender is None:
                        row=f"{selected['ship']},ship,{selected['macro']},{selected['owner']},0,0,0,0,0,0,none,0"
                        baseline=Baseline.create(lease,1,native.SECTOR,[row]);sender=Sender(baseline)
                        history.append(dict(event='baseline',at=now-start,epoch=lease,revision=1,rows=[row]))
                    for packet in sender.packets(lease,now):sock.send(packet)
                    state=('FREE','STORED','FREE','EXTERNAL','STORED','EXTERNAL','FREE','STORED')[phase]
                    dock=0 if state=='FREE' else selected['inside' if state=='STORED' else 'outside']
                    sock.send(f"IDENT|1|{lease}|{lease}|{sequence}|N|{selected['ship']}|{selected['code']}|53617665642073686970|1|1".encode())
                    packet=f"UATTACH|1|{lease}|{lease}|1|{sequence}|{selected['ship']}|{dock}|{state}"
                    sock.send(packet.encode());history.append(dict(event='observation',at=now-start,packet=packet,phase=phase))
            time.sleep(.005)
        if phase!=8:raise RuntimeError('native component did not finish all8 phases')
    finally:
        (run/'peer-history.json').write_text(json.dumps(history,indent=2))
        (run/'peer-stats.json').write_text(json.dumps(dict(received=relay.received_packets,sent=relay.sent_packets,
            send_errors=relay.send_errors,complete=phase==8,native_host=False,
            peers={slot:dict(lease=p.lease,address=p.address) for slot,p in session.peers.items()}),indent=2))
        sock.close();relay.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('capture','load','peer'));p.add_argument('--run',type=Path,required=True)
    a=p.parse_args();peer(a.run) if a.action=='peer' else install(a.run,a.action)
