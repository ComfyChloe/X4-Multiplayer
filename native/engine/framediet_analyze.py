"""Frame-diet evidence from X4 debug logs (offline; reads logs only).

  python native/engine/framediet_analyze.py phases LOG [--skip 1]
      MoveWorker cost per FRAMEDIET phase (and ATTNLOAD step) from the fixed-tick probe's FRAMEPROBE
      lines: slot-loop ms per frame (simFrames-weighted simMeanMs), frames per second, p95 of the
      per-second p95. The first --skip FRAMEPROBE lines after each phase/step change are dropped.
  python native/engine/framediet_analyze.py audit LOG
      The last FRAMEDIET_AUDIT row per region and the last FRAMEDIET line.
  python native/engine/framediet_analyze.py attribution LOG [--window LO:HI ...] [--x4re DIR]
      Presentation versus Lua attribution of slots 6 and 10 from simprof per-window folds
      (SIMPROF window/fold lines): ms per MoveWorker frame for the functions the diet targets.
Lines may carry the X4 debug-log prefix (`[=ERROR=] <time> `); <time> orders them.
"""
import argparse
import collections
import re
import sys
from pathlib import Path

LINE = re.compile(r'^(?:\[[^\]]*\]\s+)?(\d+(?:\.\d+)?)\s+\[X4LAN\]\s+(.*)$')
KV = re.compile(r'(\w+)=(\S*)')


def records(path: Path):
    with path.open(encoding='utf-8', errors='replace') as source:
        for raw in source:
            m = LINE.match(raw.rstrip('\n'))
            if m:
                yield float(m.group(1)), m.group(2)


def fields(body: str) -> dict:
    return {k: v for k, v in KV.findall(body)}


def number(d: dict, key: str, default=0.0) -> float:
    try:
        return float(d.get(key, default))
    except ValueError:
        return default


def phases(path: Path, skip: int = 1) -> list:
    """[(phase, step, samples, frames, seconds, mean_ms, p95_ms)] in first-seen order."""
    phase, step, since_change = 'off', '-', 0
    acc = collections.OrderedDict()
    last_t = None
    for t, body in records(path):
        if body.startswith('FRAMEDIET phase='):
            phase, since_change = fields(body)['phase'], 0
        elif body.startswith('FRAMEDIET armed'):
            phase, since_change = fields(body).get('mode', 'diet'), 0
        elif body.startswith('ATTNLOAD step='):
            step, since_change = fields(body)['step'], 0
        elif body.startswith('FRAMEPROBE frames='):
            f = fields(body)
            since_change += 1
            sim_frames = number(f, 'simFrames')
            if since_change <= skip or sim_frames <= 0:
                last_t = t
                continue
            key = (phase, step)
            a = acc.setdefault(key, {'samples': 0, 'frames': 0.0, 'ms': 0.0, 'p95': [], 'seconds': 0.0})
            a['samples'] += 1
            a['frames'] += sim_frames
            a['ms'] += number(f, 'simMeanMs') * sim_frames
            a['p95'].append(number(f, 'simP95Ms'))
            if last_t is not None and t > last_t:
                a['seconds'] += t - last_t
            last_t = t
    out = []
    for (ph, st), a in acc.items():
        p95 = sorted(a['p95'])
        out.append((ph, st, a['samples'], int(a['frames']), a['seconds'],
                    a['ms'] / a['frames'] if a['frames'] else 0.0,
                    p95[min(len(p95) - 1, int(0.95 * len(p95)))] if p95 else 0.0))
    return out


def compare(rows: list, reference: str) -> list:
    """Per step: each phase's MoveWorker ms/frame and fps against the reference phase."""
    out = []
    by_step = collections.OrderedDict()
    for ph, st, n, frames, seconds, ms, p95 in rows:
        by_step.setdefault(st, {})[ph] = (ms, frames / seconds if seconds else 0.0, n)
    for st, phases_ in by_step.items():
        if reference not in phases_:
            continue
        ref_ms, ref_fps, _ = phases_[reference]
        for ph, (ms, fps, n) in phases_.items():
            if ph == reference:
                continue
            saved = ref_ms - ms
            out.append(f'step {st}: {ph} vs {reference}: {ms:.3f} vs {ref_ms:.3f} ms/frame, saved {saved:.3f} ms '
                       f'({saved / ref_ms * 100 if ref_ms else 0:.1f} %), fps {fps:.1f} vs {ref_fps:.1f} (n={n})')
    return out


def audit(path: Path) -> tuple:
    rows, last = {}, None
    for _t, body in records(path):
        if body.startswith('FRAMEDIET_AUDIT '):
            f = fields(body)
            rows[f.get('row', '?')] = body
        elif body.startswith('FRAMEDIET mode='):
            last = body
    return rows, last


