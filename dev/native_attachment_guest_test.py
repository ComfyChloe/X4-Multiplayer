"""Full native guest replay of a qualified native host capture; not a joint run."""
from pathlib import Path
import argparse,json,secrets,shutil,socket,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dev import native_seed_test as seed,native_world_test as native
from dev.native_attachment_world_test import install_role
from dev.validate_attachment_world import validate as host_validate
from x4lan.attachment import Attachment,decode,encode,MAX_SHIP
from x4lan.host import Relay
from x4lan.universe import UniverseSession,Baseline,Sender


def host_replay(host_run):
    """Derive the complete saved subset from actual received native host records."""
    inventory=json.loads((host_run/'evidence-hashes.json').read_text())
    for name in ('load.log','peer-history.json','peer-stats.json','state.json','source-capture.json','full-host-validation.json'):
        if seed.digest(host_run/name)!=inventory.get(name):raise ValueError('Qualified host evidence changed: '+name)
    state=json.loads((host_run/'state.json').read_text());source=json.loads((host_run/'source-capture.json').read_text())
    history=json.loads((host_run/'peer-history.json').read_text());stats=json.loads((host_run/'peer-stats.json').read_text())
    result=host_validate((host_run/'load.log').read_text(errors='replace'),state['seed_id'],
                         source['capture_validation']['count'],history,stats)
    if result!=json.loads((host_run/'full-host-validation.json').read_text()):raise ValueError('Host gate result changed')
    baseline=max((e for e in history if e['event']=='baseline'),key=lambda e:e['revision'])
    rows=[];omitted=[]
    for wire in baseline['rows']:
        fields=wire.split(',')
        (rows if fields[1]=='ship' and int(fields[0])<=MAX_SHIP else omitted).append(wire)
    wanted={int(row.split(',')[0]) for row in rows};attachments={};identities={}
    for event in history:
        if event['event']=='attachment':
            row=decode(event['packet'])
            if row.revision==baseline['revision'] and row.ship in wanted:
                attachments[row.ship]=dict(state=row.state,dock=row.dock,source_sequence=row.sequence)
        elif event['event']=='identity':
            f=event['packet'].split('|');ship=int(f[6])
            if ship in wanted:identities[ship]=dict(code=f[7],name=f[8],known=f[9],contact=f[10])
    if not wanted or set(attachments)!=wanted or set(identities)!=wanted:
        raise ValueError('Qualified host replay subset is incomplete')
    return dict(seed_id=state['seed_id'],host_source=str(host_run.resolve()),host_source_hashes={k:inventory[k] for k in
        ('load.log','peer-history.json','peer-stats.json','state.json','source-capture.json','full-host-validation.json')},
        source_epoch=baseline['epoch'],source_revision=baseline['revision'],sector=baseline['sector'],
        rows=rows,omitted_rows=omitted,attachments=attachments,identities=identities,
        native_host_capture=True,live_native_host=False,geometry_evidence=True)


def install(run,host_run,hold_free=False):
    replay=host_replay(host_run)
    if json.loads((run/'state.json').read_text())['seed_id']!=replay['seed_id']:
        raise ValueError('Guest seed differs from qualified host')
    install_role(run,'p2','attachment_guest_probe')
    replay['diagnostic_native_hold']=bool(hold_free)
    (run/'host-replay.json').write_text(json.dumps(replay,indent=2))
    entries=[]
    for wire in replay['rows']:
        fields=wire.split(',');ship=int(fields[0]);identity=replay['identities'][ship];attachment=replay['attachments'][ship]
        entries.append(f' [{ship}]={{macro={json.dumps(fields[2])},owner={json.dumps(fields[3])},code={json.dumps(identity["code"])},'
                       f'state={json.dumps(attachment["state"])},dock={attachment["dock"]}}},')
    (native.TARGET/'ui/attachment_replay_data.lua').write_text('X4LAN_NATIVE_REPLAY={\n'+ '\n'.join(entries)+'\n}\n')
    import xml.etree.ElementTree as ET
    tree=ET.parse(native.TARGET/'ui.xml');env=tree.find('environment')
    index=next(i for i,e in enumerate(env) if e.get('name')=='ui/attachment_guest_probe.lua')
    env.insert(index,ET.Element('file',name='ui/attachment_replay_data.lua'))
    if hold_free:
        cfg=native.TARGET/'ui/config.lua';cfg.write_text(cfg.read_text().replace('X4LAN_CONFIG={','X4LAN_CONFIG={\n replica_hold_probe=true,'))
        for suffix,folder in (('lua','ui'),('xml','md')):
            shutil.copy2(ROOT/('dev/replica_hold_probe.'+suffix),native.TARGET/folder/('replica_hold_probe.'+suffix))
        ET.SubElement(env,'file',name='ui/replica_hold_probe.lua')
    tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
    (run/'play-p2/installed-hashes.json').write_text(json.dumps({p.relative_to(native.TARGET).as_posix():seed.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()},indent=2))


