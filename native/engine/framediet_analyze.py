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


def audit(path: Path) -> tuple:
    rows, last = {}, None
    for _t, body in records(path):
        if body.startswith('FRAMEDIET_AUDIT '):
            f = fields(body)
            rows[f.get('row', '?')] = body
        elif body.startswith('FRAMEDIET mode='):
            last = body
    return rows, last


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
    a = sub.add_parser('audit'); a.add_argument('log', type=Path)
    t = sub.add_parser('attribution'); t.add_argument('log', type=Path)
    t.add_argument('--window', action='append', default=[], help='LO:HI log-time range of window ends')
    args = parser.parse_args(argv)
    if args.cmd == 'phases':
        print(f'{"phase":24s} {"step":>5s} {"samples":>7s} {"frames":>7s} {"fps":>6s} {"ms/frame":>9s} {"p95":>6s}')
        for ph, st, n, frames, seconds, ms, p95 in phases(args.log, args.skip):
            fps = frames / seconds if seconds else 0.0
            print(f'{ph:24s} {st:>5s} {n:7d} {frames:7d} {fps:6.1f} {ms:9.3f} {p95:6.2f}')
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
