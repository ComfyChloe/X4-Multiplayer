"""Public session isolation and Windows server regressions; no real game writes."""
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from x4lan.host import Relay, Session
from x4lan import monitor
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_launcher', SOURCE / 'distribution/public/launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class PublicSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = self.package('host'); self.guest = self.package('guest')

    def package(self, name):
        root = self.root / name
        (root / 'extension/x4_lan_lab/ui').mkdir(parents=True)
        (root / 'package.json').write_text(json.dumps(dict(channel='public', version='6.0.1', platform='Linux', slot=None, steam_build='23660954')))
        return root

    def configure_host(self, root=None):
        root = root or self.host
        with patch.object(launcher, 'ROOT', root), patch.object(launcher, 'ensure_closed'):
            launcher.configure('host', '192.168.1.25')
        return launcher.read_json(root / 'JOIN-SESSION.json')

    def test_host_guest_share_session_but_have_distinct_roles_and_addresses(self):
        invite = self.configure_host()
        with patch.object(launcher, 'ROOT', self.guest), patch.object(launcher, 'ensure_closed'):
            launcher.configure('guest', invite=self.host / 'JOIN-SESSION.json')
            package, session = launcher.configured()
        self.assertEqual(package['slot'], 'p2'); self.assertEqual(session['host'], '192.168.1.25')
        self.assertEqual(session['token'], invite['token'])
        with patch.object(launcher, 'ROOT', self.host): package, host = launcher.configured()
        self.assertEqual(package['slot'], 'p1'); self.assertEqual(host['host'], '127.0.0.1')
        self.assertEqual(host['port'], 45842)
        self.assertIn('test_profile = "45842"', (self.host / 'extension/x4_lan_lab/ui/config.lua').read_text())

    def test_each_group_has_fresh_credentials_and_no_overwrite(self):
        first = self.configure_host(); second = self.configure_host(self.guest)
        self.assertNotEqual(first['token'], second['token']); self.assertNotEqual(first['bubble'], second['bubble'])
        before = (self.host / 'session.json').read_bytes()
        with patch.object(launcher, 'ROOT', self.host), patch.object(launcher, 'ensure_closed'):
            with self.assertRaisesRegex(RuntimeError, 'already has a session'): launcher.configure('host', '192.168.1.26')
        self.assertEqual(before, (self.host / 'session.json').read_bytes())

    def test_rejects_internal_wrong_version_bad_address_and_extra_fields(self):
        valid = self.configure_host()
        for key, value in [('channel','internal'), ('version','6.0.0'), ('port',45841),
                           ('server','127.0.0.1'), ('server','0.0.0.0'), ('server','224.0.0.1'),
                           ('token','bad'), ('bubble','coop-01'), ('coop',1), ('format',True), ('extra','x')]:
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                launcher.validate_invite({**valid, key:value})

    def test_refuses_session_change_while_installed_or_running(self):
        (self.host / 'installation.json').write_text('{}')
        with patch.object(launcher,'ROOT',self.host), patch.object(launcher,'ensure_closed'):
            with self.assertRaisesRegex(RuntimeError,'Remove the installed'): launcher.configure('host','192.168.1.25')
        with patch.object(launcher,'ROOT',self.guest):
            with launcher.session_lock():
                with self.assertRaisesRegex(RuntimeError,'running'):
                    with launcher.session_lock(): pass

    def test_public_and_internal_wire_builds_do_not_join(self):
        invite = self.configure_host()
        session = Session(invite['token'], invite['build'], invite['bubble'])
        packet = f'HELLO|1|{invite["token"]}|9.00-steam23660954|sector_macro|{invite["bubble"]}|p1|{"a"*32}'.encode()
        session.receive(packet, ('127.0.0.1',9000), 0)
        self.assertEqual(len(session.peers),0)

    def test_wrong_installed_owner_never_launches_or_removes_game(self):
        self.configure_host()
        game = self.root / 'game'
        target = game / 'extensions/x4_lan_lab'; target.mkdir(parents=True)
        (target / '.x4lan-package-id').write_text('internal-owner')
        pkg = launcher.read_json(self.host/'package.json')
        (self.host/'installation.json').write_text(json.dumps(dict(game=str(game),profile=str(self.root/'45842'),package_id=pkg['package_id'])))
        with patch.object(launcher,'ROOT',self.host), patch.object(launcher,'ensure_closed'), patch.object(launcher,'backend_module') as backend:
            for action in ['start','uninstall']:
                with self.assertRaisesRegex(RuntimeError,'another package'):launcher.platform_action(action)
            backend.assert_not_called()
        self.assertTrue(target.exists())

    def test_diagnostics_redact_keys_and_do_not_export_invites(self):
        invite = self.configure_host(); logs=self.host/'logs';logs.mkdir()
        (logs/'x4-debug.txt').write_text('payload '+invite['token'])
        with patch.object(launcher,'ROOT',self.host):launcher.diagnostics()
        self.assertNotIn(invite['token'],(logs/'x4-debug.txt').read_text())
        self.assertFalse((logs/'JOIN-SESSION.json').exists())

    def test_guest_cannot_start_second_server(self):
        self.configure_host()
        with patch.object(launcher,'ROOT',self.guest), patch.object(launcher,'ensure_closed'):
            launcher.configure('guest',invite=self.host/'JOIN-SESSION.json')
            with self.assertRaisesRegex(RuntimeError,'Only the HOST'):launcher.server()

    def test_uninstalled_session_setup_does_not_need_to_stop_an_unrelated_game(self):
        with patch.object(launcher,'ROOT',self.host), patch.object(launcher,'ensure_closed',side_effect=RuntimeError('X4 running')) as closed:
            launcher.configure('host','192.168.1.25')
            closed.assert_not_called()
            with self.assertRaisesRegex(RuntimeError,'X4 running'):launcher.platform_action('install')

    def test_windows_host_install_receives_loopback_and_guest_receives_remote_ip(self):
        self.configure_host()
        with patch.object(launcher,'ROOT',self.guest), patch.object(launcher,'ensure_closed'):
            launcher.configure('guest',invite=self.host/'JOIN-SESSION.json')
        for root, address in [(self.host,'127.0.0.1'),(self.guest,'192.168.1.25')]:
            with patch.object(launcher,'ROOT',root), patch.object(launcher,'ensure_closed'), patch.object(launcher.sys,'platform','win32'), patch.object(launcher.subprocess,'run') as run:
                launcher.platform_action('install', 'C:\\Games\\X4 Foundations')
                args=run.call_args.args[0]
                self.assertEqual(args[args.index('-ServerIP')+1],address)
                self.assertIn('C:\\Games\\X4 Foundations',args)

    def test_linux_temp_config_link_never_overwrites_another_profile(self):
        spec=importlib.util.spec_from_file_location('public_linux_fixture',SOURCE/'distribution/linux_setup.py')
        setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
        profile=self.root/'45842';profile.mkdir()
        protected=self.root/'normal-config.xml';protected.write_text('<root><language>49</language></root>')
        before=protected.read_bytes()
        (profile/'config.x4lan.tmp').symlink_to(protected)
        with patch.object(setup,'PROFILE','45842'):
            with self.assertRaisesRegex(RuntimeError,'linked test profile item'):setup.configure_profile(profile,self.root/'backup')
        self.assertEqual(protected.read_bytes(),before)


