"""Symbolize simprof output from an X4 debug log (or a folded file) into top lists and a
FlameGraph-compatible folded file.

Input lines (anywhere in the file, e.g. after the debug-log prefix):
  SIMPROF fold <phase>;<frame>;...;<leaf> <count>     one folded stack, root first
  SIMPROF window key=value ...                       per-window counters (overhead summary)
  FEATURE simprof DISABLED reason=...
Plain folded lines ("a;b;c 12") are accepted too. Frames that are bare lowercase hex are X4.exe
function-start RVAs; they are named from the x4-re label files (labels/merged.tsv first, then the
other labels/*.tsv, then symbols.csv), else fn_<rva>. Other frames (ntdll!..., module+offset,
[ext], [truncated]) pass through unchanged. The first frame is the phase: slot0..slot16,
frame (MoveWorker::Run outside a slot), idle (Run waiting for the next frame), loading, lost
(stack did not reach Run inside the slot loop) or lostout.
Never prints executable bytes; only RVAs and names.
"""
import argparse
import collections
import csv
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r'^[0-9a-f]+$')
FOLD = re.compile(r'SIMPROF fold (\S.*) (\d+)\s*$')
WINDOW = re.compile(r'SIMPROF window (.*)$')
DISABLED = re.compile(r'FEATURE simprof DISABLED reason=(.*)$')
PLAIN = re.compile(r'^([^\s;][^;]*(?:;[^;]+)*) (\d+)\s*$')
WAIT_PREFIX = 'ntdll!Nt'


def default_x4re() -> Path:
    env = os.environ.get('X4RE_DIR')
    if env:
        return Path(env)
    return ROOT.parent / 'x4-re'


def _clean(name: str) -> str:
    return name.replace(';', ':').strip()


def load_labels(x4re: Path) -> dict[int, str]:
    """RVA -> name. Earlier sources win: merged.tsv, other TSVs, linux-xfer.tsv, symbols.csv."""
    names: dict[int, str] = {}
    labels = x4re / 'labels'

    def add_tsv(path: Path):
        with path.open(encoding='utf-8', errors='replace') as source:
            for line in source:
                if not line.strip() or line.startswith('#'):
                    continue
                fields = line.rstrip('\n').split('\t')
                if len(fields) < 3 or fields[2] != 'function':
                    continue
                try:
                    rva = int(fields[0], 16)
                except ValueError:
                    continue
                if fields[1] and rva not in names:
                    names[rva] = _clean(fields[1])

    if labels.is_dir():
        others = [p for p in sorted(labels.glob('*.tsv'))
                  if p.name not in ('merged.tsv', 'conflicts.tsv', 'linux-xfer.tsv')]
        for path in [labels / 'merged.tsv', *others, labels / 'linux-xfer.tsv']:
            if path.exists():
                add_tsv(path)
    symbols = x4re / 'symbols.csv'
    if symbols.exists():
        with symbols.open(encoding='utf-8', errors='replace', newline='') as source:
            for row in csv.DictReader(source):
                if row.get('kind') != 'function' or not row.get('name'):
                    continue
                try:
                    rva = int(row['rva'], 16)
                except (KeyError, ValueError):
                    continue
                names.setdefault(rva, _clean(row['name']))
    return names


def parse(paths) -> tuple[collections.Counter, list[dict], list[str]]:
    stacks: collections.Counter = collections.Counter()
    windows: list[dict] = []
    disabled: list[str] = []
    for path in paths:
        with open(path, encoding='utf-8', errors='replace') as source:
            for line in source:
                line = line.rstrip('\r\n')
                match = FOLD.search(line)
                if match:
                    stacks[match.group(1)] += int(match.group(2))
                    continue
                match = WINDOW.search(line)
                if match:
                    fields = {}
                    for item in match.group(1).split():
                        key, _, value = item.partition('=')
                        try:
                            fields[key] = float(value)
                        except ValueError:
                            pass
                    windows.append(fields)
                    continue
                match = DISABLED.search(line)
                if match:
                    disabled.append(match.group(1))
                    continue
                if 'SIMPROF' in line or 'FEATURE' in line:
                    continue
                match = PLAIN.match(line)
                if match:
                    stacks[match.group(1)] += int(match.group(2))
    return stacks, windows, disabled


def symbolizer(names: dict[int, str], with_rva: bool):
    cache: dict[str, str] = {}

    def name(frame: str) -> str:
        if frame in cache:
            return cache[frame]
        if HEX.match(frame):
            rva = int(frame, 16)
            label = names.get(rva)
            result = f'{label}@{rva:x}' if label and with_rva else (label or f'fn_{rva:x}')
        else:
            result = frame
        cache[frame] = result
        return result
    return name


