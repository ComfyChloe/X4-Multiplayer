"""Validate the combat scenario MD files against the game's own md.xsd (development check).

The schema is not part of this repository (Egosoft file). It is read from an extracted
copy, by default ../x4-re/gamescripts/resolved/base/libraries/md.xsd, or from --xsd.
Needs lxml. Loading the schema takes about 1.5 minutes. Never launches X4.

    python dev/combat_scenario/validate.py [--xsd PATH]
"""
import argparse
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def default_xsd() -> Path:
    base = Path(os.environ.get('X4RE_DIR', ROOT.parent / 'x4-re'))
    return base / 'gamescripts' / 'resolved' / 'base' / 'libraries' / 'md.xsd'


def files() -> list[Path]:
    return [HERE / 'extension' / 'md' / 'x4mp_combat_scenario.xml', *sorted((HERE / 'variants').glob('*.xml'))]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--xsd', type=Path, default=default_xsd())
    args = parser.parse_args(argv)
    try:
        from lxml import etree
    except ImportError:
        print('lxml missing: pip install lxml', file=sys.stderr)
        return 2
    if not args.xsd.is_file():
        print(f'schema not found: {args.xsd}', file=sys.stderr)
        return 2
    schema = etree.XMLSchema(etree.parse(str(args.xsd)))
    failed = 0
    for path in files():
        ok = schema.validate(etree.parse(str(path)))
        print(f'{"PASS" if ok else "FAIL"} {path.relative_to(ROOT)}')
        if not ok:
            failed += 1
            for error in schema.error_log:
                print(f'  line {error.line}: {error.message}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
