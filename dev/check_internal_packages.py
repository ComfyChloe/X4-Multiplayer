"""Verify a generated W01 pair's inventory, digests and matching configuration."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import zipfile


def check(folder):
    packages=[]
    for archive in sorted(folder.glob('X4-LAN-*.zip')):
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None, archive.name
            names=z.namelist();prefix=archive.stem+'/'
            assert all(n.startswith(prefix) and '..' not in Path(n).parts for n in names)
            expected={}
            for line in z.read(prefix+'SHA256SUMS.txt').decode().splitlines():
                digest,name=line.split('  ',1);expected[name]=digest
                assert hashlib.sha256(z.read(prefix+name)).hexdigest()==digest, name
            actual={n[len(prefix):] for n in names if not n.endswith('/')}
            assert actual==set(expected)|{'SHA256SUMS.txt'}, 'Unlisted package content'
            meta=json.loads(z.read(prefix+'package.json'));session=json.loads(z.read(prefix+'session.json'))
            assert meta['channel']=='internal' and meta['test_profile']=='45841'
            assert session['world_sync'] and session['world_mode']=='replica'
            assert session['build'].startswith('world1-') and session['port']==45841
            cfg=z.read(prefix+'extension/x4_lan_lab/ui/config.lua').decode()
            assert 'world_sync = true' in cfg and 'world_mode = "replica"' in cfg
            assert re.search(r'\btoken = "'+session['token']+'"',cfg)
            assert not any(n.endswith('world_probe.lua') or '/dev_' in n for n in names)
            for name in ('WORLD-SYNC-TEST.txt','RELEASE-CHECKS.txt','AUTOMATED-TESTS.txt'):
                assert name in actual
            if meta['slot']=='p1': assert 'x4lan/universe.py' in actual
            packages.append((meta,session))
    assert len(packages)==2 and {m['slot'] for m,s in packages}=={'p1','p2'}
    assert packages[0][0]['pair_id']==packages[1][0]['pair_id']
    assert packages[0][1]==packages[1][1]
    assert packages[0][0]['version']==packages[1][0]['version']
    for line in (folder/'PACKAGES-SHA256.txt').read_text().splitlines():
        digest,name=line.split('  ',1)
        assert hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest
    print('PASS: matched internal W01 pair, full inventories and SHA-256 digests; no test instrumentation.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path)
    check(parser.parse_args().folder)
