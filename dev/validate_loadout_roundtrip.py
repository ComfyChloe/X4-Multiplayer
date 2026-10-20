"""Compare native source/clone equipment observations; no game mutation."""
import argparse
import json
import math
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x4lan.entity_descriptor import decode,MAX_BYTES

ROW = re.compile(r'LOADOUT_ROUNDTRIP (expected|observed) id=(\d+) macro=(\S+) rows=(\S*)')
NATIVE = re.compile(r'LOADOUT_ROUNDTRIP_NATIVE id=(\d+) source=(\S+) clone=(\S+) input=(\S+) unused=(\S+) sourceHull=(\S+) cloneHull=(\S+) sourceShield=(\S+) cloneShield=(\S+)')
DESCRIPTOR = re.compile(r'LOADOUT_ROUNDTRIP descriptor id=(\d+) bytes=(\d+) hex=(\S+)')
IDENTITY = re.compile(r'LOADOUT_ROUNDTRIP identity id=(\d+) sourceMacro=(\S+) cloneMacro=(\S+) sourceOwner=(\S+) cloneOwner=(\S+)')


def validate(log,require_serialized=False):
    if (any(v in log for v in ('Lua error', 'Error in MD cue md.X4LAN', 'LOADOUT_ROUNDTRIP failed', 'STOPPED'))
            or re.search(r'extensions[/\\]x4_lan_lab[/\\](?:md|ui)[/\\][^\r\n]+\(\d+\):',log)):
        raise ValueError('Native probe or adapter error')
    rows = {'expected':{}, 'observed':{}}
    for kind, index, macro, signature in ROW.findall(log):
        index = int(index)
        if not 1 <= index <= 8 or index in rows[kind] or not signature:
            raise ValueError('Missing/duplicate/invalid equipment readback')
        entries = signature.split(';')
        if len(set(entries)) != len(entries):
            raise ValueError('Duplicate slot observation')
        for entry in entries:
            fields = entry.split(',')
            if fields[0] == 'software':
                if len(fields) != 2 or not re.fullmatch(r'[a-z0-9_]+', fields[1]):
                    raise ValueError('Invalid software observation')
            elif (len(fields) != 3 or fields[0] not in ('engine', 'shield', 'weapon', 'turret', 'thruster')
                  or not fields[1].isdigit() or int(fields[1]) < 1
                  or (fields[2] not in ('none', '') and not re.fullmatch(r'[a-z0-9_]+_macro', fields[2]))):
                raise ValueError('Invalid equipment slot observation')
        if not any(entry.startswith('engine,') and not entry.endswith(',none') for entry in entries):
            raise ValueError('No installed engine observed')
        rows[kind][index] = (macro, tuple(sorted(entries)))
    wanted = set(range(1,9))
    if set(rows['expected']) != wanted or set(rows['observed']) != wanted:
        raise ValueError('Eight complete source/clone observations required')
    if len({macro for macro, _ in rows['expected'].values()}) != 8:
        raise ValueError('Eight distinct ship macros required')
    if rows['expected'] != rows['observed']:
        raise ValueError('Native equipment slot/software assignments differ')
    native = {}
    for index, source, clone, count, unused, hull, clone_hull, shield, clone_shield in NATIVE.findall(log):
        index = int(index)
        if index in native or index not in wanted or source == clone:
            raise ValueError('Invalid native clone identity')
        values = [float(v) for v in (count,unused,hull,clone_hull,shield,clone_shield)]
        if not all(math.isfinite(v) and v >= 0 for v in values) or values[0] <= 0 or values[2] <= 0:
            raise ValueError('Invalid native hardware measurements')
        if values[1] != 0 or values[2] != values[3] or values[4] != values[5]:
            raise ValueError('Unused hardware or different native capacity')
        native[index] = {'source_code':source, 'clone_code':clone, 'input_macros':values[0]}
    if set(native) != wanted or log.count('LOADOUT_ROUNDTRIP complete count=8') != 1:
        raise ValueError('Incomplete native reconstruction/cleanup')
    descriptors={};identities={}
    for index,size,encoded in DESCRIPTOR.findall(log):
        index=int(index)
        if index not in wanted or index in descriptors or not 1<=int(size)<=MAX_BYTES or len(encoded)!=int(size)*2:
            raise ValueError('Incomplete/duplicate serialized descriptor')
        try:d=decode(bytes.fromhex(encoded).decode('ascii'))
        except (ValueError,UnicodeError) as error:raise ValueError('Invalid serialized native descriptor') from error
        if d['macro']!=rows['expected'][index][0]:raise ValueError('Serialized ship macro differs')
        hardware={f"{s['kind']},{s['index']+1},{s['macro']}" for s in d['slots']}
        hardware.update('software,'+s for s in d['software'])
        if d['thruster'] is not None:hardware.add('thruster,1,'+d['thruster'])
        physical={s for s in rows['expected'][index][1] if not s.endswith((',none',','))}
        if hardware!=physical:raise ValueError('Serialized physical equipment differs from native source')
        descriptors[index]=d
    for index,source_macro,clone_macro,source_owner,clone_owner in IDENTITY.findall(log):
        index=int(index)
        if (index not in wanted or index in identities or source_macro!=clone_macro
                or source_macro!=rows['expected'][index][0] or source_owner!=clone_owner
                or (index in descriptors and source_owner!=descriptors[index]['owner'])):
            raise ValueError('Native reconstructed macro/owner differs')
        identities[index]=(source_macro,source_owner)
    serialized=require_serialized or bool(descriptors) or 'LOADOUT_ROUNDTRIP serialized-mode=' in log
    if serialized and (log.count('LOADOUT_ROUNDTRIP serialized-mode=true')!=1 or set(descriptors)!=wanted or set(identities)!=wanted):
        raise ValueError('Eight serialized descriptors and native identities required')
    return {'ship_macros':8, 'exact_observed_equipment_slots_and_software':True,
            'native_hull_and_shield_capacity_equal':True,
            'inventory_crew_ammunition_modifications_not_tested':True,
            'network_creation_not_tested':True, 'observations':native,
            'serialized_descriptors_match_native_equipment_and_owner':bool(descriptors)}


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('log',type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--require-serialized',action='store_true')
    args=p.parse_args();result=validate(args.log.read_text(errors='replace'),args.require_serialized)
    text=json.dumps(result,indent=2)
    if args.output:args.output.write_text(text+'\n')
    print(text)
