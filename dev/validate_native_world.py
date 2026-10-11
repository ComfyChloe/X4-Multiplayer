"""Assert recorded engine evidence; synthetic peers are not two real players."""
from pathlib import Path
import json
import re
import statistics

ROOT=Path(__file__).resolve().parents[1]/'.local/native-world-tests'
host=json.loads((ROOT/'native-host-result.json').read_text())
guest=json.loads((ROOT/'native-guest-result.json').read_text())
log=(ROOT/'native-guest.log').read_text()
assert len(host['history'])>=8
assert all(b['objects']==len(b['rows']) and b['objects']>0 for b in host['history'])
assert host['known_positions']>0
pattern=r'W01_PROBE failed=(\S+) revision=(\S+) replicas=(\d+) live=(\d+) retiredLive=(\d+) maxError=([\d.]+) localPlayer=(\S+)'
samples=[dict(failed=m[0],revision=m[1],replicas=int(m[2]),live=int(m[3]),retired=int(m[4]),error=float(m[5]),player=m[6])
         for m in re.findall(pattern,log)]
assert samples and all(s['failed']=='nil' and s['player']=='true' for s in samples)
assert all(s['retired']==0 for s in samples), 'Retired replica is still operational in engine'
count=host['history'][-1]['objects']
full=[s for s in samples if s['replicas']==count and s['live']==count]
assert len(full)>=3 and max(s['error'] for s in full)<.02
assert '1:1800.0,10000.0,1200.0' in log and '1:3800.0,10000.0,1200.0' in log
assert samples[-1]['replicas']==0 and samples[-1]['live']==0
assert 'UNIVERSE STOPPED' not in log and 'Lua error' not in log
metrics={}
for role,r,slot in [('host',host,'p1'),('guest',guest,'p2')]:
    rates=[v['callbacks_hz'] for v in r['metrics'] if v['slot']==slot]
    metrics[role]={'callbacks_hz_min':min(rates),'callbacks_hz_median':statistics.median(rates),'callbacks_hz_max':max(rates)}
result=dict(status='PASS for W01 NPC scene milestone only',game='9.00 (611726)',steam_build='23660954',
    native_platform='Linux',guest_counterpart='synthetic Python authority',host_counterpart='synthetic Python receiver',
    host_baseline_counts=[b['objects'] for b in host['history']],host_live_position_count=host['known_positions'],
    guest_full_sector_objects=count,guest_full_sector_samples=len(full),guest_full_sector_max_position_error_m=max(s['error'] for s in full),
    native_removal_confirmed=True,local_player_operational_in_all_samples=True,metrics=metrics,
    engine_mass_traffic_warnings=log.count('Setting mass traffic offsets for an object that is no mass traffic'),
    limits=['Warnings remain during suppression of arriving native NPCs; broader job/mission consequences unverified.',
            'No native Windows X4 run or simultaneous two-computer test.',
            'No ordinary cargo, magnet pickup, NPC combat, full economy or save/load acceptance.',
            'Brief transition interpolation error is expected; position tolerance applies to settled full-sector samples.'])
(ROOT/'native-checks.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
