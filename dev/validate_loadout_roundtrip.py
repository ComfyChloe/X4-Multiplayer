"""Compare native source/clone equipment observations; no game mutation."""
import argparse
import json
import math
from pathlib import Path
import re

ROW = re.compile(r'LOADOUT_ROUNDTRIP (expected|observed) id=(\d+) macro=(\S+) rows=(\S*)')
NATIVE = re.compile(r'LOADOUT_ROUNDTRIP_NATIVE id=(\d+) source=(\S+) clone=(\S+) input=(\S+) unused=(\S+) sourceHull=(\S+) cloneHull=(\S+) sourceShield=(\S+) cloneShield=(\S+)')


def validate(log):
    if (any(v in log for v in ('Lua error', 'Error in MD cue md.X4LAN', 'LOADOUT_ROUNDTRIP failed', 'UNIVERSE STOPPED'))
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
    return {'ship_macros':8, 'exact_observed_equipment_slots_and_software':True,
            'native_hull_and_shield_capacity_equal':True,
            'inventory_crew_ammunition_modifications_not_tested':True,
            'network_creation_not_tested':True, 'observations':native}


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('log',type=Path);p.add_argument('--output',type=Path)
    args=p.parse_args();result=validate(args.log.read_text(errors='replace'))
    text=json.dumps(result,indent=2)
    if args.output:args.output.write_text(text+'\n')
    print(text)
