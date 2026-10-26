#!/usr/bin/env python3
"""Stage durations of one X4 load from two copies of `<profile>/progressbar.xml`.

X4 keeps a running average per load type (`newgame`, `savedgame`, ...) with a load
count `loaded=N` and one value per stage. Copy the file before a load and after it,
then:

    python dev/progressbar_delta.py before.xml after.xml [--measured 37.2]

Rule (same as the agent's LOADSTAGES line, rust/x4mp-agent/src/loadstages.rs):

* cumulative mean (expected): avg' = (N*avg + x)/(N+1)  ->  x = (N+1)*avg' - N*avg
* any running average avg' = avg + w*(x - avg)  ->  x = avg + (avg' - avg)/w

With the load's wall time T measured independently (stopwatch, the agent's
`load_s=`), the weight is w = (S' - S)/(T - S) for the stage sums S, S'. A cumulative
mean predicts w = 1/(N+1). If the two disagree the tool also prints the stages for
the implied weight, and only that reading is trustworthy. Without T and without a
count increase only the averages are available.

`--dump file.xml` prints how a file was parsed (to check the layout assumption).
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass

VALUE_KEYS = ('time', 'duration', 'value', 'avg', 'average', 'seconds', 's', 't', 'v')
NAME_KEYS = ('id', 'name', 'type')
SKIP = {'id', 'name', 'type', 'loaded'}


@dataclass
class LoadType:
    name: str
    loaded: int
    stages: list[tuple[str, float]]


def _num(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _label(el) -> str:
    return next((el.get(k) for k in NAME_KEYS if el.get(k)), el.tag)


def _value(el):
    for k in VALUE_KEYS:
        v = _num(el.get(k))
        if v is not None:
            return v
    nums = [_num(v) for k, v in el.attrib.items() if k not in SKIP]
    nums = [v for v in nums if v is not None]
    if len(nums) == 1:
        return nums[0]
    return _num(el.text)


def parse(text: str) -> list[LoadType]:
    if not text.strip():
        return []
    root = ET.fromstring(text)
    out = []
    for el in root.iter():
        loaded = el.get('loaded')
        if loaded is None or not loaded.strip().isdigit():
            continue
        stages = [(k, v) for k, v in ((k, _num(v)) for k, v in el.attrib.items() if k not in SKIP) if v is not None]
        for child in el:
            v = _value(child)
            if v is not None:
                stages.append((_label(child), v))
        out.append(LoadType(_label(el), int(loaded), stages))
    return out


@dataclass
class Delta:
    name: str
    before_n: int
    after_n: int
    stages: list[tuple[str, float, float]]  # name, avg before, avg after

    @property
    def loads(self):
        return self.after_n - self.before_n if self.after_n > self.before_n else None

    def cumulative(self):
        if not self.loads:
            return None
        n0, n1 = self.before_n, self.after_n
        return [(s, (n1 * a1 - n0 * a0) / self.loads) for s, a0, a1 in self.stages]

    def with_weight(self, w: float):
        return [(s, a0 + (a1 - a0) / w) for s, a0, a1 in self.stages]

    def implied_weight(self, measured: float):
        s0 = sum(a0 for _, a0, _ in self.stages)
        s1 = sum(a1 for _, _, a1 in self.stages)
        denom = measured - s0
        if abs(denom) < 1e-9:
            return None
        w = (s1 - s0) / denom
        return w if w > 0 else None


def diff(before: list[LoadType], after: list[LoadType]) -> list[Delta]:
    out = []
    for a in after:
        b = next((b for b in before if b.name == a.name and b.loaded <= a.loaded), None)
        if b is not None and b.loaded == a.loaded and b.stages == a.stages:
            continue
        prev = dict(b.stages) if b else {}
        out.append(Delta(a.name, b.loaded if b else 0, a.loaded, [(s, prev.get(s, 0.0), v) for s, v in a.stages]))
    return out


def report(d: Delta, measured: float | None) -> str:
    lines = [f'{d.name}: loaded {d.before_n} -> {d.after_n}']
    cum = d.cumulative()
    w = d.implied_weight(measured) if measured is not None else None
    expected = 1.0 / d.after_n if d.loads == 1 else None
    width = max([len(s) for s, _, _ in d.stages] + [5])
    head = f'  {"stage":<{width}} {"avg before":>10} {"avg after":>10} {"this load":>10}'
    show_w = w is not None and (expected is None or abs(w - expected) / expected > 0.25)
    if show_w:
        head += f' {"at w=%.3f" % w:>10}'
    lines.append(head)
    alt = dict(d.with_weight(w)) if show_w else {}
    for i, (s, a0, a1) in enumerate(d.stages):
        x = f'{cum[i][1]:10.3f}' if cum else f'{"?":>10}'
        row = f'  {s:<{width}} {a0:10.3f} {a1:10.3f} {x}'
        if show_w:
            row += f' {alt[s]:10.3f}'
        lines.append(row)
    if cum:
        lines.append(f'  sum (cumulative rule): {sum(v for _, v in cum):.3f}'
                     + (f'  [{d.loads} loads: mean of the new loads]' if d.loads > 1 else ''))
    else:
        lines.append('  count did not increase: the single load cannot be derived without --measured')
    if measured is not None:
        lines.append(f'  measured: {measured:.3f}; implied weight: {"none" if w is None else f"{w:.4f}"}'
                     + (f'; cumulative predicts {expected:.4f}' if expected else ''))
        if w is not None and expected:
            verdict = 'agrees: cumulative mean' if abs(w - expected) / expected <= 0.25 else 'DISAGREES: use the w column'
            lines.append(f'  rule check: {verdict}')
    return '\n'.join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('before', nargs='?')
    p.add_argument('after', nargs='?')
    p.add_argument('--measured', type=float, help='wall time of the load in seconds, measured independently')
    p.add_argument('--dump', help='print how one file is parsed and exit')
    a = p.parse_args(argv)
    if a.dump:
        for t in parse(Path(a.dump).read_text(encoding='utf-8')):
            print(f'{t.name} loaded={t.loaded} sum={sum(v for _, v in t.stages):.3f}: '
                  + ', '.join(f'{s}={v:g}' for s, v in t.stages))
        return 0
    if not (a.before and a.after):
        p.error('give BEFORE and AFTER (or --dump FILE)')
    def read(path):
        return parse(Path(path).read_text(encoding='utf-8')) if path != '-' else []
    deltas = diff(read(a.before), read(a.after))
    if not deltas:
        print('no load type changed between the two files')
        return 1
    print('\n\n'.join(report(d, a.measured) for d in deltas))
    return 0


if __name__ == '__main__':
    sys.exit(main())
