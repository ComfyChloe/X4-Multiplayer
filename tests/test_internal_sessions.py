"""Internal Windows/Linux role and invitation boundaries."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
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
        with patch.object(launcher,'ROOT',self.guest),self.assertRaisesRegex(RuntimeError,'Only the HOST'):launcher.server()
        with patch.object(launcher,'ROOT',self.host),patch('x4lan.monitor.run') as run:
            launcher.server();cfg=run.call_args.args[0];self.assertTrue(cfg['world_sync']);self.assertTrue(cfg['fire2']);self.assertEqual(cfg['port'],45841)
            with launcher.session_lock():
                with self.assertRaisesRegex(RuntimeError,'running'):launcher.server()
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
