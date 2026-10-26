"""Community installer cases: temporary files only, no game or Steam interaction."""
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_internal_sessions as sessions

launcher = sessions.launcher
SOURCE = Path(__file__).resolve().parents[1] / 'tools/distribution'


class LauncherRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.root = self.base / 'package'; self.root.mkdir()
        (self.root / 'extension/x4_lan_lab/ui').mkdir(parents=True)
        self.clean = dict(channel='internal', version=launcher.VERSION, slot=None, platform='Linux')
        self.write('package.json', self.clean)
        for name in ('profile_settings.py', 'world_launcher.py'):
            shutil.copyfile(SOURCE / name, self.root / name)
        p = patch.object(launcher, 'ROOT', self.root); p.start(); self.addCleanup(p.stop)
        self.args = SimpleNamespace(server_only=False, game=None, settings_profile=None, keep_test_settings=False)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def session(self, installed=False):
        launcher.configure('host', '192.0.2.109')
        if installed:
            game = self.base / 'game'; target = game / 'extensions/x4_lan_lab'
            shutil.copytree(self.root / 'extension/x4_lan_lab', target)
            package, _ = launcher.configured()
            (target / '.x4lan-package-id').write_text(package['package_id'])
            profile = self.base / '45841'; (profile / 'save').mkdir(parents=True)
            (profile / 'save/save_010.xml.gz').write_bytes(b'owned snapshot remains untouched')
            (profile / 'content.xml').write_bytes(b'<content/>')
            self.write('installation.json', dict(package_id=package['package_id'], game=str(game), profile=str(profile)))
        return launcher.configured()

    def overlay(self):
        self.write('package.json', self.clean)
        (self.root / 'extension/x4_lan_lab/ui/config.lua').write_text('X4LAN_CONFIG = { enabled = false }')

    def inventory(self):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob('*')
                if p.is_file() and ('game' in p.parts or '45841' in p.parts or p.name == 'session.json')}

    def test_recover_preserves_live_installation_save_and_session_bytes_and_times(self):
        package, session = self.session(installed=True)
        original_config = (self.root / 'extension/x4_lan_lab/ui/config.lua').read_bytes()
        self.overlay(); before = self.inventory()
        launcher.recover_session_metadata()
        self.assertEqual(launcher.configured(), (package, session))
        self.assertEqual(self.inventory(), before)
        self.assertEqual((self.root / 'extension/x4_lan_lab/ui/config.lua').read_bytes(), original_config)
        self.assertEqual(len(list((self.root / 'backups').glob('session-recovery-*/before.json'))), 1)

    def test_recover_uninstalled_guest_and_missing_host_invite_without_new_keys(self):
        package, saved = self.session(); invitation = json.loads((self.root / 'JOIN-SESSION.json').read_text())
        (self.root / 'JOIN-SESSION.json').unlink(); self.overlay()
        launcher.recover_session_metadata()
        self.assertEqual(json.loads((self.root / 'JOIN-SESSION.json').read_text()), invitation)
        saved['host'] = saved['server']; self.write('session.json', saved); self.overlay()
        launcher.recover_session_metadata()
        self.assertEqual(launcher.configured()[0]['slot'], 'p2')
        self.assertEqual(launcher.configured()[1]['token'], invitation['token'])

    def test_recovery_refuses_conflicting_marker_invitation_version_and_links(self):
        self.session(installed=True); self.overlay()
        paths = [self.root / 'package.json', self.root / 'extension/x4_lan_lab/ui/config.lua']
        before = [p.read_bytes() for p in paths]
        marker = self.base / 'game/extensions/x4_lan_lab/.x4lan-package-id'
        old = marker.read_bytes(); marker.write_text('foreign')
        with self.assertRaisesRegex(RuntimeError, 'different package'): launcher.recover_session_metadata()
        marker.write_bytes(old)
        invitation = (self.root / 'JOIN-SESSION.json').read_bytes(); self.write('JOIN-SESSION.json', {})
        with self.assertRaisesRegex(RuntimeError, 'conflicts'): launcher.recover_session_metadata()
        (self.root / 'JOIN-SESSION.json').write_bytes(invitation)
        self.write('package.json', {**self.clean, 'version': 'older'})
        with self.assertRaisesRegex(RuntimeError, 'version/channel'): launcher.recover_session_metadata()
        self.write('package.json', self.clean)
        target = self.root / 'extension/x4_lan_lab/ui/config.lua'; target.unlink()
        sentinel = self.base / 'untouched.lua'; sentinel.write_bytes(before[1]); target.symlink_to(sentinel)
        with self.assertRaisesRegex(RuntimeError, 'Linked'): launcher.recover_session_metadata()
        self.assertEqual(sentinel.read_bytes(), before[1])
        self.assertEqual(paths[0].read_bytes(), before[0])
        self.assertFalse((self.root / 'backups').exists())

    def test_failed_recovery_rolls_back_exact_files_and_new_invitation(self):
        self.session(); (self.root / 'JOIN-SESSION.json').unlink(); self.overlay()
        paths = [self.root / 'package.json', self.root / 'extension/x4_lan_lab/ui/config.lua']
        before = [p.read_bytes() for p in paths]
        settings = launcher.backend_module('profile_settings'); atomic = settings.atomic_write
        def fail(path, data):
            if path.name == 'package.json': raise OSError('injected recovery write failure')
            atomic(path, data)
        with patch.object(launcher, 'backend_module', return_value=settings), patch.object(settings, 'atomic_write', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'injected'): launcher.recover_session_metadata()
        self.assertEqual([p.read_bytes() for p in paths], before)
        self.assertFalse((self.root / 'JOIN-SESSION.json').exists())
        launcher.recover_session_metadata(); launcher.configured()

    def test_partial_or_explicit_wrong_ownership_is_not_guessed_away(self):
        self.session(); package, _ = launcher.configured()
        self.write('package.json', {**package, 'package_id': 'other'})
        before = (self.root / 'package.json').read_bytes()
        launcher.recover_session_metadata()
        with self.assertRaisesRegex(RuntimeError, 'ownership'): launcher.configured()
        self.assertEqual((self.root / 'package.json').read_bytes(), before)
        self.overlay(); self.write('session.json', {'host': '127.0.0.1'})
        with self.assertRaises(ValueError): launcher.recover_session_metadata()
        self.assertFalse((self.root / 'backups').exists())

    def test_settings_override_on_reinstall_is_not_ignored(self):
        self.session(installed=True); self.write('settings-import.json', {'source': 'old'})
        self.args.settings_profile = 'new profile'
        with patch.object(launcher, 'ensure_closed'), patch.object(launcher, 'import_settings') as settings:
            launcher.setup(self.args)
        settings.assert_called_once_with('new profile', False)

    def test_missing_installed_mod_does_not_report_setup_success(self):
        self.session(installed=True)
        (self.base / 'game/extensions/x4_lan_lab/.x4lan-package-id').unlink()
        with patch.object(launcher, 'ensure_closed'), patch.object(launcher, 'import_settings') as settings:
            with self.assertRaisesRegex(RuntimeError, 'different package'): launcher.setup(self.args)
        settings.assert_not_called()

    def test_profile_reselection_prompt_and_decline_preserve_session(self):
        self.session(installed=True); self.write('settings-import.json', {'source': 'old'})
        before = self.inventory()
        with patch.object(launcher, 'ensure_closed'), patch.object(launcher.sys.stdin, 'isatty', return_value=True), patch.object(launcher, 'import_settings') as settings:
            with patch('builtins.input', return_value='n'): launcher.setup(self.args)
            settings.assert_not_called()
            with patch('builtins.input', return_value='y'): launcher.setup(self.args)
            settings.assert_called_once_with(force_select=True)
        self.assertEqual(self.inventory(), before)

    def test_incomplete_module_fails_before_configuring(self):
        (self.root / 'profile_settings.py').write_text('def import_settings(): pass\n')
        with patch.object(launcher, 'ensure_closed'):
            with self.assertRaisesRegex(RuntimeError, 'Mixed launcher modules'): launcher.setup(self.args)
        self.assertFalse((self.root / 'session.json').exists())

    def test_localized_tasklist_bytes_and_process_failure(self):
        with patch.object(launcher.sys, 'platform', 'win32'), patch.object(launcher.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
            for data, running in [(b'localized \x81\xff response', False), (b'"X4.EXE","123"\x81', True)]:
                with patch.object(launcher.subprocess, 'run', return_value=SimpleNamespace(stdout=data)) as run:
                    if running:
                        with self.assertRaisesRegex(RuntimeError, 'Close X4'): launcher.ensure_closed()
                    else: launcher.ensure_closed()
                    self.assertFalse(run.call_args.kwargs.get('text', False))
            with patch.object(launcher.subprocess, 'run', side_effect=OSError('tasklist unavailable')):
                with self.assertRaises(OSError): launcher.ensure_closed()

    def test_manual_game_selection_keeps_unicode_and_metacharacters_as_data(self):
        game = self.base / 'X4 Zöe $test [1]'; game.mkdir(); exe = game / 'X4.exe'; exe.touch()
        reply = SimpleNamespace(returncode=0, stdout=(str(exe)+'\n').encode(), stderr=b'')
        with patch.object(launcher.subprocess, 'run', return_value=reply) as run:
            self.assertEqual(launcher.choose_game_executable(), str(game))
            self.assertNotIn(str(exe), run.call_args.args[0][-1])
            self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertEqual(launcher.normalize_windows_game('"'+str(exe)+'"'), str(game))
        with self.assertRaises(ValueError): launcher.normalize_windows_game(str(self.base))

    def test_cancelled_windows_game_dialog_leaves_unconfigured_package(self):
        reply = SimpleNamespace(returncode=2, stdout=b'', stderr=b'')
        with patch.object(launcher.sys, 'platform', 'win32'), patch.object(launcher, 'ensure_closed'), patch.object(launcher.subprocess, 'run', return_value=reply):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'): launcher.setup(self.args)
        self.assertEqual(json.loads((self.root / 'package.json').read_text()), self.clean)
        self.assertFalse((self.root / 'session.json').exists())
