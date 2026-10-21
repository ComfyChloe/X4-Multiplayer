"""Explicit isolated overlay for the production dock registry/importer experiment."""
from pathlib import Path
import argparse
import json
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dev import native_seed_test as seed_test
from dev import native_world_test as native


def install(run, mode):
    seed_test.install(run, mode)
    config = native.TARGET / 'ui/config.lua'
    config.write_text(config.read_text().replace('X4LAN_CONFIG={',
        'X4LAN_CONFIG={\n dock_registry_probe=true,\n seed_dock_import_probe=true,'))
    shutil.copy2(ROOT / 'dev/seed_dock_import_probe.lua', native.TARGET / 'ui/seed_dock_import_probe.lua')
    tree = ET.parse(native.TARGET / 'ui.xml')
    for name in ('seed_docks', 'seed_dock_import', 'seed_dock_import_probe'):
        ET.SubElement(tree.find('environment'), 'file', name=f'ui/{name}.lua')
    tree.write(native.TARGET / 'ui.xml', encoding='utf-8', xml_declaration=True)
    (run / (mode+'-installed-hashes.json')).write_text(json.dumps({
        p.relative_to(native.TARGET).as_posix(): seed_test.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()}, indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode', choices=('capture', 'load'))
    parser.add_argument('--run', type=Path, required=True)
    args=parser.parse_args()
    install(args.run, args.mode)
