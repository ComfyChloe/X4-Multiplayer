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
    selected=re.findall(r'DOCK_STATE_NATIVE selected code=(\S+) carrier=(\S+) inside=(\S+) outside=(\S+)',log)
    if len(selected)!=1 or selected[0][0]==selected[0][1] or selected[0][2]==selected[0][3]:
        raise ValueError('Unique native ship and distinct target docks required')
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
        native[phase]=values
    for phase,stored,docked,ship,carrier in LUA.findall(log):
        phase=int(phase)
        if phase not in expected or phase in local or ship==carrier or (flag(stored),flag(docked))!=expected[phase]:
            raise ValueError('Invalid Lua phase observation')
        local[phase]=(ship,carrier)
    if set(native)!=set(expected) or set(local)!=set(expected) or len(set(local.values()))!=1:
        raise ValueError('Six complete phases with unchanged native handles required')
    if list(native)!=list(expected) or list(local)!=list(expected):
        raise ValueError('Native phases are out of order')
    applied=[int(v) for v in re.findall(r'DOCK_STATE_NATIVE applied phase=(\d+)',log)]
    if applied!=list(range(1,6)):raise ValueError('Missing/replayed native transition actions')
    if log.count('DOCK_STATE complete phases=6')!=1:raise ValueError('Native completion absent or duplicated')
    return dict(phases=6,same_ship_and_carrier=True,exact_external_and_internal_dock_references=True,
                host_network_authority_not_tested=True,persistence_not_tested=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('log',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    result=validate(a.log.read_text(errors='replace'));value=json.dumps(result,indent=2)
    if a.output:a.output.write_text(value+'\n')
    print(value)
