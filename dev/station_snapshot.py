"""Convert retained native station observations to a checked startup catalog.

A supplied recipe identifies the intended world; it is NOT proof that an actual
fresh game used that numeric seed. Existing capture/load markers prove only the
recorded native save experiment. No game, Steam or profile writes occur here.
"""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x4lan import seed, station_catalog


ROW=re.compile(r'SEED_PROBE station object=(\d+) code=(\S+) macro=(\S+) owner=(\S+) sector=(\S+) pose=(\S+)$')


def extract(text, recipe, *, capture_id, mode):
    if not seed._hex(capture_id,seed.HEX32) or mode not in ('capture','load'):
        raise seed.SeedError('invalid expected native capture identity/mode')
    begin='SEED_PROBE begin mode='+mode+' id='+capture_id+' count='
    end='SEED_PROBE end mode='+mode+' id='+capture_id+' count='
    completion=('SEED_PROBE saved success=true id=' if mode=='capture' else 'SEED_PROBE loaded id=')+capture_id
    phase='before';count=None;rows=[]
    for line in text.splitlines():
        if begin in line:
            if phase!='before':raise seed.SeedError('repeated/out-of-order native catalog begin')
            value=line.split(begin,1)[1]
            if not value.isdigit() or not 1<=int(value)<=station_catalog.MAX_STATIONS:
                raise seed.SeedError('invalid native station count')
            count=int(value);phase='rows'
        elif 'SEED_PROBE station ' in line:
            if phase!='rows':raise seed.SeedError('native station row outside observation boundaries')
            match=ROW.search(line)
            if match is None:raise seed.SeedError('invalid native station row')
            obj,code,macro,owner,sector,pose=match.groups()
            rows.append(dict(object=int(obj),code=code,macro=macro,owner=owner,sector=sector,pose=list(map(float,pose.split(',')))))
            if len(rows)>count:raise seed.SeedError('too many native station rows')
        elif end in line:
            if phase!='rows' or line.split(end,1)[1]!=str(count) or len(rows)!=count:
                raise seed.SeedError('incomplete/out-of-order native catalog end')
            phase='ended'
        elif 'SEED_PROBE saved ' in line or 'SEED_PROBE loaded ' in line:
            if not line.endswith(completion):raise seed.SeedError('native completion identity/status mismatch')
            if phase!='ended':raise seed.SeedError('native completion precedes catalog or repeats')
            phase='complete'
    if phase!='complete':raise seed.SeedError('native catalog/completion missing')
    if re.search(r'(?:Error in MD cue|Lua error).*?(?:X4LAN|x4lan|seed_probe)',text):
        raise seed.SeedError('native mod error during observation')
    return station_catalog.create(recipe,rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log',type=Path);parser.add_argument('--recipe',type=Path,required=True)
    parser.add_argument('--capture-id',required=True);parser.add_argument('--mode',choices=('capture','load'),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    catalog=extract(args.log.read_text(errors='replace'),json.loads(args.recipe.read_text()),capture_id=args.capture_id,mode=args.mode)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(catalog,stream,indent=2)
    print('Recorded stations:',len(catalog['stations']),'- fresh numeric-seed equivalence is not established.')


if __name__=='__main__':main()
