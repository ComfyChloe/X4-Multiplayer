"""Reject partial/visual-only seed binding evidence; no game mutation."""
import argparse
import json
import math
from pathlib import Path
import re

SUMMARY = re.compile(r'SEED_OBJECT_PROBE summary slot=(p[12]) revision=(\d+) count=(\d+) live=(\d+) matching=(\d+) retained=(\d+) maxError=([\d.e+-]+) failed=(\S+)')
OBJECT = re.compile(r'SEED_OBJECT_PROBE object slot=(p[12]) revision=(\d+) id=(\d+) handle=(\d+) code=(\S+) macro=(\S+) owner=(\S+) original=(\S+) sameHandle=(\S+) pose=([^\s]+)')
SPEC = re.compile(r'SEED_OBJECT_SPEC slot=(p[12]) revision=(\d+) id=(\d+) code=(\S+) macro=(\S+) owner=(\S+) maxhull=(\S+) maxshield=(\S+) weapons=(\S+) shields=(\S+) engines=(\S+) pilot=(\S+)')


def validate(host, guest, capture, replay):
    if (any(term in host + guest for term in ('STOPPED', 'Error in MD cue md.X4LAN', 'Lua error'))
            or re.search(r'extensions[/\\]x4_lan_lab[/\\](?:md|ui)[/\\][^\r\n]+\(\d+\):',host+guest)):
        raise ValueError('Native adapter/MD error')
    rows = {int(row.split(',')[0]):row.split(',') for row in capture['rows']}
    if not rows or len(rows) != len(capture['rows']):
        raise ValueError('Missing or duplicate host rows')
    identities = capture['identities']
    if set(identities) != {str(id) for id in rows}:
        raise ValueError('Incomplete host identity capture')
    omitted=[]
    if replay.get('seed_only'):
        omitted=sorted(id for id in rows if id>=250000)
        if sorted(replay.get('omitted_host_created_ids',[]))!=omitted:
            raise ValueError('Unexplained omitted host objects')
        rows={id:row for id,row in rows.items() if id<250000}
        identities={id:row for id,row in identities.items() if int(id)<250000}
        if not rows: raise ValueError('No saved-reference objects exercised')
    # Display codes may collide. The saved reference index is the shared key.
    phases = {entry['phase']:entry['revision'] for entry in replay['history'] if 'phase' in entry}
    if set(phases) != {'initial', 'absent', 'return'} or not phases['initial'] < phases['absent'] < phases['return']:
        raise ValueError('Missing or unordered exercised phases')
    handles = {}; covered = {'initial':set(), 'return':set()}
    final_errors = {'initial':{}, 'return':{}}
    def phase(rev):
        return 'return' if rev >= phases['return'] else 'absent' if rev >= phases['absent'] else 'initial'
    for m in OBJECT.finditer(guest):
        slot, rev, id, handle, code, macro, owner, original, same, pose = m.groups()
        rev, id = int(rev), int(id)
        if slot != 'p2' or id not in rows or original != 'true' or same != 'true':
            raise ValueError('Non-original, changed or unexpected native object')
        if code != identities[str(id)]['code'] or [macro,owner] != rows[id][2:4]:
            raise ValueError('Native identity/descriptor differs from host')
        coords = [float(n) for n in pose.split(',')]
        if len(coords) != 3 or not all(math.isfinite(n) for n in coords):
            raise ValueError('Invalid native position')
        if id in handles and handles[id] != handle:
            raise ValueError('Original component replaced across views')
        handles[id] = handle
        p = phase(rev)
        error = max(abs(coords[i]-float(rows[id][4+i])) for i in range(3))
        if p in covered and error <= 1: covered[p].add(id)
        if p in final_errors: final_errors[p][id]=error
    if any(found != set(rows) for found in covered.values()) or len(set(handles.values())) != len(rows):
        raise ValueError('Not all original components observed before and after view release')
    if any(set(errors)!=set(rows) or max(errors.values())>1 for errors in final_errors.values()):
        raise ValueError('Original positions diverge again before the end of an observed phase')
    settled = set(); retained = False; final_summaries={}
    for m in SUMMARY.finditer(guest):
        slot, rev, count, live, matching, kept, error, failed = m.groups()
        rev, count, live, matching, kept = map(int, (rev,count,live,matching,kept))
        error = float(error)
        if slot != 'p2' or failed != 'nil' or not math.isfinite(error):
            raise ValueError('Failed/invalid native summary')
        p = phase(rev)
        if p in covered: final_summaries[p]=(count,live,matching,error)
        if p in covered and count == live == matching == len(rows) and error <= 1:
            settled.add(p)
        if p == 'absent' and count == live == matching == 0 and kept == len(rows): retained = True
    if settled != {'initial', 'return'} or not retained:
        raise ValueError('Missing complete settled views or retained originals during absence')
    if any(value[:3]!=(len(rows),)*3 or value[3]>1 for value in final_summaries.values()):
        raise ValueError('Final observed phase summary is incomplete or divergent')
    specs = {}
    for text, role in ((host,'p1'),(guest,'p2')):
        by_id = {}; spec_phases = {'initial':set(),'return':set()}
        for m in SPEC.finditer(text):
            slot, rev, id, code, *values = m.groups(); id = int(id)
            if slot != role or id not in rows: continue
            if code != identities[str(id)]['code'] or values[:2] != rows[id][2:4]:
                raise ValueError('Native equipment identity differs')
            for value in values[2:7]:
                if not math.isfinite(float(value)) or float(value) < 0:
                    raise ValueError('Invalid native equipment readback')
            if values[7] not in ('true','false','1','0'):
                raise ValueError('Missing native pilot readback')
            values[7] = values[7] in ('true','1')
            for i in range(2,7): values[i] = float(values[i])
            value = tuple(values)
            if role == 'p2' and id in by_id and by_id[id] != value:
                raise ValueError('Guest equipment changed across views')
            by_id[id] = value
            current = phase(int(rev))
            if role == 'p2' and current in spec_phases: spec_phases[current].add(id)
        if set(by_id) != set(rows): raise ValueError('Incomplete native equipment readbacks')
        if role == 'p2' and any(ids != set(rows) for ids in spec_phases.values()):
            raise ValueError('Equipment not observed in both native views')
        specs[role] = by_id
    if specs['p1'] != specs['p2']:
        raise ValueError('Native equipment/pilot state differs between loaded copies')
    return dict(original_ships=len(rows), native_identity_and_equipment_equal=True,
                original_handles_retained_across_view_absence=True, settled_pose_error_limit_m=1,
                final_observed_pose_within_limit=True,
                explicitly_omitted_host_created_ids=omitted, seed_only=bool(replay.get('seed_only')),
                synthetic_counterparts=True, two_native_acceptance=False, ongoing_universe_authority=False)


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args()
    result=validate((a.run/'play-p1/native.log').read_text(errors='replace'),
                    (a.run/'play-p2/native.log').read_text(errors='replace'),
                    json.loads((a.run/'host-objects.json').read_text()),
                    json.loads((a.run/'guest-objects.json').read_text()))
    (a.run/'objects-result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
