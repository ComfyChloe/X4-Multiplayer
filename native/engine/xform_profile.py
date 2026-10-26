"""Attribute view-cache build (BuildCache) and transform costs in a simprof debug log.

    python native/engine/xform_profile.py <x4lan-debug.txt> [--phase NAME=FIRST-LAST ...]

Reads `SIMPROF fold` lines (each window's stacks are logged after its `SIMPROF window` line)
and `FRAMEPROBE simFrames=` lines, groups windows into phases by their end time in seconds
(default: base=40-81, eight=110-262, matching the 8-sector attention harness run of
25 Oct 2026), and prints per phase, in milliseconds per simulation frame:

- BuildCache 0x3d4a80 inclusive/self, and the caller of its outermost frame;
- its recursion depth histogram (1 = galaxy ... 5 = objects in zones), self vs refresh;
- direct callees of PropagateCacheFlags 0x3d46b0 and of the innermost BuildCache;
- the transform helpers (GetLocalTransform 0x7461c0, GetTransformRelativeTo 0x3db8a0,
  GetDistanceSqTo 0x3e0c40, Positional::vfunc693 0x751770, Scene::vfunc915 0x779a00,
  Camera::vfunc3 0x983160) by context (slot 3 BuildCache / walk / AI / events / MD, slot 6,
  slot 10 camera path).
Samples are 1 ms wall-time samples of the MoveWorker, so samples/frames = ms per frame.
"""
import argparse
import bisect
import collections
import re

FOLD = re.compile(r'^\S+ ([\d.]+) \[X4LAN\] SIMPROF fold (\S.*) (\d+)\s*$')
WINDOW = re.compile(r'^\S+ ([\d.]+) \[X4LAN\] SIMPROF window ')
PROBE = re.compile(r'^\S+ ([\d.]+) \[X4LAN\] FRAMEPROBE .*simFrames=(\d+)')
BUILD, PCF = '3d4a80', '3d46b0'
HOT = {
    '3d4a80': 'BuildCache', '3d46b0': 'PropagateCacheFlags', '7461c0': 'GetLocalTransform',
    '3db8a0': 'GetTransformRelativeTo', '3e0c40': 'GetDistanceSqTo', '751770': 'Positional::vfunc693',
    '779a00': 'Scene::vfunc915', '983160': 'Camera::vfunc3', '7463a0': 'GetLocalVelocity',
    '745920': 'Positional::vfunc622',
}


def context(frames):
    s = set(frames)
    if frames[0] == 'slot3':
        for key, name in ((BUILD, 'BuildCache'), ('748120', 'walk'), ('106d250', 'AIDirector'),
                          ('99bb50', 'events'), ('1121f80', 'MD')):
            if key in s:
                return 'slot3/' + name
        return 'slot3/other'
    if frames[0] == 'slot10':
        return 'slot10/camera' if '983160' in s else 'slot10/other'
    return frames[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('log')
    ap.add_argument('--phase', action='append', default=[], help='NAME=FIRST-LAST window end times in s')
    args = ap.parse_args()
    phases = {}
    for p in args.phase or ['base=40-81', 'eight=110-262']:
        name, _, rng = p.partition('=')
        lo, _, hi = rng.partition('-')
        phases[name] = (float(lo), float(hi))
    windows, stacks, probes = [], collections.defaultdict(collections.Counter), []
    end = 0.0
    with open(args.log, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = WINDOW.match(line)
            if m:
                end = float(m.group(1))
                windows.append(end)
                continue
            m = FOLD.match(line)
            if m:
                stacks[end][m.group(2)] += int(m.group(3))
                continue
            m = PROBE.match(line)
            if m:
                probes.append((float(m.group(1)), int(m.group(2))))
    frames = collections.Counter()
    for t, n in probes:
        i = bisect.bisect_left(windows, t)
        if i < len(windows):
            frames[windows[i]] += n
    for name, (lo, hi) in phases.items():
        ws = [w for w in windows if lo <= w <= hi]
        nf = sum(frames[w] for w in ws) or 1
        total = sum(sum(stacks[w].values()) for w in ws)
        callers, depth, pcf_callees, bc_callees = (collections.Counter() for _ in range(4))
        incl, self_ = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
        for w in ws:
            for stack, c in stacks[w].items():
                fr = stack.split(';')
                ctx = context(fr)
                for h in HOT:
                    if h in fr:
                        incl[h][ctx] += c
                if fr[-1] in HOT:
                    self_[fr[-1]][ctx] += c
                if BUILD in fr:
                    callers[';'.join(fr[:fr.index(BUILD)])] += c
                    last = max(i for i, x in enumerate(fr) if x == BUILD)
                    tail = fr[last + 1:]
                    kind = 'self' if not tail else ('refresh' if tail[0] == PCF else 'other')
                    depth[(fr.count(BUILD), kind)] += c
                    bc_callees[tail[0] if tail else '(self)'] += c
                if PCF in fr:
                    i = fr.index(PCF)
                    pcf_callees[fr[i + 1] if i + 1 < len(fr) else '(self)'] += c
        print(f'== {name}: windows {lo:g}-{hi:g} s, {nf} frames, {total / nf:.2f} ms/frame sampled')
        for k, c in callers.most_common(5):
            print(f'   BuildCache caller {k}: {c / nf:.3f} ms/frame')
        print('   depth  self  refresh  other   (ms/frame)')
        for d in sorted({k[0] for k in depth}):
            print(f'   {d:5d} {depth[(d, "self")] / nf:5.3f} {depth[(d, "refresh")] / nf:8.3f} {depth[(d, "other")] / nf:6.3f}')
        print('   PropagateCacheFlags callees: ' + ', '.join(f'{k} {c / nf:.3f}' for k, c in pcf_callees.most_common(6)))
        print('   BuildCache callees: ' + ', '.join(f'{k} {c / nf:.3f}' for k, c in bc_callees.most_common(4)))
        for h, label in HOT.items():
            ti, ts = sum(incl[h].values()), sum(self_[h].values())
            if not ti:
                continue
            parts = ', '.join(f'{ctx} {c / nf:.3f}' for ctx, c in incl[h].most_common(5))
            print(f'   {label}@{h}: incl {ti / nf:.3f} self {ts / nf:.3f} ms/frame; by context: {parts}')


if __name__ == '__main__':
    main()
