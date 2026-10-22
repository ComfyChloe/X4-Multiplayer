"""Verify the role-selectable internal packages, runtimes and complete inventories."""
from pathlib import Path
import argparse, hashlib, json, zipfile

def check(folder):
    packages=[]
    for archive in sorted(folder.glob('X4-LAN-*.zip')):
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None, archive.name
            names=z.namelist();prefix=archive.stem+'/'
            assert len(names)==len(set(names))
            assert all(n.startswith(prefix) and '..' not in Path(n).parts for n in names)
            expected={}
            for line in z.read(prefix+'SHA256SUMS.txt').decode().splitlines():
                digest,name=line.split('  ',1);expected[name]=digest
                assert hashlib.sha256(z.read(prefix+name)).hexdigest()==digest,name
            actual={n[len(prefix):] for n in names if not n.endswith('/')}
            assert actual==set(expected)|{'SHA256SUMS.txt'},'Unlisted package content'
            meta=json.loads(z.read(prefix+'package.json'))
            assert meta['channel']=='internal' and meta['test_profile']=='45841'
            assert meta['slot'] is None and meta['package_id'] is None
            assert meta['runtime_bundled'] is True and meta['roles']==['host','guest']
            assert 'session.json' not in actual and 'JOIN-SESSION.json' not in actual
            cfg=z.read(prefix+'extension/x4_lan_lab/ui/config.lua').decode()
            assert 'enabled = false' in cfg and 'token' not in cfg
            assert not any(n.endswith('_probe.lua') or '_probe.xml' in n or '/dev_' in n for n in names)
            for name in ('WORLD-SYNC-TEST.txt','NEXT-TEST.txt','RELEASE-CHECKS.txt','AUTOMATED-TESTS.txt','launcher.py','RUNTIMES.json'):
                assert name in actual,name
            for module in ('host','monitor','universe','cargo','identity','coop','quests','bubble','economy'):
                assert 'x4lan/'+module+'.py' in actual,module
            windows=meta['platform']=='Windows11'
            suffix='.cmd' if windows else '.sh'
            for action in ('1-INSTALL','2-START-SERVER','2-TEST-CONNECTION','3-START-X4-TEST','4-DIAGNOSTICS','5-REMOVE-MOD','7-VERIFY-PACKAGE'):
                assert action+suffix in actual
            assert ('runtime/python.exe' if windows else 'runtime/bin/python3') in actual
            if windows:
                assert z.read(prefix+'runtime/python.exe')[:2]==b'MZ'
                assert z.read(prefix+'runtime/python314._pth').decode()=='python314.zip\n.\n..\n'
                assert 'setup_windows.ps1' in actual
            else: assert 'setup_linux.py' in actual
            assert z.read(prefix+'VERSION.txt').decode().strip()==meta['version']
            packages.append(meta)
    assert len(packages)==2 and {m['platform'] for m in packages}=={'Linux','Windows11'}
    assert packages[0]['pair_id']==packages[1]['pair_id']
    assert packages[0]['version']==packages[1]['version']
    for line in (folder/'PACKAGES-SHA256.txt').read_text().splitlines():
        digest,name=line.split('  ',1)
        assert hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest
    print('PASS: both internal roles/platforms, bundled runtimes and relay, inert templates, complete inventories/digests; no probes or session secrets.')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path)
    check(parser.parse_args().folder)