def analyse(stacks: collections.Counter, name, phase_filter=None):
    total = 0
    phases: collections.Counter = collections.Counter()
    phase_wait: collections.Counter = collections.Counter()
    self_counts: collections.Counter = collections.Counter()
    incl_counts: collections.Counter = collections.Counter()
    folded: collections.Counter = collections.Counter()
    for stack, count in stacks.items():
        frames = stack.split(';')
        phase, body = frames[0], frames[1:]
        if phase_filter and phase != phase_filter:
            continue
        total += count
        phases[phase] += count
        if body and body[-1].startswith(WAIT_PREFIX):
            phase_wait[phase] += count
        named = [name(f) for f in body]
        folded[';'.join([phase] + named)] += count
        if named:
            self_counts[named[-1]] += count
        for frame in set(named):
            incl_counts[frame] += count
    return total, phases, phase_wait, self_counts, incl_counts, folded


def percent(count: int, total: int) -> str:
    return f'{100.0 * count / total:5.1f}%' if total else '  0.0%'


def report(stacks, windows, disabled, name, top: int, phase_filter=None, per_phase: int = 0) -> str:
    total, phases, phase_wait, self_counts, incl_counts, _ = analyse(stacks, name, phase_filter)
    out = []
    title = f'simprof report: {total} samples' + (f' in phase {phase_filter}' if phase_filter else '')
    out.append(title)
    if windows:
        def mean(key):
            values = [w[key] for w in windows if key in w]
            return sum(values) / len(values) if values else 0.0
        seconds = sum(w.get('seconds', 0.0) for w in windows)
        lost = sum(w.get('lost', 0.0) for w in windows)
        samples = sum(w.get('samples', 0.0) for w in windows)
        out.append(f'windows={len(windows)} seconds={seconds:.1f} rateHz={mean("rateHz"):.0f} '
                   f'overheadPct(mean)={mean("overheadPct"):.2f} suspendUs(mean)={mean("suspendUsMean"):.1f} '
                   f'walkUs(mean)={mean("walkUsMean"):.1f} depth(mean)={mean("depthMean"):.1f} '
                   f'lost={percent(int(lost), int(samples)).strip()} '
                   f'droppedStacks={int(sum(w.get("droppedStacks", 0.0) for w in windows))}')
    for reason in disabled:
        out.append(f'DISABLED: {reason}')
    out.append('')
    out.append('phase      share   waiting')
    for phase, count in sorted(phases.items(), key=lambda item: (-item[1], item[0])):
        out.append(f'{phase:<9} {percent(count, total)}  {percent(phase_wait[phase], total)}')
    for label, counts in (('self', self_counts), ('inclusive', incl_counts)):
        out.append('')
        out.append(f'top {top} by {label} samples')
        for frame, count in counts.most_common(top):
            out.append(f'{percent(count, total)}  {count:8d}  {frame}')
    if per_phase and not phase_filter:
        for phase, _ in sorted(phases.items(), key=lambda item: -item[1]):
            sub = analyse(stacks, name, phase)
            out.append('')
            out.append(f'[{phase}] top {per_phase} self (share of all samples)')
            for frame, count in sub[3].most_common(per_phase):
                out.append(f'{percent(count, total)}  {count:8d}  {frame}')
    return '\n'.join(out) + '\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('inputs', nargs='+', type=Path, help='x4lan debug log(s) or folded file(s)')
    parser.add_argument('--x4re', type=Path, default=default_x4re(), help='x4-re directory (labels/, symbols.csv)')
    parser.add_argument('--folded', type=Path, help='write symbolized FlameGraph folded stacks here')
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--phase', help='restrict to one phase, e.g. slot3')
    parser.add_argument('--per-phase', type=int, default=5, metavar='N', help='top N self per phase (0: off)')
    parser.add_argument('--with-rva', action='store_true', help='append @rva to named X4 frames')
    args = parser.parse_args(argv)
    stacks, windows, disabled = parse(args.inputs)
    if not stacks:
        print('no SIMPROF fold lines found', file=sys.stderr)
        return 1
    names = load_labels(args.x4re) if args.x4re.is_dir() else {}
    if not names:
        print(f'warning: no labels loaded from {args.x4re}; frames stay fn_<rva>', file=sys.stderr)
    name = symbolizer(names, args.with_rva)
    sys.stdout.write(report(stacks, windows, disabled, name, args.top, args.phase, args.per_phase))
    if args.folded:
        folded = analyse(stacks, name, args.phase)[5]
        with args.folded.open('w', encoding='utf-8', newline='\n') as target:
            for stack, count in sorted(folded.items()):
                target.write(f'{stack} {count}\n')
        print(f'folded stacks: {args.folded} ({len(folded)} unique)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
