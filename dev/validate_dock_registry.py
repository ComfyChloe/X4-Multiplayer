"""Strict local native save/reload evidence for saved dock references."""
import argparse
import json
import math
from pathlib import Path
import re

ROW=re.compile(r'index=(\d+) object=(0x[0-9a-fA-F]+) container=(0x[0-9a-fA-F]+) '
               r'macro=([a-z][a-z0-9_]*_macro) containerMacro=([a-z][a-z0-9_]*_macro) '
               r'external=([01]) storage=([01]) capacity=(\d+) parent=(0x[0-9a-fA-F]+) '
               r'expectedParent=(0x[0-9a-fA-F]+) expectedContainer=(0x[0-9a-fA-F]+) poseError=([^\s]+) reverse=1')


def read(log,mode,seed):
    if ('SEED_DOCKS failed' in log or 'SEED_DOCKS native error' in log
            or 'SEED_DOCKS native registry timed out' in log
            or re.search(r'(?:Error in MD cue|Lua error).*?(?:X4LAN|x4lan|probe)',log)
            or re.search(r'extensions[/\\]x4_lan_lab[/\\](?:md|ui)[/\\][^\r\n]+\(\d+\):',log)):
        raise ValueError('Native probe/parser failure')
    records=[line.split('SEED_DOCKS ',1)[1] for line in log.splitlines() if 'SEED_DOCKS ' in line]
    if len(records)<4:raise ValueError('Missing dock observations')
    header=re.fullmatch(r'begin mode='+mode+r' id='+seed+r' count=(\d+)',records[0])
    if not header:raise ValueError('Missing or unresolved dock header')
    count=int(header[1])
    if not 1<=count<=250000 or len(records)!=count+3:raise ValueError('Incomplete or repeated dock observations')
    if records[-2:]!=[f'end mode={mode} id={seed} count={count}',f'complete mode={mode} id={seed} count={count}']:
        raise ValueError('Missing or reordered native completion')
    rows={};objects=set()
    for index,record in enumerate(records[1:-2],1):
        match=ROW.fullmatch(record.removeprefix('row ')) if record.startswith('row ') else None
        if not match:raise ValueError('Unresolved or malformed dock row')
        i,object_id,container,macro,parent_macro,external,storage,capacity,parent,expected_parent,expected_container,error=match.groups()
        object_id=int(object_id,16);container=int(container,16)
        parent=int(parent,16);expected_parent=int(expected_parent,16);expected_container=int(expected_container,16)
        error=float(error)
        if int(i)!=index or not object_id or not container or object_id==container or object_id in objects:
            raise ValueError('Invalid or duplicate saved dock reference')
        if not parent or parent!=expected_parent or container!=expected_container:
            raise ValueError('Dock disagrees with independently saved parent/container')
        if not math.isfinite(error) or not 0<=error<=0.05:raise ValueError('Dock moved relative to saved container position')
        objects.add(object_id)
        rows[index]=(object_id,container,macro,parent_macro,int(external),int(storage),int(capacity),parent,error)
    completed=(f'SEED_PROBE saved success=true id={seed}' if mode=='capture' else f'SEED_PROBE loaded id={seed}')
    if log.count(completed)!=1 or log.index(completed)<log.index('SEED_DOCKS complete '):
        raise ValueError('Native save/load must complete after dock readback')
    return rows


def validate(capture,loaded,seed):
    if not re.fullmatch(r'[a-f0-9]{32}',seed):raise ValueError('Invalid seed')
    before=read(capture,'capture',seed);after=read(loaded,'load',seed)
    if before.keys()!=after.keys():raise ValueError('Saved dock indices changed')
    mapping={};reverse={};changed=0
    for index,old in before.items():
        new=after[index]
        if old[2:7]!=new[2:7]:raise ValueError('Saved dock native definitions changed')
        for a,b in ((old[0],new[0]),(old[1],new[1]),(old[7],new[7])):
            if a in mapping and mapping[a]!=b or b in reverse and reverse[b]!=a:
                raise ValueError('Saved component graph split or merged after reload')
            mapping[a]=b;reverse[b]=a
        changed+=old[0]!=new[0]
    return dict(saved_docks=len(before),saved_indices_definitions_and_component_graph_equal=True,
                independent_saved_parent_container_and_relative_pose_verified=True,
                locally_renumbered_docks=changed,maximum_relative_pose_error_m=max(r[-1] for r in after.values()),
                reverse_component_lookup_preserved=True,native_save_reload=True,
                cross_pc_identity_not_tested=True,attachment_network_authority_not_tested=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args()
    seed=json.loads((a.run/'state.json').read_text())['seed_id']
    result=validate((a.run/'capture.log').read_text(errors='replace'),(a.run/'load.log').read_text(errors='replace'),seed)
    (a.run/'dock-registry-validation.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
