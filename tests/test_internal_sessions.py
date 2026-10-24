"""Internal Windows/Linux role and invitation boundaries."""
import importlib.util,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('internal_launcher',ROOT/'distribution/session_launcher.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

class InternalSessions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.host=self.package('host','Windows11');self.guest=self.package('guest','Linux')
    def package(self,name,platform):
        p=self.root/name;(p/'extension/x4_lan_lab/ui').mkdir(parents=True)
        (p/'package.json').write_text(json.dumps(dict(channel='internal',version=launcher.VERSION,platform=platform,slot=None,steam_build='23660954')))
        return p
    def configure(self):
        with patch.object(launcher,'ROOT',self.host):launcher.configure('host','192.0.2.109')
        with patch.object(launcher,'ROOT',self.guest):launcher.configure('guest',invite=self.host/'JOIN-SESSION.json')
        return json.loads((self.host/'JOIN-SESSION.json').read_text())
    def test_windows_host_linux_guest_share_world_protocol_and_ship_spec(self):
        invite=self.configure()
        for folder,slot,address in ((self.host,'p1','127.0.0.1'),(self.guest,'p2','192.0.2.109')):
            with patch.object(launcher,'ROOT',folder):meta,cfg=launcher.configured()
            self.assertEqual((meta['slot'],cfg['host'],cfg['port']),(slot,address,45841))
            self.assertEqual(cfg['token'],invite['token']);self.assertTrue(cfg['world_sync']);self.assertTrue(cfg['fire2'])
            lua=(folder/'extension/x4_lan_lab/ui/config.lua').read_text()
            for text in ('world_sync = true','fire2 = true','ship_weapons = 6','ship_ter_s_xperimental_01_a_macro','test_profile = "45841"'):
                self.assertIn(text,lua)
    def test_separate_test_groups_get_distinct_keys_and_world_ids(self):
        a=self.configure();other=self.package('other','Windows11')
        with patch.object(launcher,'ROOT',other):launcher.configure('host','192.0.2.202')
        b=json.loads((other/'JOIN-SESSION.json').read_text())
        self.assertNotEqual(a['token'],b['token']);self.assertNotEqual(a['bubble'],b['bubble'])
    def test_public_old_incomplete_and_unsafe_invites_are_rejected(self):
        a=self.configure()
        for key,value in [('channel','public'),('port',45842),('version','7.1.1'),('fire2',False),('world_sync',1),('world_mode','host'),('server','127.0.0.1'),('extra',True)]:
            with self.subTest(key=key),self.assertRaises(ValueError):launcher.validate_invite({**a,key:value})
        with self.assertRaises(ValueError):launcher.validate_invite({k:v for k,v in a.items() if k!='world_sync'})
    def test_configure_cannot_replace_installed_or_existing_session(self):
        self.configure();before=(self.host/'session.json').read_bytes()
        with patch.object(launcher,'ROOT',self.host),self.assertRaisesRegex(RuntimeError,'already has a session'):launcher.configure('host','192.0.2.202')
        self.assertEqual(before,(self.host/'session.json').read_bytes())
        other=self.package('installed','Linux');(other/'installation.json').write_text('{}')
        with patch.object(launcher,'ROOT',other),self.assertRaisesRegex(RuntimeError,'Remove the installed'):launcher.configure('host','192.0.2.109')
    def test_windows_backend_gets_loopback_only_for_host(self):
        self.configure()
        for folder,address in ((self.host,'127.0.0.1'),(self.guest,'192.0.2.109')):
            with patch.object(launcher,'ROOT',folder),patch.object(launcher,'ensure_closed'),patch.object(launcher.sys,'platform','win32'),patch.object(launcher.subprocess,'run') as run:
                launcher.platform_action('install','C:\\Games\\X4')
                argv=run.call_args.args[0];self.assertEqual(argv[argv.index('-ServerIP')+1],address)
                self.assertIn(str(folder/'setup_windows.ps1'),argv)
    def test_only_host_starts_world_relay_and_session_is_locked(self):
        self.configure()
        with patch.object(launcher,'ROOT',self.guest),self.assertRaisesRegex(RuntimeError,'Only the HOST'):launcher.server(console=True)
        with patch.object(launcher,'ROOT',self.host),patch('x4lan.monitor.probe',return_value=False),patch('x4lan.monitor.run') as run,patch.object(launcher,'backend_module'):
            launcher.server(console=True);cfg=run.call_args.args[0];self.assertTrue(cfg['world_sync']);self.assertTrue(cfg['fire2']);self.assertEqual(cfg['port'],45841)
            with launcher.session_lock():
                with self.assertRaisesRegex(RuntimeError,'running'):launcher.server(console=True)
    def test_desktop_server_opens_terminal_without_starting_in_background(self):
        with patch.object(launcher,'ROOT',self.host),patch.object(launcher.sys,'platform','linux'),patch.object(launcher.sys.stdout,'isatty',return_value=False),patch.object(launcher.shutil,'which',return_value='/usr/bin/konsole'),patch.object(launcher.subprocess,'Popen') as start,patch.object(launcher,'configured') as configured:
            launcher.server()
            argv=start.call_args.args[0]
            self.assertEqual(argv,['/usr/bin/konsole','--separate','-e','bash',str(self.host/'03-START-SERVER-HOST.sh'),'--console'])
            self.assertEqual(start.call_args.kwargs['cwd'],self.host)
            for name in ('stdin','stdout','stderr'):
                self.assertEqual(start.call_args.kwargs[name],launcher.subprocess.DEVNULL)
            configured.assert_not_called()
    def test_desktop_without_terminal_fails_instead_of_starting_hidden_server(self):
        with patch.object(launcher.sys,'platform','linux'),patch.object(launcher.sys.stdout,'isatty',return_value=False),patch.object(launcher.shutil,'which',return_value=None),patch('x4lan.monitor.run') as run:
            with self.assertRaisesRegex(RuntimeError,'No supported terminal'):launcher.server()
            run.assert_not_called()
    def test_second_open_attaches_while_server_holds_setup_lock(self):
        invite=self.configure();logs=self.host/'logs';logs.mkdir()
        status={k:invite[k] for k in ('build','bubble','port')};status['running']=True
        (logs/'server-status.json').write_text(json.dumps(status))
        with patch.object(launcher,'ROOT',self.host),patch('x4lan.monitor.probe',return_value=True),patch('x4lan.monitor.run') as run,patch('x4lan.monitor.attach') as attach:
            with launcher.session_lock():launcher.server(console=True)
            attach.assert_called_once_with(logs/'server-status.json');run.assert_not_called()
            for key,value in [('running',False),('build','old'),('bubble','other'),('port',45842)]:
                (logs/'server-status.json').write_text(json.dumps({**status,key:value}))
                with self.subTest(key=key),self.assertRaisesRegex(RuntimeError,'no matching live status'):launcher.server(console=True)
            attach.assert_called_once()
    def test_wrong_owned_installation_never_starts_or_removes(self):
        self.configure();game=self.root/'game';target=game/'extensions/x4_lan_lab';target.mkdir(parents=True)
        (target/'.x4lan-package-id').write_text('other-session')
        meta=json.loads((self.host/'package.json').read_text())
        (self.host/'installation.json').write_text(json.dumps(dict(game=str(game),profile=str(self.root/'45841'),package_id=meta['package_id'])))
        with patch.object(launcher,'ROOT',self.host),patch.object(launcher,'ensure_closed'),patch.object(launcher,'backend_module') as backend:
            for action in ('start','uninstall'):
                with self.assertRaisesRegex(RuntimeError,'another package'):launcher.platform_action(action)
            backend.assert_not_called()
    def test_diagnostics_redact_secret_and_leave_invitation_private(self):
        invite=self.configure();logs=self.host/'logs';logs.mkdir();(logs/'x4-debug.txt').write_text(invite['token'])
        with patch.object(launcher,'ROOT',self.host):launcher.diagnostics()
        self.assertNotIn(invite['token'],(logs/'x4-debug.txt').read_text());self.assertFalse((logs/'JOIN-SESSION.json').exists())
    def test_setup_retries_failed_settings_import_without_reinstalling_or_reimporting_success(self):
        self.configure();(self.host/'installation.json').write_text('{}')
        args=SimpleNamespace(server_only=False,game=None,settings_profile=None,keep_test_settings=False)
        with patch.object(launcher,'ROOT',self.host),patch.object(launcher,'platform_action') as install,patch.object(launcher,'import_settings') as settings:
            settings.side_effect=RuntimeError('invalid source input XML')
            with self.assertRaisesRegex(RuntimeError,'invalid source'):launcher.setup(args)
            settings.side_effect=lambda *_:(self.host/'settings-import.json').write_text('{}')
            launcher.setup(args);launcher.setup(args)
            self.assertEqual(settings.call_count,2);install.assert_not_called()
    def test_server_only_setup_does_not_import_player_preferences(self):
        self.configure()
        with patch.object(launcher,'ROOT',self.host),patch.object(launcher,'import_settings') as settings,patch.object(launcher,'platform_action') as install:
            launcher.setup(SimpleNamespace(server_only=True,game=None))
            settings.assert_not_called();install.assert_not_called()

    def test_windows_invitation_dialog_returns_path_as_data(self):
        path = 'C:\\Users\\Test Name\\Downloads\\JOIN-SESSION.json'
        with patch.object(launcher.sys, 'platform', 'win32'), patch.object(launcher.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=path+'\n', stderr='')) as run:
            self.assertEqual(launcher.choose_invitation(), path)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4], ['powershell.exe', '-NoProfile', '-STA', '-Command'])
            self.assertNotIn(path, argv[-1])
            self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_cancelled_invitation_does_not_prompt_or_configure(self):
        with patch.object(launcher.sys, 'platform', 'win32'), patch.object(launcher.subprocess, 'run', return_value=SimpleNamespace(returncode=2, stdout='', stderr='')), patch('builtins.input') as ask:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                launcher.choose_invitation()
            ask.assert_not_called()

    def test_unavailable_dialog_falls_back_to_quoted_terminal_path(self):
        with patch.object(launcher.sys, 'platform', 'win32'), patch.object(launcher.subprocess, 'run', side_effect=FileNotFoundError), patch('builtins.input', return_value='"C:/Folder With Spaces/JOIN-SESSION.json"'):
            self.assertEqual(launcher.choose_invitation(), 'C:/Folder With Spaces/JOIN-SESSION.json')


class InternalPackageRuntime(unittest.TestCase):
    def test_both_built_servers_import_and_construct_session_outside_checkout(self):
        from distribution import build_packages
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'packages';build_packages.build(out)
            folders=sorted(out.glob('X4-LAN-*'))
            folders=[p for p in folders if p.is_dir()];self.assertEqual(len(folders),2)
            for folder in folders:
                with self.subTest(platform=folder.name):
                    code="import sys;sys.path.insert(0,sys.argv[1]);from x4lan.monitor import run;from x4lan.universe import UniverseSession;s=UniverseSession('a'*32,'world1-package-check','bubble');print(type(s).__name__)"
                    result=subprocess.run([sys.executable,'-I','-B','-c',code,str(folder)],cwd=d,text=True,capture_output=True)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(result.stdout.strip(),'UniverseSession')