class WindowsRelayTests(unittest.TestCase):
    def test_closed_udp_peer_does_not_stop_relay(self):
        relay=Relay.__new__(Relay);relay.socket=Mock();relay.next_tick=100
        relay.socket.recvfrom.side_effect=[ConnectionResetError(10054,'peer closed'),BlockingIOError()]
        relay.step(1)
        self.assertEqual(relay.socket.recvfrom.call_count,2)

    def test_windows_udp_reset_mode_is_disabled(self):
        sock=Mock()
        with patch.object(socket,'SIO_UDP_CONNRESET',0x9800000c,create=True),patch.object(socket,'socket',return_value=sock):
            relay=Relay('127.0.0.1',0,Session('a'*32,'build','bubble'))
        sock.ioctl.assert_called_once_with(0x9800000c,False);relay.close()

    def test_windows_interfaces_are_decoded_without_localized_text_parsing(self):
        import subprocess
        rows=[dict(InterfaceAlias='ZeroTier One',IPAddress='10.2.3.4'),dict(InterfaceAlias='Ethernet',IPAddress='192.168.1.5')]
        with patch.object(monitor.sys,'platform','win32'),patch.object(subprocess,'CREATE_NO_WINDOW',0,create=True),patch.object(subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(rows))):
            result=monitor.addresses()
        self.assertEqual(result[0]['kind'],'ZeroTier');self.assertEqual(result[1]['ip'],'192.168.1.5')


@unittest.skipUnless(LuaRuntime,'LuaJIT required')
class PublicProfileTests(unittest.TestCase):
    def runtime(self, configured_profile, actual_profile):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK+EXTRA)
        lua.globals().X4LAN_CONFIG.test_profile=configured_profile
        lua.execute('api.GetSaveFolderPath=function() return ... end'.replace('...', '"/isolated/X4/'+actual_profile+'/save/"'))
        lua.execute((SOURCE/'extension/x4_lan_lab/ui/bubble_game.lua').read_text())
        return lua

    def test_public_profile_activates_and_internal_or_normal_profile_does_not(self):
        for configured, actual, active in [('45842','45842',True),('45842','45841',False),('45841','45842',False),('45842','70000001',False),('70000001','70000001',False)]:
            with self.subTest(configured=configured,actual=actual):
                lua=self.runtime(configured,actual)
                self.assertEqual(bool(lua.globals().X4LAN_BUBBLE_API.state.active),active)


if __name__=='__main__':unittest.main()
