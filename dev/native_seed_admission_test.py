"""Opt-in isolated production admission probe; no network or gameplay fixtures."""
from pathlib import Path
import argparse,json,shutil,sys,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dev import native_dock_import_test as dock,native_seed_test as seed,native_world_test as native


def install(run,mode):
    dock.install(run,mode)
    if mode=='load':
        state=json.loads((run/'state.json').read_text())
        config=dict(enabled=True,test_profile='45841',seed_id=state['seed_id'],seed_probe='load',
                    seed_admission_probe=True,seed_verified=True,seed_binding=True,attachment_sync=True,
                    bubble=state['seed_id'],bubble_actions=True,coop=True,world_sync=True,slot='p2')
        (native.TARGET/'ui/config.lua').write_text('X4LAN_CONFIG={\n'+''.join(f' {k}={json.dumps(v)},\n' for k,v in config.items())+'}\n')
        shutil.copy2(ROOT/'dev/seed_admission_probe.lua',native.TARGET/'ui/seed_admission_probe.lua')
        tree=ET.parse(native.TARGET/'ui.xml');env=tree.find('environment')
        for element in list(env):
            if element.tag=='file':env.remove(element)
        for name in ('config','seed_docks','seed_dock_import','seed_game','seed_admission_probe','seed_dock_game'):
            ET.SubElement(env,'file',name=f'ui/{name}.lua')
        tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
    (run/(mode+'-installed-hashes.json')).write_text(json.dumps({p.relative_to(native.TARGET).as_posix():seed.digest(p) for p in native.TARGET.rglob('*') if p.is_file()},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=('capture','load'));p.add_argument('--run',type=Path,required=True)
    a=p.parse_args();install(a.run,a.mode)
