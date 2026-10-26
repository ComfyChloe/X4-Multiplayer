"""Read COMBATSCN markers from an X4 debug log and profile the scenario's measurement window.

    python dev/combat_scenario/markers.py summary <x4lan-debug.txt>        JSON summary; exit 0 when done
    python dev/combat_scenario/markers.py profile <x4lan-debug.txt> [--folded out.folded] [--x4re DIR] [--open-end]

`profile` keeps only the simprof report windows that lie completely inside the scenario's
measurement window (log order: the window before must close after "COMBATSCN window begin",
the window itself before "COMBATSCN window end") and the fold lines drained after each of
them, then prints the normal dev/simprof_report.py report for that subset. Never launches X4.
"""
import argparse
import collections
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'dev'))

MARKER = re.compile(r'COMBATSCN (\w+)((?: [a-z]+(?= |$))?)((?: [A-Za-z_]+=\S*)*)')
KINDS = {'loaded', 'refused', 'armed', 'spawn', 'spawned', 'engaged', 'tick', 'window', 'done', 'cleaned'}
FOLD = re.compile(r'SIMPROF fold (\S.*) (\d+)\s*$')
WINDOW = re.compile(r'SIMPROF window (.*)$')


@dataclass
class Marker:
    kind: str
    sub: str
    fields: dict = field(default_factory=dict)
    line: int = 0


def number(value: str):
    """int or float for numeric marker values; MD may append a unit (45.2s, 120m)."""
    text = value[:-1] if value[-1:] in ('s', 'm') else value
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return value


def parse_markers(lines) -> list[Marker]:
    markers = []
    for index, line in enumerate(lines):
        match = MARKER.search(line)
        if not match or match.group(1) not in KINDS:
            continue
        fields = {}
        for item in match.group(3).split():
            key, _, value = item.partition('=')
            fields[key] = number(value)
        markers.append(Marker(match.group(1), match.group(2).strip(), fields, index))
    return markers


def summary(markers: list[Marker]) -> dict:
    out = dict(loaded=None, refused=None, armed=None, spawn_begin=None, spawned={}, spawn_complete=None,
               engaged=None, window_begin=None, window_end=None, done=None, cleaned=None, ticks=[])
    for marker in markers:
        f = dict(marker.fields)
        if marker.kind == 'spawned':
            out['spawned'][f.get('side', '?')] = f
        elif marker.kind == 'tick':
            out['ticks'].append(f)
        elif marker.kind == 'window':
            out['window_' + (marker.sub or 'begin')] = f
        elif marker.kind == 'spawn':
            out['spawn_' + (marker.sub or 'begin')] = f
        else:
            out[marker.kind] = f
    window = out['window_begin'], out['window_end']
    if all(window) and isinstance(window[0].get('t'), (int, float)) and isinstance(window[1].get('t'), (int, float)):
        out['window_seconds'] = round(window[1]['t'] - window[0]['t'], 3)
    out['complete'] = bool(out['done'] and not out['refused'] and all(window))
    return out


def window_lines(markers: list[Marker]):
    begin = next((m.line for m in markers if m.kind == 'window' and m.sub == 'begin'), None)
    end = next((m.line for m in markers if m.kind == 'window' and m.sub == 'end'), None)
    return begin, end


def combat_profile(lines: list[str], open_end: bool = False):
    """(stacks, window dicts) of the simprof windows fully inside the measurement window.
    open_end: a run that stopped before "window end" counts up to the end of the log."""
    begin, end = window_lines(parse_markers(lines))
    if end is None and open_end:
        end = len(lines)
    if begin is None or end is None:
        return collections.Counter(), []
    windows = []          # (line, fields)
    folds = collections.defaultdict(collections.Counter)   # window index -> stacks
    current = -1
    for index, line in enumerate(lines):
        match = WINDOW.search(line)
        if match:
            fields = {}
            for item in match.group(1).split():
                key, _, value = item.partition('=')
                try:
                    fields[key] = float(value)
                except ValueError:
                    pass
            windows.append((index, fields))
            current = len(windows) - 1
            continue
        match = FOLD.search(line)
        if match and current >= 0:
            folds[current][match.group(1)] += int(match.group(2))
    stacks: collections.Counter = collections.Counter()
    kept = []
    for k in range(1, len(windows)):
        if windows[k - 1][0] > begin and windows[k][0] < end:
            stacks.update(folds[k])
            kept.append(windows[k][1])
    return stacks, kept


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('summary', 'profile'))
    parser.add_argument('log', type=Path)
    parser.add_argument('--folded', type=Path)
    parser.add_argument('--x4re', type=Path)
    parser.add_argument('--top', type=int, default=25)
    parser.add_argument('--open-end', action='store_true',
                        help='profile: without "window end" (X4 stopped early) use the rest of the log')
    args = parser.parse_args(argv)
    lines = args.log.read_text(encoding='utf-8', errors='replace').splitlines()
    if args.action == 'summary':
        result = summary(parse_markers(lines))
        print(json.dumps(result, indent=2))
        return 0 if result['complete'] else 1
    import simprof_report
    stacks, windows = combat_profile(lines, args.open_end)
    if not stacks:
        print('no complete simprof window inside the COMBATSCN measurement window', file=sys.stderr)
        return 1
    x4re = args.x4re or simprof_report.default_x4re()
    names = simprof_report.load_labels(x4re) if x4re.is_dir() else {}
    name = simprof_report.symbolizer(names, True)
    sys.stdout.write(simprof_report.report(stacks, windows, [], name, args.top, None, 5))
    if args.folded:
        folded = simprof_report.analyse(stacks, name)[5]
        with args.folded.open('w', encoding='utf-8', newline='\n') as target:
            for stack, count in sorted(folded.items()):
                target.write(f'{stack} {count}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
