"""Explicit fresh-world loadout reconstruction experiment; never a saved game."""
from pathlib import Path
import json
import secrets
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dev import native_world_test as native
from dev import native_seed_test as hashing


def install(saved=False,fill_groups=False):
    if fill_groups and not saved:
        raise ValueError('Full-group reconstruction requires the explicit saved-preset experiment')
    native.setup.ensure_closed()
    if native.PROFILE.is_symlink() or (native.PROFILE/'save').is_symlink() or list((native.PROFILE/'save').glob('*.xml*')):
        raise RuntimeError('Roundtrip experiment requires an empty isolated test-save directory')
    if saved:
        for path in (native.PROFILE/'loadouts.xml',native.PROFILE.parent/'loadouts.xml'):
            if path.exists() or path.is_symlink():
                raise RuntimeError('Saved-loadout primitive requires absent isolated/global preset files')
    native.install('p1',True)
    config=native.TARGET/'ui/config.lua'
    config.write_text(config.read_text().replace('X4LAN_CONFIG={','X4LAN_CONFIG={\n loadout_roundtrip=true,'))
    if saved:
        probe_id='x4lan_roundtrip_'+secrets.token_hex(8)
        config.write_text(config.read_text().replace('X4LAN_CONFIG={',f'X4LAN_CONFIG={{\n loadout_saved_exact=true,loadout_probe_id="{probe_id}",'))
        (native.HERE/'preset-journal.json').write_text(json.dumps({'probe_id':probe_id,'profile':str(native.PROFILE),
            'isolated_loadouts_preexisting':False,'global_loadouts_preexisting':False},indent=2))
    if fill_groups:
        config.write_text(config.read_text().replace('X4LAN_CONFIG={','X4LAN_CONFIG={\n loadout_fill_groups=true,'))
    for suffix,sub in (('lua','ui'),('xml','md')):
        shutil.copy2(ROOT/('dev/loadout_roundtrip_probe.'+suffix),native.TARGET/sub/('loadout_roundtrip_probe.'+suffix))
    tree=ET.parse(native.TARGET/'ui.xml');ET.SubElement(tree.find('environment'),'file',name='ui/loadout_roundtrip_probe.lua')
    tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
    (native.HERE/'roundtrip-installed-hashes.json').write_text(json.dumps({
        p.relative_to(native.TARGET).as_posix():hashing.digest(p)
        for p in native.TARGET.rglob('*') if p.is_file()},indent=2))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=['install']);p.add_argument('--saved',action='store_true')
    p.add_argument('--fill-groups',action='store_true',help='Dev-only full group then remove surplus components from newly created clones')
    args=p.parse_args();install(args.saved,args.fill_groups)
