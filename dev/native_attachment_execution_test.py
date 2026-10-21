"""Opt-in native execution-window overlay; never installed by standard checks."""
from pathlib import Path
import argparse,json,sys,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dev import native_attachment_controller_test as controller,native_world_test as native,native_seed_test as seed

def install(run,mode):
    controller.install(run,mode)
    config=native.TARGET/'ui/config.lua'
    config.write_text(config.read_text().replace('X4LAN_CONFIG={','X4LAN_CONFIG={\n attachment_execution_probe=true,'))
    tree=ET.parse(native.TARGET/'ui.xml');env=tree.find('environment')
    index=next(i for i,e in enumerate(env) if e.get('name')=='ui/attachment_controller_probe.lua')
    env.insert(index,ET.Element('file',name='ui/attachment_execution.lua'))
    tree.write(native.TARGET/'ui.xml',encoding='utf-8',xml_declaration=True)
    (run/(mode+'-installed-hashes.json')).write_text(json.dumps({p.relative_to(native.TARGET).as_posix():seed.digest(p) for p in native.TARGET.rglob('*') if p.is_file()},indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('capture','load'));parser.add_argument('--run',type=Path,required=True)
    args=parser.parse_args();install(args.run,args.mode)
