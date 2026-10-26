"""Read-only pre/post-frame and native shot orientation evidence, not an aim fix.

Install angular_probe plus projectile_probe with production orientation mode.
The original projectile gates remain unchanged. This report accepts observation
coverage separately, even when that trajectory experiment fails orientation.
"""
import argparse
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess

from dev.native_projectile_test import HERE, ROOT, analyze as projectiles, run as projectile_run

BASIS = dict(fx=0, fy=0, fz=1, ux=0, uy=1, uz=0)
ANGLES = ('Yaw', 'Pitch', 'Roll')
PHASES = ('stationary', 'rendered', 'physical', 'native')


def analyze(log):
    frames, native, shots = {}, {}, {}
    malformed = 0
    for line in log.splitlines():
        if 'ANGULAR_PROBE' not in line:
            continue
        # Native DebugError lines begin with [=ERROR=]. That engine prefix is
        # not a diagnostic field (ERROR=]) and must not enter numeric parsing.
        values = dict(re.findall(r'(\w+)=([^\s]+)', line.split('ANGULAR_PROBE', 1)[1]))
        kind = next((k for k in ('shot', 'native', 'frame') if k in values), None)
        if kind is None:
            malformed += 1
            continue
        try:
            phase = values.pop('phase', None)
            row = {k: float(v) for k, v in values.items()}
            if not all(math.isfinite(v) for v in row.values()):
                raise ValueError('nonfinite')
            key = row[kind]
            if key != int(key) or key < 1:
                raise ValueError('serial')
            required = (('elapsed', 'beforeTime', 'afterTime') + tuple(p+a for p in ('before', 'after') for a in ANGLES)
                        if kind == 'frame' else ('time',) + tuple(BASIS) + (('uiTime',) if kind == 'native' else ('frame',)))
            if not all(k in row for k in required) or kind != 'shot' and phase not in PHASES:
                raise ValueError('missing field')
            row['phase'] = phase
            target = dict(frame=frames, native=native, shot=shots)[kind]
            if key in target:
                raise ValueError('duplicate')
            target[int(key)] = row
        except (ValueError, KeyError):
            malformed += 1
    trajectory = projectiles(log)
    errors = [line for line in log.splitlines() if 'angularprobe' in line.lower().replace('_', '')
              and any(s in line.lower() for s in ('error:', 'error in', 'failed', 'not found'))]
    result = dict(frames=len(frames), native_samples=len(native), shots=len(shots),
                  malformed=malformed, errors=errors, phases={}, observation_complete=False,
                  projectile_measurement_complete=trajectory['measurement_complete'],
                  damage_or_aim_acceptance=False, two_native_acceptance=False,
                  angular_velocity_getter_used=False,
                  rate_meaning='Wrapped Euler drift divided by elapsed game time between prior post-frame and current pre-frame samples; not a native angular-velocity getter.',
                  shot_frame_meaning='Last UI frame processed by this MD observer, not necessarily the latest rendered frame.')
    for index, phase in enumerate(PHASES):
        settled = [r for r in frames.values() if r['phase'] == phase and 5 <= r['elapsed']-12*index <= 8]
        readbacks = [native.get(int(r['frame'])) for r in settled]
        firing = [r for r in shots.values() if frames.get(r['frame'], {}).get('phase') == phase]
        rates = []
        for row in settled:
            previous = frames.get(int(row['frame'])-1)
            if not previous or previous['phase'] != phase:
                continue
            dt = row['beforeTime']-previous['afterTime']
            if .001 <= dt <= .25:
                rates.append(max(abs((row['before'+a]-previous['after'+a]+math.pi) % (2*math.pi)-math.pi)/dt for a in ANGLES))
        complete = (len(settled) >= 30 and len(rates) >= 20 and len(firing) == 6 and all(readbacks)
                    and all(r['afterTime'] >= r['beforeTime'] for r in settled))
        p = dict(settled_frames=len(settled), derived_rate_intervals=len(rates), native_readbacks=sum(r is not None for r in readbacks), shots=len(firing), valid=False)
        if complete:
            p.update(max_before_angle=max(abs(r['before'+a]) for r in settled for a in ANGLES),
                     max_after_angle=max(abs(r['after'+a]) for r in settled for a in ANGLES),
                     median_derived_rate=statistics.median(rates), max_derived_rate=max(rates),
                     max_native_basis_error=max(abs(r[k]-v) for r in readbacks for k, v in BASIS.items()),
                     max_shot_basis_error=max(abs(r[k]-v) for r in firing for k, v in BASIS.items()),
                     min_native_delay=min(n['time']-f['afterTime'] for f, n in zip(settled, readbacks)),
                     max_native_delay=max(n['time']-f['afterTime'] for f, n in zip(settled, readbacks)),
                     max_shot_delay=max(r['time']-frames[r['frame']]['afterTime'] for r in firing))
            p['valid'] = all(n['phase'] == phase and abs(n['uiTime']-f['afterTime']) < .001
                             and -.001 <= n['time']-f['afterTime'] <= .5 for f, n in zip(settled, readbacks))
            for shot in firing:
                reference = trajectory['native_readbacks'].get(int(shot['shot']))
                p['valid'] &= bool(reference and reference['phase'] == phase and abs(reference['gameTime']-shot['time']) < .001)
                if reference:
                    p['valid'] &= all(abs(shot[k]-reference[v]) < .00001 for k, v in
                                      zip(BASIS, ('forwardX', 'forwardY', 'forwardZ', 'upX', 'upY', 'upZ')))
                p['valid'] &= -.001 <= shot['time']-frames[shot['frame']]['afterTime'] <= .5
        result['phases'][phase] = p
    result['observation_complete'] = (not errors and malformed == 0 and len(shots) == 24
        and trajectory['projectile_samples_complete'] and trajectory['orientation_control'] == 'native_only'
        and all(p['valid'] for p in result['phases'].values()))
    return result


def run():
    try:
        projectile_run()
    except SystemExit:
        # A failed original projectile gate stays failed in its own report.
        pass
    log_path = HERE/'projectile-native-host.log'
    result = analyze(log_path.read_text(errors='replace'))
    result.update(source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  production_tree=subprocess.check_output(['git', 'rev-parse', 'HEAD:src/extension/x4_lan_lab'], cwd=ROOT, text=True).strip())
    shutil.copy2(log_path, HERE/'angular-native-host.log')
    (HERE/'angular-native-host-result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2), flush=True)
    if not result['observation_complete']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--analyze', type=Path)
    args = parser.parse_args()
    if args.analyze:
        print(json.dumps(analyze(args.analyze.read_text(errors='replace')), indent=2))
    else:
        run()
