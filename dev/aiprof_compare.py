"""Compare the AIPROF census of two lane runs (ai-driver): e.g. our AIDirector::Move in normal mode
against the original (census mode) on the same load. Prints per-frame means side by side with the
relative difference and flags structural measures that differ by more than the tolerance.
usage: python aiprof_compare.py <debug-A> <debug-B> [--skip-a N] [--skip-b N] [--tol 0.15]
Timing columns (ms, share) are shown but never flagged: lane runs share the PC."""
import re
import statistics as st
import sys

KV = re.compile(r'(\w+)=(-?\d+\.?\d*)')
STRUCTURAL = ['runs', 'irqRuns', 'passes', 'extWakes', 'timedDue', 'wakeOwn', 'wakeForeign',
              'evOwn', 'evForeign', 'evCue', 'hazard', 'keyless', 'sectors']
TIMING = ['ms', 'share', 'secMaxMs', 'speedup']


def windows(path, skip):
    rows = []
    for line in open(path, encoding='utf-8', errors='replace'):
        if 'AIPROF mode=' in line and ' frames=' in line:
            rows.append({k: float(v) for k, v in KV.findall(line.split('AIPROF', 1)[1])})
    return rows[skip:]


def arg(name, default):
    if name in sys.argv:
        return type(default)(sys.argv[sys.argv.index(name) + 1])
    return default


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith('--') and not a.replace('.', '').isdigit()]
    if len(paths) != 2:
        print(__doc__)
        return 2
    a, b = windows(paths[0], arg('--skip-a', 0)), windows(paths[1], arg('--skip-b', 0))
    tol = arg('--tol', 0.15)
    if not a or not b:
        print('no AIPROF windows in one of the logs')
        return 1
    print(f'A: {paths[0]} ({len(a)} windows)\nB: {paths[1]} ({len(b)} windows)')
    print(f'{"measure":14s} {"A mean":>10s} {"B mean":>10s} {"B/A-1":>8s}')
    flagged = 0
    for key in STRUCTURAL + TIMING:
        va, vb = [r[key] for r in a if key in r], [r[key] for r in b if key in r]
        if not va or not vb:
            continue
        ma, mb = st.mean(va), st.mean(vb)
        rel = (mb / ma - 1.0) if ma else (0.0 if not mb else float('inf'))
        flag = ''
        if key in STRUCTURAL and abs(rel) > tol and max(ma, mb) > 0.05:
            flag = '  <-- differs'
            flagged += 1
        print(f'{key:14s} {ma:10.3f} {mb:10.3f} {rel:+8.1%}{flag}')
    # population-normalized ratios: stable between runs of one load when the drivers are equivalent
    def ratio(rows, num, den):
        n = sum(r.get(num, 0.0) for r in rows)
        d = sum(r.get(den, 0.0) for r in rows) if isinstance(den, str) else sum(sum(r.get(k, 0.0) for k in den) for r in rows)
        return n / d if d else 0.0
    print('ratios (population-normalized):')
    for label, num, den in [('irq per run', 'irqRuns', 'runs'), ('timed due per run', 'timedDue', 'runs'),
                            ('ext wakes per run', 'extWakes', 'runs'), ('cue events per run', 'evCue', 'runs'),
                            ('foreign wake fraction', 'wakeForeign', ('wakeOwn', 'wakeForeign')),
                            ('runs per sector', 'runs', 'sectors')]:
        ra, rb = ratio(a, num, den), ratio(b, num, den)
        rel = (rb / ra - 1.0) if ra else 0.0
        flag = '  <-- differs' if abs(rel) > tol else ''
        flagged += bool(flag)
        print(f'  {label:22s} {ra:9.3f} {rb:9.3f} {rel:+8.1%}{flag}')
    print(f'{flagged} measure(s) outside +-{tol:.0%} '
          '(loads are not identical between runs: read flags as leads, not verdicts)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
