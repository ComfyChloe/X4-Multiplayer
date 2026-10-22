import copy,re,unittest
from dev.validate_attachment_session import project
from dev.validate_attachment_execution import validate as execution
from dev.validate_attachment_controller import STATES
from test_validate_attachment_execution import trace as execution_trace,SEED

HOST='c'*32;GUEST='d'*32


def trace():
    selected=dict(ship='1',carrier='2',inside='2',outside='1',code='ABC-123',macro='ship_fighter_macro',owner='argon')
    history=[dict(event='selected',**selected),dict(event='baseline',epoch=HOST,revision=1,rows=['1,ship,ship_fighter_macro,argon,0,0,0,0,0,0,none,0'])]
    stats=dict(complete=True,native_host=False,send_errors=0,peers=dict(p1=dict(lease=HOST),p2=dict(lease=GUEST)))
    original=execution_trace();out=[];sent=set()
    def emit(value):out.append('[X4LAN] '+value)
    def wire(sequence,phase):
        if sequence in sent:return
        sent.add(sequence);state=STATES[phase];dock=0 if state=='FREE' else 2 if state=='STORED' else 1
        packet=f'UATTACH|1|{HOST}|{HOST}|1|{sequence}|1|{dock}|{state}'
        history.append(dict(event='observation',packet=packet,phase=phase))
        emit(f'ATTACH_SESSION wire at={sequence:.6f} result=applied value='+packet.replace('|'+HOST+'|','|'+GUEST+'|',1))
    for i,line in enumerate(original):
        value=line.split('[X4LAN] ',1)[1]
        if value.startswith('ATTACH_NATIVE selected '):
            emit('ATTACH_SESSION begin seed='+SEED);out.append(line)
        elif value.startswith('ATTACH_CONTROLLER selected '):
            emit('ATTACH_SESSION selected '+' '.join(f'{k}={v}' for k,v in selected.items()))
            emit('ATTACH_SESSION startup-request role=p1')
            emit('ATTACH_SESSION startup-rejected reason=native attachment session requires verified saved references')
            emit('ATTACH_SESSION connected lease='+GUEST)
            emit(f'ATTACH_SESSION baseline epoch={HOST} revision=1 ship=1')
            emit(f'ATTACH_SESSION event kind=Ready value={SEED}|{HOST}');emit('ATTACH_WORLD READY epoch='+HOST)
        elif 'ATTACH_CONTROLLER execution-ready' in value:continue
        elif value.startswith('ATTACH_DOCK_EVENT '):
            following=next(x for x in original[i:] if 'ATTACH_CONTROLLER prepare ' in x)
            phase=int(re.search(r'phase=(\d+)',following)[1]);seq=int(re.search(r'sequence=(\d+)',following)[1])+1
            wire(seq,phase);emit(value.replace('ATTACH_DOCK_EVENT ','ATTACH_SESSION_DOCK event mode=load '))
        elif value.startswith(('ATTACH_CONTROLLER prepare ','ATTACH_CONTROLLER dispatch ')):
            phase=int(re.search(r'phase=(\d+)',value)[1]);seq=int(re.search(r'sequence=(\d+)',value)[1])+1;wire(seq,phase)
            value=re.sub(r'sequence=\d+',f'sequence={seq}',value).replace('ATTACH_CONTROLLER','ATTACH_SESSION',1)
            emit(value+(' ship=1' if ' prepare ' in value else '')+f' at={seq+.01:.6f}')
        elif value.startswith('ATTACH_EXEC prepared '):
            out.append(line);op=re.search(r'operation=(\d+)',value)[1]
            emit(f'ATTACH_SESSION event kind=Window value={SEED}|{HOST}|{op}|1')
        elif value.startswith('ATTACH_CONTROLLER window '):emit(value.replace('ATTACH_CONTROLLER','ATTACH_SESSION',1))
        elif value.startswith('ATTACH_CONTROLLER execution-ack '):
            op=re.search(r'operation=(\d+)',value)[1]
            emit(f'ATTACH_SESSION event kind=Applied value={SEED}|{HOST}|{op}|applied')
            emit(value.replace('ATTACH_CONTROLLER','ATTACH_SESSION',1))
        elif value.startswith('ATTACH_CONTROLLER read '):
            m=re.fullmatch(r'ATTACH_CONTROLLER read phase=(\d+) state=(\w+) dock=(\d+) shipIndex=1',value)
            if m[1]=='0':wire(1,0)
            emit(f'ATTACH_SESSION read phase={m[1]} ship=1 code=ABC-123 state={m[2]} dock={m[3]} allowPose={str(m[2]=="FREE").lower()} at={max(sent)+.02:.6f}')
        elif value.startswith('ATTACH_NATIVE read '):
            out.append(line);phase=re.search(r'phase=(\d+)',value)[1];emit('ATTACH_SESSION phase-complete phase='+phase)
        elif value.startswith('ATTACH_CONTROLLER complete '):emit('ATTACH_SESSION complete phases=8 native_host=false whole_universe=false')
        else:out.append(line)
    return out,history,stats


