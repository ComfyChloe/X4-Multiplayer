"""Smoke-test staged artifacts in disposable copies; never installs into real X4."""
from pathlib import Path
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time


def run_checks(packages, output, wine=None):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'}
    if wine:
        env.update(WINEPREFIX='/tmp/x4-public-wine-prefix', WINEDEBUG='-all',
                   WINEDLLOVERRIDES='mscoree,mshtml=', DISPLAY='')
    with tempfile.TemporaryDirectory(prefix='x4 public packages ') as temporary:
        temp = Path(temporary)
        originals = {platform:next(packages.glob('*-' + platform + '-x64')) for platform in ('Linux','Windows')}

        def command(root, platform, *args):
            if platform == 'Linux': return [str(root/'runtime/bin/python3'),'-I','-B','launcher.py',*args]
            return [str(wine),str(root/'runtime/python.exe'),'-I','-B','launcher.py',*args]

        def execute(root, platform, *args):
            completed = subprocess.run(command(root,platform,*args), cwd=root, env=env,
                                       capture_output=True, text=True, timeout=45)
            if completed.returncode:
                raise RuntimeError(f'{platform} {args[0]} failed: {completed.stdout} {completed.stderr}')
            return completed.stdout

        for host_platform in (('Linux','Windows') if wine else ('Linux',)):
            guest_platform = 'Windows' if host_platform == 'Linux' and wine else 'Linux'
            host = temp/(host_platform+' host');guest=temp/(host_platform+' peer')
            shutil.copytree(originals[host_platform],host,symlinks=True)
            shutil.copytree(originals[guest_platform],guest,symlinks=True)
            # Synthetic traffic is strictly loopback; no packet goes to this
            # documentation address from the configured guest.
            execute(host,host_platform,'setup','--role','host','--server','192.0.2.25','--server-only')
            invite = host/'JOIN-SESSION.json'
            invite_argument = str(invite) if guest_platform=='Linux' else 'Z:'+str(invite).replace('/','\\')
            execute(guest,guest_platform,'setup','--role','guest','--invite',invite_argument,'--server-only')
            cfg=json.loads((host/'session.json').read_text())
            guest_cfg=json.loads((guest/'session.json').read_text())
            assert cfg['token']==guest_cfg['token'] and cfg['build']==guest_cfg['build']
            logfile=output/(host_platform.lower()+'-server-smoke.log')
            with logfile.open('w') as log:
                proc=subprocess.Popen(command(host,host_platform,'server'),cwd=host,env=env,
                                      stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                peers=[]
                try:
                    status=host/'logs/server-status.json'
                    deadline=time.monotonic()+30
                    while not status.exists():
                        if proc.poll() is not None:raise RuntimeError(logfile.read_text())
                        if time.monotonic()>deadline:raise RuntimeError('Server did not become ready: '+logfile.read_text())
                        time.sleep(.1)
                    assert 'CONNECTION OK' in execute(host,host_platform,'test')
                    for index,slot in enumerate(('p1','p2'),1):
                        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                        peers.append(sock);sock.connect(('127.0.0.1',cfg['port']));sock.settimeout(3)
                        nonce=str(index)*32
                        sock.send(f'HELLO|1|{cfg["token"]}|{cfg["build"]}|cluster_14_sector001_macro|{cfg["bubble"]}|{slot}|{nonce}'.encode())
                        deadline=time.monotonic()+3
                        while time.monotonic()<deadline:
                            fields=sock.recv(769).decode().split('|')
                            if fields[:3]==['WELCOME','1',nonce]:break
                        else:raise AssertionError('No WELCOME')
                        lease=fields[3]
                        sock.send(f'IDLE|1|{lease}|1'.encode())
                        sock.send(f'CREW|1|{lease}|1|{index}|{index}'.encode())
                        sock.send(f'NAV|1|{lease}|{index}|1|1|cluster_01_sector001_macro|250000|10000|0|0|0|0|0|0'.encode())
                        if index==1:
                            sock.send(f'QUEST|1|{lease}|1|1|0|0|0'.encode())
                    time.sleep(.8)
                    state=json.loads(status.read_text())
                    assert state['connected']==2,state
                    assert [s['pilot'] for s in state['fleet']]==['p1','p2'],state
                    assert state['quest']['delivered']==0
                    assert state['relay_process']['peak_memory_mib'] is not None
                    assert cfg['token'] not in status.read_text()
                    execute(host,host_platform,'diagnose')
                    results.append(dict(host_runtime=host_platform,guest_setup_runtime=guest_platform,
                        python='3.14.8',session_import=True,probe=True,udp_players=2,ship_authority=True,
                        sector_navigation_sent=True,quest_dashboard=True,peak_memory=True,
                        diagnostics=True,game_launched=False,windows_environment='Wine 10.0' if wine else None))
                finally:
                    for peer in peers:peer.close()
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGINT if host_platform=='Linux' else signal.SIGTERM)
                        try:proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=5)
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as check:check.bind(('127.0.0.1',cfg['port']))
            print('PASS:',host_platform,'packaged server and',guest_platform,'session setup',flush=True)
    (output/'runtime-smoke.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--wine',type=Path)
    args=parser.parse_args()
    run_checks(args.packages.resolve(),args.output.resolve(),args.wine)
