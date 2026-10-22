"""Developer-only host checkpoint and shared numeric-seed planning CLI.

No command launches X4, captures a save, changes a profile, or grants admission.
Use a completed disposable native snapshot and its independently measured content
identity. Human-delivered packages do not invoke this experimental tool.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x4lan import seed, world_start, world_store, station_catalog


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    make=commands.add_parser('recipe');make.add_argument('--content',type=Path,required=True)
    make.add_argument('--generation-seed',type=int,required=True);make.add_argument('--output',type=Path,required=True)
    init=commands.add_parser('init');init.add_argument('--recipe',type=Path,required=True);init.add_argument('--store',type=Path,required=True)
    for name in ('publish','head'):
        command=commands.add_parser(name);command.add_argument('--store',type=Path,required=True);command.add_argument('--world-id',required=True)
        if name=='publish':command.add_argument('--snapshot',type=Path,required=True);command.add_argument('--expected-revision',type=int,required=True)
    plan=commands.add_parser('plan');plan.add_argument('--recipe',type=Path,required=True);plan.add_argument('--profile',type=Path,required=True)
    plan.add_argument('--observed-content',type=Path,required=True)
    compare=commands.add_parser('compare-stations');compare.add_argument('host',type=Path);compare.add_argument('guest',type=Path)
    compare.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
    if args.action=='recipe':
        value=world_start.create(**read(args.content),generation_seed=args.generation_seed)
        with args.output.open('x',encoding='utf-8') as stream:json.dump(value,stream,indent=2)
        print('Recipe prepared. Equal station layout is NOT yet proven. World:',value['world_id'])
    elif args.action=='init':
        store=world_store.WorldStore.create(args.store,read(args.recipe));print('Empty world repository:',store.recipe['world_id'])
    elif args.action=='plan':
        print(json.dumps(dict(arguments=world_start.arguments(read(args.recipe),args.profile,observed_content=read(args.observed_content)),
                              launch_performed=False,native_generation_match_verified=False),indent=2))
    elif args.action=='compare-stations':
        result=station_catalog.match(read(args.host),read(args.guest))
        with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2)
        print('Station layout:', 'MATCH' if result['station_layout_matches'] else 'MISMATCH')
        return 0 if result['station_layout_matches'] else 1
    else:
        store=world_store.WorldStore(args.store,world_id=args.world_id)
        value=store.publish(args.snapshot,expected_revision=args.expected_revision) if args.action=='publish' else store.latest()
        print(json.dumps(None if value is None else dict(checkpoint=value['info'],manifest=value['manifest']),indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
