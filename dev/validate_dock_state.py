"""Reject incomplete native docking/storage primitive evidence."""
import argparse
import json
from pathlib import Path
import re

READ=re.compile(r'DOCK_STATE_NATIVE read phase=(\d+) code=(\S+) live=(\w+) stored=(\w+) docked=(\w+) sameCarrier=(\w+) sameInside=(\w+) sameOutside=(\w+)')
LUA=re.compile(r'DOCK_STATE read phase=(\d+) stored=(\w+) docked=(\w+) ship=(\d+) carrier=(\d+)')


def validate(log):
    if (any(s in log for s in ('DOCK_STATE failed','DOCK_STATE_NATIVE failed','STOPPED','Lua error','Error in MD cue md.X4LAN'))
            or re.search(r'extensions[/\\]x4_lan_lab[/\\](?:md|ui)[/\\][^\r\n]+\(\d+\):',log)):
        raise ValueError('Native probe/adapter error')
    selected=re.findall(r'DOCK_STATE_NATIVE selected code=([A-Z]{3}-[0-9]{3}) carrier=([A-Z]{3}-[0-9]{3}) ship=(0x[0-9a-fA-F]+) carrierHandle=(0x[0-9a-fA-F]+) inside=(0x[0-9a-fA-F]+) outside=(0x[0-9a-fA-F]+)',log)
    if len(selected)!=1 or log.count('DOCK_STATE_NATIVE selected ')!=1:
        raise ValueError('Unique native ship and distinct target docks required')
    handles=[int(value,16) for value in selected[0][2:]]
    if 0 in handles or len(set(handles))!=4:
        raise ValueError('Resolved distinct ship/carrier/dock handles required')
    if (log.count('DOCK_STATE_NATIVE read ')!=6 or log.count('DOCK_STATE read ')!=6
            or log.count('DOCK_STATE_NATIVE applied ')!=5):
        raise ValueError('Missing, unresolved or duplicate native records')
    def flag(v):
        if v not in ('true','false','1','0'):raise ValueError('Invalid native boolean')
        return v in ('true','1')
    native={};local={}
    expected={0:(True,True),1:(False,True),2:(True,True),3:(False,True),4:(False,False),5:(True,True)}
    for phase,code,*values in READ.findall(log):
        phase=int(phase)
        if phase not in expected or phase in native or code!=selected[0][0]:raise ValueError('Changed/duplicate native ship phase')
        live,stored,docked,carrier,inside,outside=map(flag,values)
        if not live or (stored,docked)!=expected[phase] or carrier!=docked:raise ValueError('Native attachment state differs')
        if phase in (0,5) and not inside:raise ValueError('Wrong original internal dock')
        if phase in (1,3) and not outside:raise ValueError('Wrong selected external dock')
        if phase==4 and (inside or outside):raise ValueError('Undocked ship still attached')
        if phase not in (1,3) and outside or phase in (1,3) and inside:
            raise ValueError('Conflicting native dock references')
        native[phase]=values
    for phase,stored,docked,ship,carrier in LUA.findall(log):
        phase=int(phase)
        if phase not in expected or phase in local or ship==carrier or (flag(stored),flag(docked))!=expected[phase]:
            raise ValueError('Invalid Lua phase observation')
        if (int(ship),int(carrier))!=tuple(handles[:2]):raise ValueError('Lua handles differ from native selection')
        local[phase]=(ship,carrier)
    if set(native)!=set(expected) or set(local)!=set(expected) or len(set(local.values()))!=1:
        raise ValueError('Six complete phases with unchanged native handles required')
    if list(native)!=list(expected) or list(local)!=list(expected):
        raise ValueError('Native phases are out of order')
    actions=re.findall(r'DOCK_STATE_NATIVE applied phase=(\d+) code=(\S+)',log)
    applied=[int(v) for v,code in actions]
    if applied!=list(range(1,6)):raise ValueError('Missing/replayed native transition actions')
    if any(code!=selected[0][0] for _,code in actions):raise ValueError('Transition used another ship')
    if log.count('DOCK_STATE complete phases=6')!=1:raise ValueError('Native completion absent or duplicated')
    events=[('native' if native else 'lua',kind) for native,kind in
            re.findall(r'DOCK_STATE(_NATIVE)? (selected|applied|read|complete)\b',log)]
    wanted=[('native','selected')]
    for phase in range(6):
        if phase:wanted.append(('native','applied'))
        wanted.extend([('lua','read'),('native','read')])
    wanted.append(('lua','complete'))
    if events!=wanted:raise ValueError('Native actions and readbacks are out of causal order')
    return dict(phases=6,same_ship_and_carrier=True,exact_external_and_internal_dock_references=True,
                host_network_authority_not_tested=True,persistence_not_tested=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('log',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    result=validate(a.log.read_text(errors='replace'));value=json.dumps(result,indent=2)
    if a.output:a.output.write_text(value+'\n')
    print(value)
