"""Causal raw-evidence gate for the isolated saved-pair controller experiment."""
import re
from dev.validate_dock_import import capture

STATES=('FREE','STORED','FREE','EXTERNAL','STORED','EXTERNAL','FREE','STORED')


def validate(text,seed):
    registry=capture(text,'load',seed)
    selected=None
    lua_selected=False
    phase=0
    pending=None
    operation=0
    sequence=0
    phase_actions=[]
    lua_read=False
    completed=False
    query=None
    query_serial=2  # the import qualification already completed two native queries
    target_readback=None
    queries=0
    imported=False
    for line in text.splitlines():
        if f'[X4LAN] DOCK_IMPORT complete mode=load seed={seed} count={registry["count"]}' in line:
            imported=True
        if '[X4LAN] ATTACH_' not in line:
            continue
        value=line.split('[X4LAN] ',1)[1]
        if not imported:
            raise ValueError('controller evidence precedes completed saved dock import')
        if completed or 'STOPPED' in value:
            raise ValueError('controller failed or emitted evidence after completion')
        if value.startswith('ATTACH_NATIVE selected '):
            m=re.fullmatch(r'ATTACH_NATIVE selected seed=([a-f0-9]{32}) shipIndex=(\d+) carrierIndex=(\d+) ship=(0x[0-9a-f]+) carrier=(0x[0-9a-f]+) code=(\S+) inside=(\d+) outside=(\d+)',value)
            if not m or selected or m[1]!=seed:
                raise ValueError('invalid native controller selection')
            selected=dict(ship=int(m[2]),carrier=int(m[3]),ship_handle=m[4],carrier_handle=m[5],code=m[6],inside=int(m[7]),outside=int(m[8]))
            if not 1<=selected['ship']<=249999 or not 1<=selected['carrier']<=249999 or selected['ship']==selected['carrier'] or int(m[4],16)==int(m[5],16):
                raise ValueError('aliased or invalid selected saved ship')
            for index,flag in ((selected['inside'],6),(selected['outside'],5)):
                row=registry['rows'].get(index)
                if not row or row[0][flag]!='1' or int(row[0][2],16)!=int(selected['carrier_handle'],16):
                    raise ValueError('selected target differs from saved dock graph')
        elif value.startswith('ATTACH_CONTROLLER selected '):
            m=re.fullmatch(r'ATTACH_CONTROLLER selected shipIndex=(\d+) carrierIndex=(\d+) inside=(\d+) outside=(\d+) identity=(\S+)',value)
            if not m or not selected or lua_selected or list(map(int,m.groups()[:4])) != [selected[k] for k in ('ship','carrier','inside','outside')]:
                raise ValueError('Lua selection differs from native saved indices')
            if not m[5].startswith(f"{seed}:{selected['ship']}:") or not m[5].endswith(':'+selected['code']):
                raise ValueError('Lua selected identity differs')
            lua_selected=True
        elif value.startswith('ATTACH_DOCK_EVENT '):
            m=re.fullmatch(r'ATTACH_DOCK_EVENT kind=(Begin|Row|End|Error) value=(.*)',value)
            if not m or not lua_selected or m[1]=='Error':
                raise ValueError('invalid target readback event')
            kind,f=m[1],m[2].split('|')
            if len(f)<2 or f[0]!=seed:
                raise ValueError('target readback seed differs')
            serial=int(f[1])
            if kind=='Begin':
                if query or len(f)!=5 or serial!=query_serial+1 or f[2]!=f[3] or int(f[4])!=registry['count']:
                    raise ValueError('target query order/bounds differ')
                query=dict(serial=serial,index=int(f[2]),row=None);target_readback=None
            elif kind=='Row':
                if not query or len(f)!=12 or serial!=query['serial'] or int(f[2])!=query['index'] or query['row']:
                    raise ValueError('unrequested or duplicate target row')
                definition=registry['rows'].get(query['index'])
                if not definition or tuple(f[3:11])!=definition[0] or not 0<=float(f[11])<=.05:
                    raise ValueError('fresh target witnesses differ')
                query['row']=True
            else:
                if not query or len(f)!=6 or not query['row'] or f[5]!='1' or list(map(int,f[1:5])) != [query['serial'],query['index'],query['index'],registry['count']]:
                    raise ValueError('target query ended without complete readback')
                query_serial=serial;target_readback=query['index'];query=None;queries+=1
        elif value.startswith('ATTACH_CONTROLLER dispatch '):
            m=re.fullmatch(r'ATTACH_CONTROLLER dispatch phase=(\d+) operation=(\d+) sequence=(\d+) action=(UNDOCK|ATTACH) state=(FREE|EXTERNAL|STORED) dock=(\d+)',value)
            if not m or not lua_selected or pending or lua_read or int(m[1])!=phase or int(m[2])!=operation+1 or int(m[3])<max(1,sequence):
                raise ValueError('controller dispatch order differs')
            action,state,dock=m[4],m[5],int(m[6])
            if action=='ATTACH' and (query or target_readback!=dock):
                raise ValueError('attachment dispatched before fresh exact target readback')
            pending=(int(m[2]),phase,action,state,dock);sequence=int(m[3]);target_readback=None
        elif value.startswith('ATTACH_NATIVE action '):
            m=re.fullmatch(r'ATTACH_NATIVE action operation=(\d+) phase=(\d+) action=(UNDOCK|ATTACH) state=(FREE|EXTERNAL|STORED) dock=(\d+) allowed=(true|false|1|0) shipIndex=(\d+) code=(\S+)',value)
            actual=(int(m[1]),int(m[2]),m[3],m[4],int(m[5])) if m else None
            if not m or pending!=actual or m[6] not in ('true','1') or int(m[7])!=selected['ship'] or m[8]!=selected['code']:
                raise ValueError('native mutation rejected or unrelated to pending dispatch')
            operation=actual[0];phase_actions.append(actual[2:]);pending=None
        elif value.startswith('ATTACH_CONTROLLER read '):
            m=re.fullmatch(r'ATTACH_CONTROLLER read phase=(\d+) state=(FREE|EXTERNAL|STORED) dock=(\d+) shipIndex=(\d+)',value)
            if not m or not lua_selected or pending or query or lua_read or int(m[1])!=phase or phase>=len(STATES) or m[2]!=STATES[phase] or int(m[4])!=selected['ship']:
                raise ValueError('Lua readback before completed native actions')
            wanted_dock=0 if m[2]=='FREE' else selected['inside' if m[2]=='STORED' else 'outside']
            if int(m[3])!=wanted_dock:
                raise ValueError('Lua readback has another saved dock')
            expected=[]
            if phase:
                if STATES[phase-1]!='FREE':expected.append(('UNDOCK','FREE',0))
                if STATES[phase]!='FREE':expected.append(('ATTACH',STATES[phase],wanted_dock))
            if phase_actions!=expected:
                raise ValueError('native transition path omitted or duplicated an action')
            lua_read=True
        elif value.startswith('ATTACH_NATIVE read '):
            m=re.fullmatch(r'ATTACH_NATIVE read phase=(\d+) shipIndex=(\d+) code=(\S+) stored=(true|false|1|0) docked=(true|false|1|0) dock=(\d+)',value)
            if not m or not lua_read or int(m[1])!=phase or int(m[2])!=selected['ship'] or m[3]!=selected['code']:
                raise ValueError('native readback phase/identity differs')
            state=STATES[phase];wanted_dock=0 if state=='FREE' else selected['inside' if state=='STORED' else 'outside']
            if (m[4] in ('true','1'))!=(state=='STORED') or (m[5] in ('true','1'))!=(state!='FREE') or int(m[6])!=wanted_dock:
                raise ValueError('native readback does not match requested attachment')
            phase+=1;lua_read=False;phase_actions=[]
        elif value.startswith('ATTACH_CONTROLLER complete '):
            m=re.fullmatch(r'ATTACH_CONTROLLER complete phases=8 operations=(\d+)',value)
            if not m or phase!=8 or int(m[1])!=operation or operation!=9 or pending or query or lua_read:
                raise ValueError('native controller cycle incomplete')
            completed=True
        else:
            raise ValueError('unknown native controller evidence')
    if not completed:
        raise ValueError('native controller cycle did not complete')
    return dict(passed=True,phases=phase,operations=operation,correlated_target_queries=queries,
                selected=selected,network_attachment_acceptance=False)