COUNTERS = ('audited', 'rngCalls', 'rngSteps', 'rngUnknown', 'idCalls', 'ids', 'eventCalls', 'eventsUp',
            'digests', 'components', 'changed', 'splitCalls', 'splitRenderSteps', 'frames', 't',
            'mainCpuMs', 'workerCpuMs', 'processCpuMs', 'scenesSkipped', 'cameraSkipped', 'elementsSkipped')


def audit_phases(path: Path) -> list:
    """Per-phase deltas of the cumulative audit counters: [(phase, {row: {counter: delta}})].
    The loader logs a report right before every phase switch, so the last value seen before a
    `FRAMEDIET phase=` line closes that phase."""
    current, phase, out = {}, 'start', []
    base = {}

    def close(name):
        delta = {}
        for row, values in current.items():
            before = base.get(row, {})
            delta[row] = {k: v - before.get(k, 0.0) for k, v in values.items()}
        out.append((name, delta))
        base.clear()
        base.update({row: dict(values) for row, values in current.items()})

    for _t, body in records(path):
        if body.startswith('FRAMEDIET phase='):
            close(phase)
            phase = fields(body)['phase']
            continue
        if body.startswith('FRAMEDIET_AUDIT '):
            row = 'audit:' + fields(body).get('row', '?')
        elif body.startswith('FRAMEDIET_FRAMES '):
            row = 'frames'
        elif body.startswith('FRAMEDIET mode='):
            row = 'stats'
        else:
            continue
        f = fields(body)
        current[row] = {k: number(f, k) for k in COUNTERS if k in f}
    close(phase)
    return out[1:] if out and out[0][0] == 'start' and not any(out[0][1].values()) else out


ERROR_LINE = re.compile(r'(?i)lua error|\berror\b|exception|assert')


def stability(path: Path) -> dict:
    """Diet stability evidence: FRAMEDIET report cadence (onUpdate alive), frames, commit size per phase,
    FEATURE ... DISABLED lines and error lines that are not X4LAN/X4LANE output."""
    reports, phase, commit = [], 'start', collections.OrderedDict()
    features, errors, frames = [], collections.Counter(), []
    with path.open(encoding='utf-8', errors='replace') as source:
        for raw in source:
            line = raw.rstrip('\n')
            m = LINE.match(line)
            body = m.group(2) if m else ''
            if m and body.startswith('FRAMEDIET phase='):
                phase = fields(body)['phase']
            elif m and body.startswith('FRAMEDIET mode='):
                f = fields(body)
                reports.append(number(f, 't'))
                frames.append(number(f, 'frames'))
                if 'commitMb' in f:
                    lo, hi = commit.get(phase, (float('inf'), 0.0))
                    commit[phase] = (min(lo, number(f, 'commitMb')), max(hi, number(f, 'commitMb')))
            elif m and 'FEATURE' in body and 'DISABLED' in body:
                features.append(body)
            elif '[X4LAN]' not in line and '[X4LANE]' not in line and ERROR_LINE.search(line):
                errors[re.sub(r'\d+(\.\d+)?', '#', line)[:160]] += 1
    gaps = [b - a for a, b in zip(reports, reports[1:])]
    return {'reports': len(reports), 'max_gap_s': max(gaps) if gaps else 0.0,
            'first_t': reports[0] if reports else 0.0, 'last_t': reports[-1] if reports else 0.0,
            'frames_first': frames[0] if frames else 0, 'frames_last': frames[-1] if frames else 0,
            'commit_mb': dict(commit), 'features': features, 'errors': dict(errors)}


