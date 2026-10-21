"""Audit every native prepare/consume attempt before reusing the settled-state gate.

Rejected attempts are counted and must have complete no-mutation outcomes. Only
then are successful actions projected into the existing controller state-machine
gate; raw evidence is never rewritten. Unexpected actions/errors remain failures.
"""
import re
from dev.validate_attachment_controller import validate as validate_controller

COMMAND=r'phase=(\d+) operation=(\d+) sequence=(\d+) action=(UNDOCK|ATTACH) state=(FREE|EXTERNAL|STORED) dock=(\d+)'


def validate(text,seed):
    canonical=[];pending=None;last_operation=0;successful=0;rejected=0;unissued=0
    imported=False;selected=False;ready=False;completed=False
    def require(condition,reason):
        if not condition:raise ValueError(reason)
    for line in text.splitlines():
        if f'DOCK_IMPORT complete mode=load seed={seed} count=' in line:imported=True
        if '[X4LAN] ATTACH_' not in line:
            canonical.append(line);continue
        value=line.split('[X4LAN] ',1)[1]
        require(imported and not completed,'execution evidence outside completed import/cycle boundary')
        if value.startswith('ATTACH_CONTROLLER selected '):selected=True
        if value.startswith('ATTACH_CONTROLLER execution-ready '):
            require(selected and not ready and value=='ATTACH_CONTROLLER execution-ready epoch='+seed,'invalid native execution admission')
            ready=True
        elif value.startswith('ATTACH_CONTROLLER prepare '):
            m=re.fullmatch('ATTACH_CONTROLLER prepare '+COMMAND,value)
            require(m and ready and pending is None,'overlapping or invalid execution preparation')
            phase,operation,sequence,action,state,dock=m.groups()
            require(int(operation)==last_operation+1,'execution operation is not the next local operation')
            last_operation=int(operation)
            pending=dict(phase=phase,operation=operation,sequence=sequence,action=action,state=state,dock=dock,stage='prepare')
        elif value.startswith('ATTACH_EXEC prepared '):
            m=re.fullmatch(r'ATTACH_EXEC prepared operation=(\d+) accepted=(true|false|1|0)',value)
            require(m and pending and pending['stage']=='prepare' and m[1]==pending['operation'],'unmatched native preparation')
            pending['stage']='window' if m[2] in ('true','1') else 'denied-window'
        elif value.startswith('ATTACH_CONTROLLER dispatch '):
            m=re.fullmatch('ATTACH_CONTROLLER dispatch '+COMMAND,value)
            require(m and pending and pending['stage']=='window','dispatch without accepted native preparation')
            require(tuple(m.groups())==tuple(pending[k] for k in ('phase','operation','sequence','action','state','dock')),'dispatch differs from prepared capability')
            pending['stage']='issued-window'
        elif value.startswith('ATTACH_CONTROLLER window '):
            m=re.fullmatch(r'ATTACH_CONTROLLER window operation=(\d+) result=([a-z]+)',value)
            require(m and pending and m[1]==pending['operation'],'uncorrelated native window callback')
            if m[2]=='issued':
                require(pending['stage']=='issued-window','window claims issue without controller dispatch')
                pending['stage']='apply'
            else:
                valid=(pending['stage']=='denied-window' and m[2]=='rejected') or (
                    pending['stage']=='window' and m[2] in ('expired','paused','changed','blocked','stale'))
                require(valid,'window ended after dispatch or with unknown result')
                pending=None;unissued+=1
        elif value.startswith('ATTACH_EXEC applied '):
            m=re.fullmatch(r'ATTACH_EXEC applied operation=(\d+) outcome=(applied|rejected)',value)
            require(m and pending and pending['stage']=='apply' and m[1]==pending['operation'],'native consumption has unknown or uncorrelated outcome')
            pending['outcome']=m[2];pending['stage']='action' if m[2]=='applied' else 'ack'
        elif value.startswith('ATTACH_EXEC action '):
            m=re.fullmatch(r'ATTACH_EXEC action operation=(\d+) action=(UNDOCK|ATTACH) state=(FREE|EXTERNAL|STORED) dock=(\d+) shipIndex=(\d+) code=(\S+)',value)
            require(m and pending and pending['stage']=='action','native action before exact accepted consumption')
            require(tuple(m.groups()[:4])==tuple(pending[k] for k in ('operation','action','state','dock')),'native action differs from prepared command')
            number=successful+1
            canonical.append('[X4LAN] ATTACH_CONTROLLER dispatch phase={phase} operation={number} sequence={sequence} action={action} state={state} dock={dock}'.format(**pending,number=number))
            canonical.append('[X4LAN] ATTACH_NATIVE action operation={number} phase={phase} action={action} state={state} dock={dock} allowed=true shipIndex={ship} code={code}'.format(**pending,number=number,ship=m[5],code=m[6]))
            pending['stage']='ack'
        elif value.startswith('ATTACH_CONTROLLER execution-ack '):
            m=re.fullmatch(r'ATTACH_CONTROLLER execution-ack operation=(\d+) outcome=(applied|rejected)',value)
            require(m and pending and pending['stage']=='ack' and m[1]==pending['operation'] and m[2]==pending['outcome'],'acknowledgment precedes or differs from consumed action')
            if m[2]=='applied':
                successful+=1;canonical.append(f'[X4LAN] ATTACH_CONTROLLER acknowledged operation={successful}')
            else:rejected+=1
            pending=None
        else:
            require(not value.startswith(('ATTACH_EXEC ','ATTACH_NATIVE action ','ATTACH_CONTROLLER acknowledged ')),'unexpected native execution evidence or direct-action bypass')
            if any(value.startswith(prefix) for prefix in ('ATTACH_CONTROLLER read ','ATTACH_NATIVE read ','ATTACH_CONTROLLER complete ','ATTACH_DOCK_EVENT ')):
                require(pending is None and ready,'readback/query/completion before native exchange resolved')
            if value.startswith('ATTACH_CONTROLLER complete '):completed=True
            canonical.append(line)
    require(pending is None and ready and completed,'native execution qualification incomplete')
    result=validate_controller('\n'.join(canonical),seed)
    result.update(native_prepare_attempts=last_operation,native_rejected_consumptions=rejected,
                  native_unissued_windows=unissued,native_execution_consumer=True,
                  execution_negative_timing_acceptance=False)
    return result
