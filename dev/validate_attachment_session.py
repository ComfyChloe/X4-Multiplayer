"""Audit raw network provenance/timing before the existing native causal gates.

Only fully audited diagnostic records are projected to the original single-pair
execution validator. Raw logs remain untouched. Synthetic-host component evidence
is explicitly distinct from a full native world adapter or two native games.
"""
import re,math
from dataclasses import replace
from x4lan.attachment import decode,encode,HEX
from x4lan.universe import record
from dev.validate_attachment_controller import STATES
from dev.validate_attachment_execution import validate as execution,COMMAND
from dev.validate_seed_admission import validate as admission


def require(condition,why):
    if not condition:raise ValueError(why)


def project(text,seed,history,stats):
    require(stats.get('complete') is True and stats.get('native_host') is False and stats.get('send_errors')==0,'peer run incomplete or failed')
    peers=stats.get('peers',{});host=peers.get('p1',{}).get('lease');guest=peers.get('p2',{}).get('lease')
    require(isinstance(host,str) and HEX.fullmatch(host) and isinstance(guest,str) and HEX.fullmatch(guest) and host!=guest,'peer leases missing or aliased')
    selected=[v for v in history if v.get('event')=='selected'];baselines=[v for v in history if v.get('event')=='baseline']
    require(len(selected)==len(baselines)==1,'peer selection/baseline missing or duplicated')
    selected=selected[0];baseline=baselines[0]
    require(baseline['epoch']==host and baseline['revision']==1 and len(baseline['rows'])==1,'peer baseline identity differs')
    ship=int(selected['ship']);inside=int(selected['inside']);outside=int(selected['outside'])
    entity=record(baseline['rows'][0]);fields=baseline['rows'][0].split(',')
    require(entity==ship and fields[1:4]==['ship',selected['macro'],selected['owner']],'peer selected descriptor differs')
    sent={};previous=0;phases=set()
    for event in history:
        if event.get('event')!='observation':continue
        row=decode(event['packet']);phase=event['phase'];require(type(phase) is int and 0<=phase<8,'invalid peer phase')
        dock=0 if STATES[phase]=='FREE' else inside if STATES[phase]=='STORED' else outside
        require(row.recipient==host and row.epoch==host and row.revision==1 and row.ship==ship
                and row.sequence>previous and row.state==STATES[phase] and row.dock==dock,'invalid peer observation')
        previous=row.sequence;phases.add(phase);sent[row.sequence]=(encode(replace(row,recipient=guest)),phase)
    require(phases==set(range(8)),'peer phases incomplete')
    canonical=[];startup_requested=False;startup_rejected=False;connected=False;committed=False;ready=False;world_ready=False;complete=False
    received={};last_sequence=0;phase_reads=[];native_reads=[];completed_phases=[];wire_events=0;prepared={};pending_event=None
    def output(value):canonical.append('[X4LAN] '+value)
    for line in text.splitlines():
        if '[X4LAN] ATTACH_' not in line:canonical.append(line);continue
        value=line.split('[X4LAN] ',1)[1]
        require(not complete and 'STOPPED' not in value,'component stopped or emitted evidence after completion')
        if value.startswith('ATTACH_SESSION_DOCK '):
            require(committed and ready,'native target query before session admission')
            output(value.replace('ATTACH_SESSION_DOCK event mode=load ','ATTACH_DOCK_EVENT ',1));continue
        if value.startswith('ATTACH_NATIVE read '):
            m=re.fullmatch(r'ATTACH_NATIVE read phase=(\d+) .*',value);require(m and len(native_reads)<len(phase_reads),'native phase read preceded Lua read')
            native_reads.append(int(m[1]));canonical.append(line);continue
        if not value.startswith(('ATTACH_SESSION ','ATTACH_WORLD ')):
            canonical.append(line);continue  # Existing gate audits ALL native execution/selection lines.
        if value=='ATTACH_SESSION begin seed='+seed:continue
        if value.startswith('ATTACH_SESSION selected '):
            expected='ATTACH_SESSION selected '+' '.join(f'{k}={selected[k]}' for k in ('ship','carrier','inside','outside','code','macro','owner'))
            require(value==expected,'native/peer selected descriptor differs')
            output(f"ATTACH_CONTROLLER selected shipIndex={ship} carrierIndex={selected['carrier']} inside={inside} outside={outside} identity={seed}:{ship}:{selected['macro']}:{selected['owner']}:{selected['code']}")
        elif value=='ATTACH_SESSION startup-request role=p1':
            require(not startup_requested and not connected,'startup fault request out of order');startup_requested=True
        elif value.startswith('ATTACH_SESSION startup-rejected '):
            require(startup_requested and not startup_rejected and not connected
                    and value=='ATTACH_SESSION startup-rejected reason=native attachment session requires verified saved references',
                    'startup rejection missing, duplicated or unrelated');startup_rejected=True
        elif value.startswith('ATTACH_SESSION connected '):
            require(startup_rejected,'network component connected before the rejected-start regression')
            require(not connected and value=='ATTACH_SESSION connected lease='+guest,'native recipient lease differs');connected=True
        elif value.startswith('ATTACH_SESSION baseline '):
            require(connected and not committed and value==f'ATTACH_SESSION baseline epoch={host} revision=1 ship={ship}','native baseline differs');committed=True
        elif value.startswith('ATTACH_WORLD READY '):
            require(ready and not world_ready and value=='ATTACH_WORLD READY epoch='+host,'world native readiness differs');world_ready=True
        elif value.startswith('ATTACH_SESSION wire '):
            m=re.fullmatch(r'ATTACH_SESSION wire at=(\d+\.\d+) result=(applied|duplicate) value=(.*)',value)
            require(m and committed,'malformed or unadmitted network evidence');at=float(m[1]);row=decode(m[3]);wire_events+=1
            require(row.sequence in sent and m[3]==sent[row.sequence][0],'native wire absent from exact peer transmission')
            if m[2]=='applied':
                require(row.sequence>last_sequence and math.isfinite(at),'network replay accepted as new')
                received[row.sequence]=(row,at,sent[row.sequence][1]);last_sequence=row.sequence
            else:require(row.sequence in received,'duplicate without original receive')
        elif value.startswith('ATTACH_SESSION prepare '):
            m=re.fullmatch('ATTACH_SESSION prepare '+COMMAND+r' ship=(\d+) at=(\d+\.\d+)',value)
            require(m and world_ready,'preparation before native session readiness')
            phase,op,seq=map(int,m.groups()[:3]);at=float(m[8]);require(int(m[7])==ship and seq in received,'preparation lacks selected network intent')
            row,received_at,wire_phase=received[seq]
            require(phase==wire_phase and 0<=at-received_at<=.75001,'preparation intent expired or from another phase')
            require(op not in prepared,'duplicate local preparation');prepared[op]=(seq,at,phase)
            output(value.replace('ATTACH_SESSION','ATTACH_CONTROLLER',1).rsplit(' ship=',1)[0])
        elif value.startswith('ATTACH_SESSION dispatch '):
            m=re.fullmatch('ATTACH_SESSION dispatch '+COMMAND+r' at=(\d+\.\d+)',value)
            require(m,'malformed native dispatch');op=int(m[2]);seq=int(m[3]);at=float(m[7])
            require(op in prepared and prepared[op][0]==seq and 0<=at-prepared[op][1]<=.50001
                    and 0<=at-received[seq][1]<=.75001,'native dispatch exceeded original capability lifetime')
            output(value.replace('ATTACH_SESSION','ATTACH_CONTROLLER',1).rsplit(' at=',1)[0])
        elif value.startswith('ATTACH_SESSION event '):
            m=re.fullmatch(r'ATTACH_SESSION event kind=(Ready|Window|Applied) value=(.*)',value);require(m,'unknown native event')
            fields=m[2].split('|');require(fields[:2]==[seed,host],'native event seed/authority differs')
            if m[1]=='Ready':
                require(committed and not ready and len(fields)==2,'native readiness order differs');ready=True
                output('ATTACH_CONTROLLER execution-ready epoch='+seed)
            else:
                require(len(fields)==4 and fields[2].isdigit() and int(fields[2]) in prepared and pending_event is None,'uncorrelated native event')
                pending_event=(m[1],int(fields[2]),fields[3])
        elif value.startswith('ATTACH_SESSION window '):
            m=re.fullmatch(r'ATTACH_SESSION window operation=(\d+) result=([a-z]+)',value)
            require(m and pending_event and pending_event[:2]==('Window',int(m[1])) and pending_event[2] in ('0','1'),'window callback differs')
            if pending_event[2]=='0':require(m[2]=='rejected','denied window dispatched')
            pending_event=None;output(value.replace('ATTACH_SESSION','ATTACH_CONTROLLER',1))
        elif value.startswith('ATTACH_SESSION execution-ack '):
            m=re.fullmatch(r'ATTACH_SESSION execution-ack operation=(\d+) outcome=(applied|rejected)',value)
            require(m and pending_event==('Applied',int(m[1]),m[2]),'acknowledgment callback differs')
            pending_event=None;output(value.replace('ATTACH_SESSION','ATTACH_CONTROLLER',1))
        elif value.startswith('ATTACH_SESSION read '):
            m=re.fullmatch(r'ATTACH_SESSION read phase=(\d+) ship=(\d+) code=(\S+) state=(FREE|STORED|EXTERNAL) dock=(\d+) allowPose=(true|false) at=(\d+\.\d+)',value)
            require(m and int(m[1])==len(phase_reads) and int(m[2])==ship and m[3]==selected['code']
                    and (m[6]=='true')==(m[4]=='FREE'),'native pose-admission read differs')
            require(last_sequence in received,'phase read lacks network observation')
            latest,at,wire_phase=received[last_sequence]
            require(wire_phase==int(m[1]) and latest.state==m[4] and latest.dock==int(m[5])
                    and 0<=float(m[7])-at<=2.00001,'phase read lacks fresh matching network state')
            phase_reads.append(int(m[1]));output(f'ATTACH_CONTROLLER read phase={m[1]} state={m[4]} dock={m[5]} shipIndex={m[2]}')
        elif value.startswith('ATTACH_SESSION phase-complete '):
            m=re.fullmatch(r'ATTACH_SESSION phase-complete phase=(\d+)',value)
            require(m and native_reads==list(range(len(completed_phases)+1)) and int(m[1])==len(completed_phases),'phase completion before ordered native read')
            completed_phases.append(int(m[1]))
        elif value=='ATTACH_SESSION complete phases=8 native_host=false whole_universe=false':
            require(completed_phases==list(range(8)) and pending_event is None,'native component cycle incomplete')
            complete=True;output('ATTACH_CONTROLLER complete phases=8 operations=9')
        else:raise ValueError('unknown component evidence')
    require(connected and committed and ready and world_ready and complete,'component evidence incomplete')
    return '\n'.join(canonical),dict(rejected_start_then_ready=True,received_wire_events=wire_events,received_new_observations=len(received),host_epoch=host,recipient_lease=guest)


def validate(text,seed,history,stats):
    admission(text,seed)
    canonical,network=project(text,seed,history,stats)
    result=execution(canonical,seed)
    result.update(network,native_udp_attachment_component=True,network_attachment_acceptance=True,
                  native_host=False,whole_universe_adapter=False,two_native_acceptance=False)
    return result