class AttachmentSessionEvidenceTests(unittest.TestCase):
    def check(self,lines,history,stats):
        projected,network=project('\n'.join(lines),SEED,history,stats)
        return execution(projected,SEED),network

    def test_every_native_action_has_exact_peer_observation_and_original_lifetime(self):
        result,network=self.check(*trace())
        self.assertEqual((result['operations'],network['received_new_observations']),(9,10))
        self.assertEqual(network['host_epoch'],HOST)

    def test_missing_reordered_wrong_or_late_network_and_native_events_are_rejected(self):
        original,history,stats=trace();variants=[]
        for i,line in enumerate(original):
            if any(k in line for k in ('ATTACH_SESSION wire ','ATTACH_SESSION event ','ATTACH_SESSION connected ',
                                       'ATTACH_SESSION baseline ','ATTACH_SESSION phase-complete ','ATTACH_WORLD READY','ATTACH_SESSION startup-')):
                variants.append(original[:i]+original[i+1:])
        variants.append([x for x in original if 'ATTACH_SESSION wire ' not in x]+[x for x in original if 'ATTACH_SESSION wire ' in x])
        for before,after in (('at=2.010000','at=2.800000'),('allowPose=false','allowPose=true'),
                             ('kind=Applied value='+SEED,'kind=Applied value='+HOST),('code=ABC-123 state=FREE','code=XYZ-999 state=FREE')):
            variants.append([x.replace(before,after) for x in original])
        for lines in variants:
            with self.subTest(variant=variants.index(lines)),self.assertRaises(ValueError):self.check(lines,history,stats)

    def test_rejected_start_must_precede_the_valid_native_session(self):
        original,history,stats=trace()
        startup=[v for v in original if 'ATTACH_SESSION startup-' in v]
        variants=[
            [v for v in original if v not in startup]+startup,
            [v.replace('requires verified saved references','already started or stopped') for v in original],
            [v.replace('startup-request role=p1','startup-request role=p2') for v in original],
        ]
        for lines in variants:
            with self.assertRaises(ValueError):self.check(lines,history,stats)

    def test_peer_provenance_cannot_be_inferred_from_native_success(self):
        lines,history,stats=trace()
        for altered in ({**stats,'complete':False},{**stats,'native_host':True},{**stats,'send_errors':1},
                        {**stats,'peers':dict(p1=dict(lease=HOST),p2=dict(lease=HOST))}):
            with self.subTest(stats=altered),self.assertRaises(ValueError):self.check(lines,history,altered)
        changed=copy.deepcopy(history)
        next(row for row in changed if row['event']=='observation')['packet']=next(row for row in changed if row['event']=='observation')['packet'].replace(HOST,'f'*32)
        with self.assertRaises(ValueError):self.check(lines,changed,stats)


if __name__=='__main__':unittest.main()