def load_names(x4re: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from dev.simprof_report import load_labels
    return load_labels(x4re)


# Functions the diet targets or keeps, for the attribution table (RVA, short name).
ATTRIBUTION = [
    ('b3bad0', 'slot 6 UI controller total (CUIController::Move)'),
    ('lua', 'slot 6 Lua (any lua51_64 frame; the mod runs here)'),
    ('1399c40', 'Anark gameface element update (no Lua)'),
    ('af8e50', 'Anark scene manager -> scene layer walk (ui_scenes)'),
    ('ae9880', 'UI ICom message pump / VisibilityData'),
    ('121c1f0', 'slot 10 render preparation total (GameRender::Move)'),
    ('983160', 'camera render (U::Camera slot 3; camera site)'),
    ('b3bba0', 'UI render bank (CUIController::Render; ui_render site)'),
    ('3d4a80', 'Component::BuildCache (slot 3)'),
]


def attribution(path: Path, windows: list) -> list:
    """[(label, [(ms_per_frame, share) per window])] from per-window SIMPROF folds."""
    per = []  # per window: Counter of stack -> samples, frames
    cur = None
    probe = []
    for t, body in records(path):
        if body.startswith('SIMPROF window '):
            cur = {'t': t, 'folds': collections.Counter()}
            per.append(cur)
        elif body.startswith('SIMPROF fold ') and cur is not None:
            stack, count = body[len('SIMPROF fold '):].rsplit(' ', 1)
            cur['folds'][stack] += int(count)
        elif body.startswith('FRAMEPROBE frames='):
            probe.append((t, number(fields(body), 'simFrames')))
    result = []
    for lo, hi in windows:
        folds, frames = collections.Counter(), 0.0
        for w in per:
            if lo < w['t'] <= hi:
                folds.update(w['folds'])
                frames += sum(n for t, n in probe if w['t'] - 10.0 < t <= w['t'])
        total = sum(folds.values())
        row = []
        for rva, _label in ATTRIBUTION:
            n = 0
            for stack, c in folds.items():
                fr = stack.split(';')
                if rva == 'lua':
                    if fr[0] == 'slot6' and any(f.startswith('lua51_64') for f in fr):
                        n += c
                elif rva in fr:
                    n += c
            row.append((n / frames if frames else 0.0, n / total if total else 0.0))
        result.append((lo, hi, total, frames, row))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('phases'); p.add_argument('log', type=Path); p.add_argument('--skip', type=int, default=1)
    p.add_argument('--reference', default='pass', help='phase the others are compared with, per step')
    a = sub.add_parser('audit'); a.add_argument('log', type=Path)
    a.add_argument('--phases', action='store_true', help='per-phase deltas of the cumulative counters')
    st = sub.add_parser('stability'); st.add_argument('log', type=Path)
    t = sub.add_parser('attribution'); t.add_argument('log', type=Path)
    t.add_argument('--window', action='append', default=[], help='LO:HI log-time range of window ends')
    args = parser.parse_args(argv)
    if args.cmd == 'phases':
        print(f'{"phase":24s} {"step":>5s} {"samples":>7s} {"frames":>7s} {"fps":>6s} {"ms/frame":>9s} {"p95":>6s}')
        rows = phases(args.log, args.skip)
        for ph, st, n, frames, seconds, ms, p95 in rows:
            fps = frames / seconds if seconds else 0.0
            print(f'{ph:24s} {st:>5s} {n:7d} {frames:7d} {fps:6.1f} {ms:9.3f} {p95:6.2f}')
        for line in compare(rows, args.reference):
            print(line)
    elif args.cmd == 'stability':
        r = stability(args.log)
        print(f"FRAMEDIET reports: {r['reports']} from t={r['first_t']:.1f} to {r['last_t']:.1f} s, "
              f"largest gap {r['max_gap_s']:.1f} s; frames {r['frames_first']:.0f} -> {r['frames_last']:.0f}")
        for ph, (lo, hi) in r['commit_mb'].items():
            print(f'  commit MB in {ph}: {lo:.1f} .. {hi:.1f}')
        print(f"  FEATURE DISABLED lines: {len(r['features'])}")
        for line in r['features'][:5]:
            print('   ', line)
        print(f"  error-like lines outside X4LAN: {sum(r['errors'].values())}")
        for line, n in sorted(r['errors'].items(), key=lambda kv: -kv[1])[:10]:
            print(f'    {n:4d} {line}')
    elif args.cmd == 'audit' and args.phases:
        for name, delta in audit_phases(args.log):
            print(f'== phase {name}')
            for row, values in sorted(delta.items()):
                shown = ' '.join(f'{k}={v:g}' for k, v in values.items() if v)
                print(f'   {row:18s} {shown or "-"}')
            stats = delta.get('stats', {})
            if stats.get('t', 0) > 0:
                wall = stats['t'] * 1000.0
                print(f'   cpu per wall: main {stats.get("mainCpuMs", 0) / wall * 100:.1f} %, MoveWorker '
                      f'{stats.get("workerCpuMs", 0) / wall * 100:.1f} %, process {stats.get("processCpuMs", 0) / wall * 100:.1f} %')
    elif args.cmd == 'audit':
        rows, last = audit(args.log)
        for name in ('ui_scenes', 'ui_render', 'camera', 'control'):
            if name in rows:
                print(rows[name])
        if last:
            print(last)
    else:
        windows = [tuple(float(x) for x in w.split(':')) for w in args.window] or [(0.0, float('inf'))]
        for lo, hi, total, frames, row in attribution(args.log, windows):
            print(f'windows ({lo:g},{hi:g}]: {total} samples, {frames:.0f} frames, {total / frames if frames else 0:.2f} ms/frame')
            for (rva, label), (ms, share) in zip(ATTRIBUTION, row):
                print(f'  {ms:7.3f} ms  {share * 100:5.1f}%  {label} [{rva}]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