def peer(run,duration=300):
    cfg=json.loads((run/'play-session.json').read_text());replay=json.loads((run/'host-replay.json').read_text())
    session=UniverseSession(cfg['token'],cfg['build'],cfg['bubble'],attachments=True)
    relay=Relay('127.0.0.1',cfg['port'],session)
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('127.0.0.1',cfg['port']));sock.setblocking(False)
    nonce=secrets.token_hex(16);lease=None;sender=None;history=[];seq=revision=attach_sequence=0
    start=time.monotonic();hello=send=ident=scan=next_baseline=0;ready=False;offset=0;carry='';complete=False
    def emit(packet):sock.send(packet.encode())
    try:
        while time.monotonic()-start<duration:
            now=time.monotonic();relay.step(now)
            if now>=hello:
                emit(f"HELLO|1|{cfg['token']}|{cfg['build']}|{replay['sector']}|{cfg['bubble']}|p1|{nonce}");hello=now+1
            for _ in range(1024):
                try:fields=sock.recv(769).decode().split('|')
                except BlockingIOError:break
                if fields[0]=='WELCOME':lease=fields[3]
                elif fields[0]=='UACK' and sender:sender.acknowledge(fields[3],int(fields[4]),int(fields[5]))
                elif fields[0]=='UINTEREST':ready=True
            if lease and now>=send:
                seq+=1;send=now+.1
                for packet in (f'IDLE|1|{lease}|{seq}',f'CREW|1|{lease}|{seq}|0|0',
                    'WORLD|1|'+lease+'|'+str(seq)+'|'+'|'.join(map(str,[100,100,0,100,100,0,100,100,100,100,200,1,1,0,0,0,0]))):emit(packet)
                if ready:
                    if (not sender or sender.done) and now>=next_baseline:
                        revision+=1;next_baseline=now+2
                        sender=Sender(Baseline.create(lease,revision,replay['sector'],replay['rows']))
                        history.append(dict(event='baseline',at=now-start,epoch=lease,revision=revision,
                                            sector=replay['sector'],rows=replay['rows']))
                    if now>=ident:
                        ident=now+.5
                        for ship,row in replay['identities'].items():
                            packet=f"IDENT|1|{lease}|{lease}|{seq}|N|{ship}|{row['code']}|{row['name']}|{row['known']}|{row['contact']}"
                            emit(packet);history.append(dict(event='identity',at=now-start,packet=packet))
                    if sender and sender.done:
                        for ship,row in replay['attachments'].items():
                            attach_sequence+=1
                            packet=encode(Attachment(lease,lease,revision,attach_sequence,int(ship),row['dock'],row['state']))
                            emit(packet);history.append(dict(event='attachment',at=now-start,packet=packet))
            if sender:
                for packet in sender.packets(lease,now):sock.send(packet)
            if now>=scan:
                scan=now+.1
                with (native.PROFILE/'x4lan-debug.txt').open(errors='replace') as stream:
                    stream.seek(offset);new=stream.read();offset=stream.tell()
                lines=(carry+new).splitlines(keepends=True);carry=''
                if lines and not lines[-1].endswith('\n'):carry=lines.pop()
                for line in lines:
                    if '[X4LAN]' in line and ('STOPPED' in line or 'ATTACH_GUEST failed' in line):
                        raise RuntimeError('Native guest adapter stopped')
                    if '[X4LAN] ATTACH_GUEST complete ' in line:complete=True
            if complete:break
            time.sleep(.005)
        if not complete:raise RuntimeError('Full native guest did not complete')
    finally:
        (run/'peer-history.json').write_text(json.dumps(history,indent=2))
        (run/'peer-stats.json').write_text(json.dumps(dict(complete=complete,native_host=False,native_guest=True,
            replayed_native_capture=True,received=relay.received_packets,sent=relay.sent_packets,send_errors=relay.send_errors,
            peers={slot:dict(lease=p.lease,address=p.address) for slot,p in session.peers.items()}),indent=2))
        sock.close();relay.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('install','peer'));p.add_argument('--run',type=Path,required=True)
    p.add_argument('--host-run',type=Path);p.add_argument('--hold-free',action='store_true',help='Explicit dev-only constant-zero velocity comparison, not production guest acceptance');a=p.parse_args()
    if a.action=='install':
        if a.host_run is None:p.error('install requires --host-run')
        install(a.run,a.host_run,a.hold_free)
    else:peer(a.run)
