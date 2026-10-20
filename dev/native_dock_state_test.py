"""Explicit fresh isolated-world native attachment transition experiment."""
from pathlib import Path
import json
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dev import native_world_test as native
from dev import native_seed_test as hashing


def install():
    native.setup.ensure_closed()
    if native.PROFILE.is_symlink() or (native.PROFILE/'save').is_symlink() or list((native.PROFILE/'save').glob('*.xml*')):
        raise RuntimeError('Attachment primitive requires an empty isolated save directory')
    native.install('p1',True)
    config=native.TARGET/'ui/config.lua'
    config.write_text(config.read_text().replace('X4LAN_CONFIG={','X4LAN_CONFIG={\n dock_state_probe=true,'))
    for suffix,folder in (('lua','ui'),('xml','md')):
        shutil.copy2(ROOT/('dev/dock_state_probe.'+suffix),native.TARGET/folder/('dock_state_probe.'+suffix))
    tree=ET.parse(native.TARGET/'ui.xml');ET.SubElement(tree.find('environment'),'file',name='ui/dock_state_probe.lua')
    tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
    (native.HERE/'dock-state-installed-hashes.json').write_text(json.dumps({
        p.relative_to(native.TARGET).as_posix():hashing.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()},indent=2))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=['install']);p.parse_args();install()
