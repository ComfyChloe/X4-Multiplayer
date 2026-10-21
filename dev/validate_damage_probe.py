"""Validate and summarise DAMAGE_PROBE lines from one X4 debug log (unshipped dev tool).

Usage: python dev/validate_damage_probe.py <x4lan-debug.txt> [--late 0.5] [--tolerance 0.5]

For each probe run it pairs every 'apply_requested' with its native 'apply_readback' and
reports requests that are missing, late (readback delay above --late seconds) or mismatched
(observed hull/shield differs from the request by more than --tolerance percentage points),
plus unmatched, invalid, echo-mismatched or orphan readbacks (a readback whose request line
is absent, e.g. in a truncated log, never counts as confirmed; pairing is by request number)
and request/readback lines without a probe frame number ('frameless').
Every observed mismatch stays listed in 'raw_mismatched'. An earlier request of a multi-request
frame whose readback shows that frame's LAST request, while the last request's own readback
matches it, moves from 'mismatched' to 'later_in_frame' ("consistent with a later request in
the same recorded frame"); this is a classification, not proof of the engine's update order.
Batches (frames with two or more requests) list the readback frames and the MD-side game-clock
markers (md_seen: MD update that handled the Apply; md_read: MD update of the readback).
Health changes seen on 'tick' lines between a confirmed readback and the next request are
listed as CANDIDATE local drift only: the probe cannot prove their cause. The exit status is 1
when any completion is missing, invalid, unmatched, echo-mismatched, orphan or frameless, or
when the log contains no probe lines at all.
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
            'unmatched': 0, 'invalid': 0, 'echo_mismatch': [], 'orphan': [], 'candidate_drift': [], 'last_confirmed': None,
            'frameless': [], 'frames': {}, 'frame_of': {}, 'readback_frame': {}, 'observed': {}, 'wanted': {},
            'md': {}, 'paused': {}, 'readback_paused': {}})
        reason, detail = row.get('reason'), row['detail_text']
        frame = row.get('frame') if re.fullmatch(r'\d+', row.get('frame') or '') else None
        if reason == 'apply_requested':
            apply = re.search(r'apply=(\d+)', detail)
            if apply:
                ident = int(apply.group(1))
                run['requested'][ident] = row
                run['last_confirmed'] = None
                run['paused'][ident] = row.get('paused')
                if frame is None:
                    # A request without a frame number can never be grouped with other requests.
                    run['frameless'].append(ident)
                    continue
                # Requests logged in the same probe frame were queued in one Lua update.
                run['frames'].setdefault(frame, []).append(ident)
                run['frame_of'][ident] = frame
        elif reason == 'apply_readback':
            apply = re.search(r'apply=(\d+)', detail)
            delay = re.search(r'delay=(-?\d+(?:\.\d+)?)', detail)
            req = re.search(r'requested\[ship1=(\S+) ship2=(\S+)\]', detail)
            obs = re.search(r'observed\[ship1=(\S+) ship2=(\S+)\]', detail)
            if not (apply and delay and req and obs):
                run['invalid'] += 1
                continue
            ident = int(apply.group(1))
            if frame is None:
                run['frameless'].append(ident)
            if ident not in run['requested']:
                # A readback without its request (truncated log, duplicate or foreign line)
                # must never count as a confirmed completion.
                run['orphan'].append(ident)
                continue
            saved = run['requested'].pop(ident)
            # Confirm only when the readback's echo equals the saved request: same health
            # for both ships and the same WORLD revision. A same-ID echo that differs is not
            # a completion of that request.
            saved_detail = saved['detail_text']
            saved_req = re.search(r'requested\[ship1=(\S+) ship2=(\S+)', saved_detail)
            saved_rev = re.search(r'wrev=(\S+)', saved_detail)
            echo_rev = re.search(r'wrev=(\S+)', detail)
            if (not saved_req or not saved_rev or not echo_rev or saved_rev.group(1) != echo_rev.group(1) or
                    any(health(a) != health(b) or health(a) is None
                        for a, b in zip(saved_req.groups(), req.groups()))):
                run['echo_mismatch'].append(ident)
                run['last_confirmed'] = None
                continue
            run['confirmed'] += 1
            if float(delay.group(1)) > late:
                run['late'].append(ident)
            observed = [health(obs.group(1)), health(obs.group(2))]
            wanted = [health(req.group(1)), health(req.group(2))]
            run['observed'][ident], run['wanted'][ident] = observed, wanted
            run['readback_frame'][ident] = frame
            run['readback_paused'][ident] = row.get('paused')
            md = re.search(r' md=(\d+)/(\d+)', detail)
            run['md'][ident] = md.groups() if md else (None, None)
            if not close(observed, wanted, tolerance):
                run['mismatched'].append(ident)
            run['last_confirmed'] = (ident, observed)
        elif reason == 'apply_missing':
            apply = re.search(r'apply=(\d+)', detail)
            if apply:
                run['requested'].pop(int(apply.group(1)), None)
                run['missing'].append(int(apply.group(1)))
        elif reason == 'apply_readback_echo_mismatch':
            apply = re.search(r'apply=(\d+)', detail)
            if apply:
                run['requested'].pop(int(apply.group(1)), None)
                run['echo_mismatch'].append(int(apply.group(1)))
            run['last_confirmed'] = None
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
        # An earlier request of a multi-request frame whose readback shows exactly that frame's
        # LAST request is "consistent with a later request in the same recorded frame", but only
        # while the last request's own readback matches it. Every raw mismatch stays listed.
        later_in_frame, mismatched = [], []
        for ident in run['mismatched']:
            ids = run['frames'].get(run['frame_of'].get(ident), [])
            last = ids[-1] if ids else None
            if (len(ids) >= 2 and ident != last and last in run['wanted'] and last not in run['mismatched'] and
                    close(run['observed'][ident], run['wanted'][last], tolerance)):
                later_in_frame.append(ident)
            else:
                mismatched.append(ident)
        batches = []
        for frame, ids in run['frames'].items():
            if len(ids) >= 2:
                values = [tuple(run['wanted'].get(i) or ()) for i in ids]
                markers = [run['md'].get(i, (None, None)) for i in ids]
                batches.append({'frame': frame, 'requests': ids,
                                'confirmed': all(i in run['wanted'] for i in ids),
                                # The MD game clock is frozen while paused: both the request and
                                # its readback must be logged unpaused (unknown counts as paused).
                                'unpaused': all(run['paused'].get(i) == 'false' and
                                                run['readback_paused'].get(i) == 'false' for i in ids),
                                'readback_frames': sorted({str(run['readback_frame'].get(i)) for i in ids}),
                                'md_seen': sorted({str(m[0]) for m in markers}),
                                'md_read': sorted({str(m[1]) for m in markers}),
                                'identical_values': len(values) != len(set(values)),
                                'distinct_values': len(set(values)) >= 2})
        result[name] = {'confirmed': run['confirmed'], 'missing': run['missing'] + unresolved,
                        'late': run['late'], 'raw_mismatched': run['mismatched'], 'mismatched': mismatched,
                        'later_in_frame': later_in_frame,
                        'unmatched': run['unmatched'], 'invalid': run['invalid'],
                        'echo_mismatch': run['echo_mismatch'], 'orphan': run['orphan'], 'frameless': run['frameless'],
                        'batches': batches, 'candidate_drift': run['candidate_drift']}
    return result


def close(observed, wanted, tolerance):
    return all(o is not None and w is not None and max(abs(o[0] - w[0]), abs(o[1] - w[1])) <= tolerance
               for o, w in zip(observed, wanted))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('log')
    parser.add_argument('--late', type=float, default=.5)
    parser.add_argument('--tolerance', type=float, default=.5)
    args = parser.parse_args(argv)
    with open(args.log, encoding='utf-8', errors='replace') as handle:
        result = summarise(parse(handle), args.late, args.tolerance)
    print(json.dumps(result, indent=2))
    bad = any(r['missing'] or r['unmatched'] or r['invalid'] or r['echo_mismatch'] or r['orphan'] or r['frameless']
              for r in result.values())
    return 1 if bad or not result else 0


if __name__ == '__main__':
    sys.exit(main())
