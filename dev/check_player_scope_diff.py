"""Offline check of the player-scope script patches against locally extracted game files.

Usage (game files stay outside the checkout and are never committed):
  python dev/check_player_scope_diff.py <extracted base dir containing aiscripts/, md/, libraries/>

Applies extension/x4_lan_lab/aiscripts/move.seekenemies.xml (add pos=after, replace @attr) to
the stock script, requires every selector to match exactly once, and validates the merged
script and md/x4lan_player_scope.xml against the game's XSDs when lxml is installed.
Does not launch X4 and proves nothing about runtime behaviour.
"""
import copy
import re
import sys
import xml.etree.ElementTree as E
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / 'extension/x4_lan_lab'
PATCH = EXT / 'aiscripts/move.seekenemies.xml'


def find(tree, sel):
    m = re.fullmatch(r"//(\w+)\[@(\w+)='([^']*)'\](/@(\w+))?", sel)
    if not m:
        raise SystemExit('unsupported selector ' + sel)
    tag, attr, value, _, target = m.groups()
    parents = {c: p for p in tree.iter() for c in p}
    hits = [e for e in tree.iter(tag) if e.get(attr) == value]
    if len(hits) != 1:
        raise SystemExit('selector matched %d nodes: %s' % (len(hits), sel))
    return hits[0], parents[hits[0]], target


def apply(stock, patch):
    tree = E.parse(stock)
    root = tree.getroot()
    for op in E.parse(patch).getroot():
        node, parent, target = find(root, op.get('sel'))
        if op.tag == 'add' and op.get('pos') == 'after' and not target:
            at = list(parent).index(node) + 1
            for i, child in enumerate(op):
                parent.insert(at + i, copy.deepcopy(child))
        elif op.tag == 'replace' and target:
            if not (op.text or '').startswith(node.get(target)):
                raise SystemExit('replacement does not extend the stock expression')
            node.set(target, op.text)
        else:
            raise SystemExit('unsupported operation ' + op.tag)
    return tree


def validate(path_or_tree, xsd):
    try:
        from lxml import etree
    except ImportError:
        print('lxml missing: schema validation not run')
        return None
    doc = etree.fromstring(E.tostring(path_or_tree.getroot())) if isinstance(path_or_tree, E.ElementTree) else etree.parse(str(path_or_tree))
    schema = etree.XMLSchema(etree.parse(str(xsd)))
    ok = schema.validate(doc)
    for err in list(schema.error_log)[:10]:
        print('  ', err)
    return ok


def main(base):
    base = Path(base)
    merged = apply(base / 'aiscripts/move.seekenemies.xml', PATCH)
    print('patch applied: 2 selectors matched once each')
    results = {
        'move.seekenemies.xml (merged)': validate(merged, base / 'libraries/aiscripts.xsd'),
        'x4lan_player_scope.xml': validate(EXT / 'md/x4lan_player_scope.xml', base / 'libraries/md.xsd'),
    }
    for name, ok in results.items():
        print(name, 'VALID' if ok else ('NOT RUN' if ok is None else 'INVALID'))
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
