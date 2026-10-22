import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('isolate_test_mods', ROOT / 'distribution/isolate_test_mods.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class IsolateTestMods(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.root = self.base / 'package'; self.root.mkdir()
        self.game = self.base / 'game'; self.profile = self.base / 'user/45841'; self.profile.mkdir(parents=True)
        (self.profile / 'config.xml').write_text('<root/>')
        (self.root / 'package.json').write_text(json.dumps(dict(channel='internal', slot='p2', version='7.2.0', package_id='owned')))
        self.state = dict(game=str(self.game), profile=str(self.profile), package_id='owned', version='7.2.0')
        self.write_state()
        self.extension(self.game / 'extensions/x4_lan_lab', 'x4_lan_lab')
        (self.game / 'extensions/x4_lan_lab/.x4lan-package-id').write_text('owned')
        self.extension(self.game / 'extensions/official', 'ego_dlc_timelines')
        self.extension(self.game / 'extensions/legacy-ui', 'actual_ui_id')

    def write_state(self):
        (self.root / 'installation.json').write_text(json.dumps(self.state))

    def extension(self, folder, extension_id, compressed=False):
        folder.mkdir(parents=True)
        raw = f'<content id="{extension_id}"/>'.encode()
        (folder / ('content.xml.gz' if compressed else 'content.xml')).write_bytes(gzip.compress(raw) if compressed else raw)

    def selection(self, proposed):
        return {e.get('id'): e.get('enabled') for e in ET.fromstring(proposed[2]).findall('extension')}

    def test_missing_file_preview_does_not_write_and_apply_is_withdrawn(self):
        normal = self.base / 'user/12345'; normal.mkdir(); (normal / 'content.xml').write_bytes(b'normal sentinel')
        proposed = helper.plan(self.root)
        self.assertIsNone(proposed[1]); self.assertFalse((self.profile / 'content.xml').exists())
        self.assertEqual(self.selection(proposed), dict(actual_ui_id='false', ego_dlc_timelines='true', ego_dlc_ventures='false', x4_lan_lab='true'))
        with self.assertRaisesRegex(RuntimeError, 'WITHDRAWN'):
            helper.apply(self.root, proposed)
        self.assertFalse((self.profile / 'content.xml').exists())
        self.assertFalse((self.root / 'backups').exists())
        self.assertEqual((normal / 'content.xml').read_bytes(), b'normal sentinel')
        self.assertEqual(ET.parse(self.game / 'extensions/legacy-ui/content.xml').getroot().get('id'), 'actual_ui_id')

    def test_existing_dlc_choice_preserved_exact_backup_and_duplicate_entries_disabled(self):
        original = b'<content sync="false"><extension id="ego_dlc_timelines" enabled="false"/><extension id="actual_ui_id" enabled="true"/><extension id="actual_ui_id" enabled="true"/></content>'
        (self.profile / 'content.xml').write_bytes(original)
        proposed = helper.plan(self.root); self.assertEqual(self.selection(proposed)['ego_dlc_timelines'], 'false')
        tree = ET.fromstring(proposed[2]); self.assertEqual(tree.get('sync'), 'false')
        self.assertTrue(all(e.get('enabled') == 'false' for e in tree.findall('extension') if e.get('id') == 'actual_ui_id'))
        with self.assertRaisesRegex(RuntimeError, 'WITHDRAWN'):
            helper.apply(self.root, proposed)
        self.assertEqual((self.profile / 'content.xml').read_bytes(), original)

    def test_user_extension_locations_and_compressed_manifest_are_included(self):
        self.extension(self.profile / 'extensions/one', 'user_one')
        self.extension(self.profile.parent / 'extensions/two', 'user_two', compressed=True)
        values = self.selection(helper.plan(self.root))
        self.assertEqual((values['user_one'], values['user_two']), ('false', 'false'))

    def test_wrong_profile_or_owner_refused_before_writes(self):
        for change in ({'profile': str(self.base / 'user/12345')}, {'package_id': 'other'}):
            original = self.state.copy(); self.state.update(change); self.write_state()
            with self.assertRaises(RuntimeError): helper.plan(self.root)
            self.assertFalse((self.root / 'backups').exists())
            self.state = original; self.write_state()
        (self.game / 'extensions/x4_lan_lab/.x4lan-package-id').write_text('other')
        with self.assertRaises(RuntimeError): helper.plan(self.root)

    def test_malformed_manifest_or_profile_refused_before_writes(self):
        manifest = self.game / 'extensions/legacy-ui/content.xml'; original = manifest.read_bytes()
        manifest.write_bytes(b'<broken')
        with self.assertRaises(ET.ParseError): helper.plan(self.root)
        self.assertFalse((self.profile / 'content.xml').exists()); manifest.write_bytes(original)
        (self.profile / 'content.xml').write_bytes(b'<unexpected/>')
        with self.assertRaises(RuntimeError): helper.plan(self.root)
        self.assertFalse((self.root / 'backups').exists())

    def test_profile_changed_after_plan_is_not_overwritten(self):
        proposed = helper.plan(self.root)
        (self.profile / 'content.xml').write_bytes(b'changed')
        with self.assertRaises(RuntimeError): helper.apply(self.root, proposed)
        self.assertEqual((self.profile / 'content.xml').read_bytes(), b'changed')
        self.assertFalse((self.root / 'backups').exists())

    def test_linked_profile_file_refused(self):
        normal = self.base / 'normal.xml'; normal.write_bytes(b'<content/>')
        (self.profile / 'content.xml').symlink_to(normal)
        with self.assertRaises(RuntimeError): helper.plan(self.root)
        self.assertEqual(normal.read_bytes(), b'<content/>')

    def test_running_windows_game_is_refused(self):
        with patch.object(helper.sys, 'platform', 'win32'), patch.object(helper.subprocess, 'CREATE_NO_WINDOW', 0, create=True), patch.object(helper.subprocess, 'run', return_value=Mock(stdout='"X4.exe","1234"')):
            with self.assertRaisesRegex(RuntimeError, 'Close X4'): helper.ensure_closed()
