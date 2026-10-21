"""Require deliberate stale and paused native rejection with unchanged readbacks."""
import re
from dev.validate_attachment_execution import validate as execution_validate


def validate(text,seed):
    projected=[];selected=None;command=None;fault=None;verified=[];actions=0
    def need(condition,reason):
        if not condition:raise ValueError(reason)
    def matches(m):
        return m and fault and int(m[1])==fault['operation'] and m[2]==fault['kind']
    for line in text.splitlines():
        if '[X4LAN] ATTACH_' not in line:projected.append(line);continue
        value=line.split('[X4LAN] ',1)[1]
        if value.startswith('ATTACH_NATIVE selected '):
            m=re.search(r'shipIndex=(\d+).* code=(\S+) inside=(\d+)',value)
            need(m,'missing selected fault actor');selected={'ship':m[1],'code':m[2],'dock':m[3]}
        if value.startswith('ATTACH_CONTROLLER dispatch '):
            m=re.fullmatch(r'ATTACH_CONTROLLER dispatch phase=(\d+) operation=(\d+) sequence=(\d+) action=(\S+) state=(\S+) dock=(\d+)',value)
            need(m and fault is None,'new dispatch during unresolved fault')
            command=m
        if value.startswith('ATTACH_FAULT armed '):
            m=re.fullmatch(r'ATTACH_FAULT armed operation=(\d+) kind=(stale|paused) shipIndex=(\d+) state=FREE dock=0',value)
            need(m and selected and command and fault is None and len(verified)<2 and actions==0,'invalid fault arming')
            need(m[2]==('stale','paused')[len(verified)] and m[3]==selected['ship'] and m[1]==command[2]
                 and (command[1],command[4],command[5],command[6])==('1','ATTACH','STORED',selected['dock']),'fault has another action, actor or order')
            fault={'operation':int(m[1]),'kind':m[2],'stage':'armed'}
        elif value.startswith('ATTACH_FAULT sent '):
            m=re.fullmatch(r'ATTACH_FAULT sent operation=(\d+) kind=(stale|paused) elapsed=(\d+\.\d+) paused=(true|false)',value)
            need(matches(m) and fault['stage']=='armed','uncorrelated delayed send')
            elapsed=float(m[3]);need((2.5 if m[2]=='stale' else .05)<=elapsed<4.5 and (m[4]=='true')==(m[2]=='paused'),'fault delay or actual pause differs')
            fault.update(stage='sent',elapsed=elapsed)
        elif value.startswith('ATTACH_EXEC applied ') and fault:
            m=re.fullmatch(r'ATTACH_EXEC applied operation=(\d+) outcome=rejected reason=(expired|paused)',value)
            need(m and fault['stage']=='sent' and int(m[1])==fault['operation'] and m[2]==('expired' if fault['kind']=='stale' else 'paused'),'fault native reason/outcome differs')
            fault.update(stage='consumed',reason=m[2])
        elif value.startswith('ATTACH_EXEC action '):
            need(fault is None and len(verified)==2,'native mutation before both fault readbacks')
            actions+=1
        elif value.startswith('ATTACH_CONTROLLER execution-ack ') and fault:
            m=re.fullmatch(r'ATTACH_CONTROLLER execution-ack operation=(\d+) outcome=rejected',value)
            need(m and fault['stage']=='consumed' and int(m[1])==fault['operation'],'fault acknowledgment lacks native rejection')
            fault['stage']='lua'
        elif value.startswith('ATTACH_FAULT lua '):
            m=re.fullmatch(r'ATTACH_FAULT lua operation=(\d+) kind=(stale|paused) shipIndex=(\d+) state=FREE dock=0',value)
            need(matches(m) and fault['stage']=='lua' and m[3]==selected['ship'],'fault Lua readback differs')
            fault['stage']='native'
        elif value.startswith('ATTACH_FAULT native '):
            m=re.fullmatch(r'ATTACH_FAULT native operation=(\d+) kind=(stale|paused) shipIndex=(\d+) code=(\S+) stored=(0|false) docked=(0|false) dock=0 paused=(0|1|true|false)',value)
            need(matches(m) and fault['stage']=='native' and m[3]==selected['ship'] and m[4]==selected['code']
                 and (m[7] in ('1','true'))==(fault['kind']=='paused'),'fault native actor/state/pause differs')
            fault['stage']='verify'
        elif value.startswith('ATTACH_FAULT verified '):
            m=re.fullmatch(r'ATTACH_FAULT verified operation=(\d+) kind=(stale|paused)',value)
            need(matches(m) and fault['stage']=='verify','fault completes before independent readbacks')
            verified.append({k:v for k,v in fault.items() if k!='stage'});fault=None
        elif value.startswith('ATTACH_FAULT '):raise ValueError('unknown fault evidence')
        elif fault and value.startswith(('ATTACH_CONTROLLER prepare ','ATTACH_CONTROLLER read ','ATTACH_NATIVE read ','ATTACH_DOCK_EVENT ','ATTACH_CONTROLLER complete ')):
            raise ValueError('new work/readback before fault verification')
        if not value.startswith('ATTACH_FAULT '):projected.append(line)
    need(fault is None and len(verified)==2,'both native faults must be completely verified')
    result=execution_validate('\n'.join(projected),seed)
    need(result['native_rejected_consumptions']>=2 and actions==9,'missing native rejection or successful recovery')
    result.update(execution_negative_timing_acceptance=True,native_faults=verified)
    return result
