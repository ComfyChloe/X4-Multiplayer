"""Validate and summarise DAMAGE_PROBE lines from one X4 debug log (unshipped dev tool).

Usage: python dev/validate_damage_probe.py <x4lan-debug.txt> [--late 0.5] [--tolerance 0.5]

For each probe run it pairs every 'apply_requested' with its native 'apply_readback' and
reports requests that are missing, late (readback delay above --late seconds) or mismatched
(observed hull/shield differs from the request by more than --tolerance percentage points),
plus unmatched or invalid readbacks. Health changes seen on 'tick' lines between a confirmed
readback and the next request are listed as CANDIDATE local drift only: the probe cannot prove
their cause. The exit status is 1 when any completion is missing, invalid or unmatched.
"""
import argparse
import json
import re
import sys

LINE = re.compile(r'\[X4LAN\] DAMAGE_PROBE (run=\S+ .*)$')
FIELD = re.compile(r'(\w+)=(\[[^\]]*\]|\S+)')
PAIR = re.compile(r'(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)')


def fields(text):
    return {k: v for k, v in FIELD.findall(text)}


def health(value):
    m = PAIR.fullmatch(value or '')
    return (float(m.group(1)), float(m.group(2))) if m else None


def parse(lines):
    rows = []
    for raw in lines:
        m = LINE.search(raw)
        if not m:
            continue
        f = fields(m.group(1))
        detail = re.search(r' detail=(.*?) paused=', m.group(1))
        f['detail_text'] = detail.group(1) if detail else ''
        rows.append(f)
    return rows


def summarise(rows, late=.5, tolerance=.5):
    runs = {}
    for row in rows:
        run = runs.setdefault(row.get('run', '?'), {
            'requested': {}, 'confirmed': 0, 'missing': [], 'late': [], 'mismatched': [],
            'unmatched': 0, 'invalid': 0, 'candidate_drift': [], 'last_confirmed': None})
        reason, detail = row.get('reason'), row['detail_text']
        if reason == 'apply_requested':
            apply = re.search(r'apply=(\d+)', detail)
            if apply:
                run['requested'][int(apply.group(1))] = row
                run['last_confirmed'] = None
        elif reason == 'apply_readback':
            apply = re.search(r'apply=(\d+)', detail)
            delay = re.search(r'delay=(-?\d+(?:\.\d+)?)', detail)
            req = re.search(r'requested\[ship1=(\S+) ship2=(\S+)\]', detail)
            obs = re.search(r'observed\[ship1=(\S+) ship2=(\S+)\]', detail)
            if not (apply and delay and req and obs):
                run['invalid'] += 1
                continue
            ident = int(apply.group(1))
            run['requested'].pop(ident, None)
            run['confirmed'] += 1
            if float(delay.group(1)) > late:
                run['late'].append(ident)
            observed = [health(obs.group(1)), health(obs.group(2))]
            wanted = [health(req.group(1)), health(req.group(2))]
            if any(o is None or w is None or max(abs(o[0] - w[0]), abs(o[1] - w[1])) > tolerance
                   for o, w in zip(observed, wanted)):
                run['mismatched'].append(ident)
            run['last_confirmed'] = (ident, observed)
        elif reason == 'apply_missing':
            apply = re.search(r'apply=(\d+)', detail)
            if apply:
                run['requested'].pop(int(apply.group(1)), None)
                run['missing'].append(int(apply.group(1)))
        elif reason == 'apply_readback_unmatched':
            run['unmatched'] += 1
        elif reason == 'apply_readback_invalid':
            run['invalid'] += 1
        elif reason == 'tick' and run['last_confirmed']:
            ident, observed = run['last_confirmed']
            now = [health(row.get('ship1')), health(row.get('ship2'))]
            for ship, (o, n) in enumerate(zip(observed, now), 1):
                if o and n and max(abs(o[0] - n[0]), abs(o[1] - n[1])) > tolerance:
                    run['candidate_drift'].append({'after_apply': ident, 'ship': ship, 'rt': row.get('rt'),
                                                   'confirmed': o, 'observed': n})
    result = {}
    for name, run in runs.items():
        # Requests still open at the end of the log never received a readback.
        unresolved = sorted(run['requested'])
        result[name] = {'confirmed': run['confirmed'], 'missing': run['missing'] + unresolved,
                        'late': run['late'], 'mismatched': run['mismatched'], 'unmatched': run['unmatched'],
                        'invalid': run['invalid'], 'candidate_drift': run['candidate_drift']}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('log')
    parser.add_argument('--late', type=float, default=.5)
    parser.add_argument('--tolerance', type=float, default=.5)
    args = parser.parse_args(argv)
    with open(args.log, encoding='utf-8', errors='replace') as handle:
        result = summarise(parse(handle), args.late, args.tolerance)
    print(json.dumps(result, indent=2))
    bad = any(r['missing'] or r['unmatched'] or r['invalid'] for r in result.values())
    return 1 if bad or not result else 0


if __name__ == '__main__':
    sys.exit(main())
