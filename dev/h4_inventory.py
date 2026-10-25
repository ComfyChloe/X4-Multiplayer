"""H4 inventory: list every literal two-player (p1/p2, entity 1/2) assumption outside tests/.

Read-only. Usage: python dev/h4_inventory.py [ROOT] [OUT.tsv]. It prints the grouped appendix of
reports/H4-IDENTITY-AUTHORITY-DESIGN.txt; OUT.tsv also receives every matching line. Pattern
matches are leads, not proof: the report classifies them and names the known false positives.
"""
from collections import defaultdict
from pathlib import Path
import re
import subprocess
import sys

CATS = {
    'slot': re.compile(r"""['"]p[12]['"]|\bp[12]\b"""),
    'pair': re.compile(r"\(1, ?2\)|\{1, ?2\}|\[1, ?2\]|range\(1, ?3\)|for \w+ ?= ?1, ?2\b|\b3 ?- ?\w|== ?1 and 2 or 1"
                       r"|== ?2 and 1 or 2|1 if [^\n]{1,40} else 2|2 if [^\n]{1,40} else 1"),
    'mdidx': re.compile(r"\.\{[12]\}|\$\w+[12]\b"),
    'role': re.compile(r"\b(is_?host|isHost|is_?guest|isGuest)\b|['\"](host|guest)['\"]"),
}


def scan(root):
    files = subprocess.run(['git', '-C', str(root), 'ls-files', '*.py', '*.lua', '*.xml', '*.c', '*.h', '*.ps1', '*.sh'],
                           capture_output=True, text=True, check=True).stdout.split()
    rows = []
    for name in files:
        if name.startswith('tests/') or name == 'dev/h4_inventory.py': continue
        text = (root / name).read_text(encoding='utf-8', errors='replace')
        for number, line in enumerate(text.splitlines(), 1):
            rows += [(name, number, cat, line.strip()[:160]) for cat, rx in CATS.items() if rx.search(line)]
    return rows


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    rows = scan(root)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(''.join('%s:%d\t%s\t%s\n' % row for row in rows), encoding='utf-8')
    grouped, totals = defaultdict(lambda: defaultdict(list)), defaultdict(int)
    for name, number, cat, _ in rows:
        grouped[name][cat].append(number); totals[cat] += 1
    print('files %d, hits %d (%s)' % (len(grouped), len(rows), ', '.join('%s %d' % kv for kv in sorted(totals.items()))))
    for name in sorted(grouped):
        print('  ' + name)
        for cat in CATS:
            if grouped[name][cat]: print('    %-5s %s' % (cat, ','.join(map(str, grouped[name][cat]))))


if __name__ == '__main__':
    main()
