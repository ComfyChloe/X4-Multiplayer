"""Causal native startup admission order, separate from ongoing-world acceptance."""
import re
from dev.validate_dock_import import capture


def admission_order(text,seed,count):
    loaded=[];full=[];dock_ready=[];gate_ready=[];observed=[];done=[];reads=[];inspect=[]
    for index,line in enumerate(text.splitlines()):
        if '[X4LAN] SEED_ADMISSION STOPPED' in line or '[X4LAN] SEED_GATE STOPPED' in line:
            raise ValueError('native admission stopped')
        message=line.split('[X4LAN] ',1)[1] if '[X4LAN] ' in line else ''
        if message=='SEED_NATIVE loaded count=1 marked=1':loaded.append(index)
        end=re.search(r'DOCK_IMPORT event mode=load kind=End value='+seed+r'\|0\|\d+\|(\d+)\|(\d+)\|1$',line)
        if end and int(end[1])==int(end[2])==count:full.append(index)
        if message=='SEED_DOCK_GATE READY seed='+seed:dock_ready.append(index)
        if message=='SEED_GATE READY seed='+seed+' slot=p2':gate_ready.append(index)
        if '[X4LAN] SEED_ADMISSION ' not in line:continue
        value=line.split('[X4LAN] SEED_ADMISSION ',1)[1]
        request=re.fullmatch(r'request control=(Read|Inspect) paused=true ready=(true|false)',value)
        ready=re.fullmatch(r'ready seed='+seed+r' paused=(true|false) pauseCalls=(\d+) resumeCalls=(\d+)',value)
        if request:
            (reads if request[1]=='Read' else inspect).append((index,request[2]=='true'))
        elif ready:
            paused=ready[1]=='true';pauses=int(ready[2]);resumes=int(ready[3])
            if not ((pauses>0 and resumes==1 and not paused) or (pauses==0 and resumes==0 and paused)):
                raise ValueError('native admission pause ownership differs')
            observed.append((index,dict(pause_calls=pauses,resume_calls=resumes,preserved_existing_pause=paused)))
        elif value==f'complete seed={seed} count={count} pristine=1 loads=1':done.append(index)
        else:raise ValueError('unknown or unsafe native admission evidence')
    if any(len(values)!=1 for values in (loaded,full,dock_ready,gate_ready,observed,done)):
        raise ValueError('native admission evidence missing or duplicated')
    if not loaded[0]<full[0]<dock_ready[0]<gate_ready[0]<observed[0][0]<done[0]:
        raise ValueError('native admission preceded complete loaded registry')
    if not reads or not any(i<full[0] and not ready for i,ready in reads) or not any(ready for _,ready in reads):
        raise ValueError('native initial/live dock request coverage missing')
    if any(i<loaded[0] or (ready and i<gate_ready[0]) or (not ready and i>gate_ready[0]) for i,ready in reads):
        raise ValueError('native dock request crossed admission boundary incorrectly')
    if any(i>done[0] or (ready and i<gate_ready[0]) or (not ready and i>gate_ready[0]) for i,ready in reads+inspect):
        raise ValueError('native request outside its recorded admission phase')
    if not any(i<gate_ready[0] and not ready for i,ready in inspect) or not any(i>observed[0][0] and ready for i,ready in inspect):
        raise ValueError('pristine native marker inspection missing around admission')
    return observed[0][1]


def validate(text,seed):
    registry=capture(text,'load',seed)
    result=admission_order(text,seed,registry['count'])
    return dict(passed=True,seed_id=seed,count=registry['count'],**result,
                native_startup_admission=True,network_attachment_acceptance=False,ongoing_simulation_authority=False)
