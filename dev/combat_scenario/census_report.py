"""Summarise a combat census table (x4mp_combat.dll census mode, x4mp-combatcensus.txt).

    python dev/combat_scenario/census_report.py <x4mp-combatcensus.txt> [--json]

Per-frame distributions of the counts, plus the derived figures the parallel-walk design needs:
the share of the thread's RNG steps made inside weapon and bullet updates, RNG steps per shot and per
flight check, FireShot calls per frame, hits per frame (hit-path locks or buffers), and the
fresh-bullet timestamp proxy. That proxy is not a unique-bullet or same-creation-frame count:
last-check equality can persist and shot time includes salvo delay/jitter. Never launches X4.
"""
import argparse
import json
import math
from pathlib import Path
import sys

MAGIC = 'X4MP-COMBATCENSUS 1'
REQUIRED_COLUMNS = set(('now weapon_calls with_target aiming fired shots missile_shots bullet_calls '
                        'bullet_checks bullet_hits bullet_fresh fresh_same_frame rng_weapon rng_bullet '
                        'rng_unmatched rng_frame weapon_ticks bullet_ticks frame_ticks').split())


def read(path: Path):
    lines = path.read_text(encoding='utf-8').splitlines()
    if not lines or lines[0].split()[:2] != MAGIC.split():
        raise ValueError(f'{path}: not a combat census table')
    if len(lines) < 3 or not lines[1].startswith('# '):
        raise ValueError(f'{path}: missing census metadata')
    meta = {}
    for item in lines[1].lstrip('# ').split():
        key, _, value = item.partition('=')
        try:
            meta[key] = float(value)
        except ValueError:
            meta[key] = value
    header = next((i for i, l in enumerate(lines) if l.startswith('now\t')), None)
    if header is None:
        raise ValueError(f'{path}: missing census columns')
    columns = lines[header].split('\t')
    if len(set(columns)) != len(columns) or not REQUIRED_COLUMNS <= set(columns):
        raise ValueError(f'{path}: missing or duplicate census columns')
    rows = []
    for line_number, line in enumerate(lines[header + 1:], header + 2):
        if not line.strip():
            continue
        values = line.split('\t')
        if len(values) != len(columns):
            raise ValueError(f'{path}:{line_number}: incomplete census row')
        row = dict(zip(columns, map(float, values)))
        if not all(math.isfinite(v) for v in row.values()):
            raise ValueError(f'{path}:{line_number}: non-finite census value')
        for name, value in row.items():
            if name != 'now' and (value != int(value) or (value < 0 and not (name == 'rng_frame' and value == -1))):
                raise ValueError(f'{path}:{line_number}: invalid count {name}={value}')
        rows.append(row)
    if meta.get('frames') != len(rows):
        raise ValueError(f'{path}: frame count disagrees with metadata (truncated capture?)')
    tps = meta.get('ticks_per_second')
    if not isinstance(tps, (int, float)) or not math.isfinite(tps) or tps <= 0:
        raise ValueError(f'{path}: invalid ticks_per_second')
    return meta, rows


def dist(values):
    v = sorted(values)
    if not v:
        return {'mean': 0.0, 'p50': 0.0, 'p95': 0.0, 'p99': 0.0, 'max': 0.0}
    at = lambda q: v[round((len(v) - 1) * q)]
    return {'mean': sum(v) / len(v), 'p50': at(0.5), 'p95': at(0.95), 'p99': at(0.99), 'max': v[-1]}


def summarise(meta, rows) -> dict:
    tps = meta.get('ticks_per_second') or 1.0
    total = lambda key: sum(r[key] for r in rows)
    # A call over CALL_CAP is stored as zero RNG steps plus rng_unmatched.
    # Including that frame would turn a missing numerator into a measured low share.
    frames_ok = [r for r in rows if r['rng_frame'] >= 0 and r['rng_unmatched'] == 0]
    calls_complete = all(r['rng_unmatched'] == 0 for r in rows)
    rng_all = sum(r['rng_frame'] for r in frames_ok)
    rng_combat = sum(r['rng_weapon'] + r['rng_bullet'] for r in frames_ok)
    shots, checks = total('shots'), total('bullet_checks')
    out = {
        'frames': len(rows),
        'meta': meta,
        'rng_complete_frames': len(frames_ok),
        'rng_excluded_frames': len(rows) - len(frames_ok),
        'rng_unmatched_calls': total('rng_unmatched'),
        'per_frame': {key: dist([r[key] for r in rows]) for key in (
            'weapon_calls', 'with_target', 'aiming', 'fired', 'shots', 'missile_shots', 'bullet_calls',
            'bullet_checks', 'bullet_hits', 'bullet_fresh', 'fresh_same_frame', 'rng_weapon', 'rng_bullet')},
        'rng_frame': dist([r['rng_frame'] for r in frames_ok]),
        'rng_combat_share': rng_combat / rng_all if rng_all else None,
        'rng_per_shot_upper': total('rng_weapon') / shots if shots and calls_complete else None,
        'rng_bullet_per_check': total('rng_bullet') / checks if checks and calls_complete else None,
        'fresh_same_frame_share': total('fresh_same_frame') / total('bullet_fresh') if total('bullet_fresh') else None,
        'weapon_ms': dist([r['weapon_ticks'] * 1000.0 / tps for r in rows]),
        'bullet_ms': dist([r['bullet_ticks'] * 1000.0 / tps for r in rows]),
        'frame_ms': dist([r['frame_ticks'] * 1000.0 / tps for r in rows]),
        'no_primary_target_share': (1.0 - total('with_target') / total('weapon_calls')
                                    if total('weapon_calls') and meta.get('primary_target_offset') == 16 else None),
    }
    return out


def text(s: dict) -> str:
    out = [f"frames {s['frames']}",
           f"rng complete_frames={s['rng_complete_frames']} excluded_frames={s['rng_excluded_frames']} "
           f"unmatched_calls={s['rng_unmatched_calls']:.0f}"]
    for key, d in s['per_frame'].items():
        out.append(f"{key:18} mean {d['mean']:9.2f}  p50 {d['p50']:8.0f}  p95 {d['p95']:8.0f}  p99 {d['p99']:8.0f}  max {d['max']:8.0f}")
    for key in ('weapon_ms', 'bullet_ms', 'frame_ms', 'rng_frame'):
        d = s[key]
        out.append(f"{key:18} mean {d['mean']:9.3f}  p50 {d['p50']:8.3f}  p95 {d['p95']:8.3f}  max {d['max']:8.3f}")
    for key in ('rng_combat_share', 'rng_per_shot_upper', 'rng_bullet_per_check', 'fresh_same_frame_share',
                'no_primary_target_share'):
        value = s[key]
        out.append(f"{key:24} {'n/a' if value is None else f'{value:.4f}'}")
    out.append('fresh counters classify Move calls by shot/check timestamps; they do not prove unique first moves or creation-frame order.')
    out.append('no_primary_target_share is not an idle-weapon measure; secondary targets and untargeted firing are not classified.')
    return '\n'.join(out) + '\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('table', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        meta, rows = read(args.table)
    except (OSError, ValueError, StopIteration) as error:
        print(f'REFUSED {error}', file=sys.stderr)
        return 3
    s = summarise(meta, rows)
    sys.stdout.write(json.dumps(s, indent=2) + '\n' if args.json else text(s))
    return 0


if __name__ == '__main__':
    sys.exit(main())
