"""Opt-in dock-reference persistence overlay for the guarded seed harness."""
from pathlib import Path
import argparse
import json
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dev import native_seed_test as seed_test
from dev import native_world_test as native


def install(run,mode):
    seed_test.install(run,mode)
    config=native.TARGET/'ui/config.lua'
    config.write_text(config.read_text().replace('X4LAN_CONFIG={','X4LAN_CONFIG={\n dock_registry_probe=true,'))
    for suffix,folder in (('lua','ui'),('xml','md')):
        shutil.copy2(ROOT/('dev/dock_registry_probe.'+suffix),native.TARGET/folder/('dock_registry_probe.'+suffix))
    tree=ET.parse(native.TARGET/'ui.xml')
    ET.SubElement(tree.find('environment'),'file',name='ui/dock_registry_probe.lua')
    tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
    (run/(mode+'-installed-hashes.json')).write_text(json.dumps({
        p.relative_to(native.TARGET).as_posix():seed_test.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('capture','load'))
    parser.add_argument('--run',type=Path,required=True);args=parser.parse_args();install(args.run,args.mode)
