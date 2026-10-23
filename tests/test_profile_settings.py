import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('profile_settings', ROOT / 'distribution/profile_settings.py')
settings = importlib.util.module_from_spec(spec); spec.loader.exec_module(settings)


class ProfileSettings(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.package = self.base / 'package'; self.package.mkdir()
        self.target = self.base / 'OneDrive/Documents/Egosoft/X4/45841'; self.target.mkdir(parents=True)
        self.source = self.target.parent / '12345'; self.source.mkdir()
        self.game = self.base / 'game'; mod = self.game / 'extensions/x4_lan_lab'; mod.mkdir(parents=True)
        (mod / '.x4lan-package-id').write_text('owned')
        (self.package / 'package.json').write_text(json.dumps(dict(channel='internal', package_id='owned')))
        (self.package / 'installation.json').write_text(json.dumps(dict(package_id='owned', profile=str(self.target), game=str(self.game))))
        (self.target / 'config.xml').write_bytes(b'<root><autosave>false</autosave><uisafemode>false</uisafemode><res_width>1920</res_width></root>')
        (self.target / 'inputmap.xml').write_bytes(b'<old/>')
        (self.target / 'content.xml').write_bytes(b'<content><extension id="other" enabled="false"/></content>')
        (self.source / 'config.xml').write_bytes(b'<root><res_width>5120</res_width><res_height>1440</res_height><fullscreen>false</fullscreen><borderless>true</borderless><shaderquality>3</shaderquality><soundvolume>0.31</soundvolume><autosave>true</autosave><uisafemode>true</uisafemode></root>')
        (self.source / 'inputmap.xml').write_bytes(b'<?xml version="1.0"?><root><binding key="custom"/></root>')
        (self.source / 'inputmap_1.xml').write_bytes(b'<root><controller name="HOTAS"/></root>')
        (self.source / 'content.xml').write_bytes(b'<content><extension id="other" enabled="true"/></content>')
        (self.source / 'uidata.xml').write_bytes(b'not settings')
        (self.source / 'save').mkdir(); (self.source / 'save/normal.xml.gz').write_bytes(b'normal save')

    def test_preferences_copied_without_saves_or_mod_selection_or_source_changes(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.source.rglob('*') if p.is_file()}
        mod_selection = (self.target / 'content.xml').read_bytes(); old = (self.target / 'config.xml').read_bytes()
        record = settings.import_settings(self.package)
        config = ET.parse(self.target / 'config.xml').getroot()
        for key, value in [('res_width','5120'),('res_height','1440'),('fullscreen','false'),('borderless','true'),('shaderquality','3'),('soundvolume','0.31'),('autosave','false'),('uisafemode','false')]:
            self.assertEqual(config.findtext(key), value)
        for name in ('inputmap.xml','inputmap_1.xml'):
            self.assertEqual((self.target / name).read_bytes(), (self.source / name).read_bytes())
        self.assertEqual((self.target / 'content.xml').read_bytes(), mod_selection)
        self.assertFalse((self.target / 'save').exists()); self.assertFalse((self.target / 'uidata.xml').exists())
        self.assertEqual((Path(record['backup']) / 'config.xml').read_bytes(), old)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)

    def test_background_mode_overrides_only_test_copy(self):
        config = self.source / 'config.xml'
        config.write_text('<root><fullscreen>true</fullscreen><borderless>false</borderless><res_width>5120</res_width><res_height>1440</res_height><texturequality>high</texturequality></root>')
        before = config.read_bytes()
        settings.import_settings(self.package)
        tree = ET.parse(self.target / 'config.xml').getroot()
        self.assertEqual(tree.findtext('fullscreen'), 'false')
        self.assertEqual(tree.findtext('borderless'), 'true')
        self.assertEqual(tree.findtext('res_width'), '5120')
        self.assertEqual(tree.findtext('res_height'), '1440')
        self.assertEqual(tree.findtext('texturequality'), 'high')
        self.assertEqual(config.read_bytes(), before)

    def test_multiple_profiles_require_selection_and_test_profiles_are_excluded(self):
        other = self.source.parent / '99999'; other.mkdir(); (other / 'config.xml').write_text('<root><res_width>3440</res_width></root>')
        public = self.source.parent / '45842'; public.mkdir(); (public / 'config.xml').write_text('<root/>')
        self.assertEqual(settings.candidates(self.target), [self.source, other])
        record = settings.import_settings(self.package, ask=lambda _: '2')
        self.assertEqual(record['source'], str(other))
        self.assertEqual(ET.parse(self.target / 'config.xml').getroot().findtext('res_width'), '3440')

    def test_keep_option_does_not_modify_test_preferences(self):
        old = (self.target / 'config.xml').read_bytes()
        record = settings.import_settings(self.package, keep=True)
        self.assertEqual((self.target / 'config.xml').read_bytes(), old); self.assertEqual(record['files'], [])

    def test_malformed_input_fails_before_any_preferences_are_written(self):
        old = (self.target / 'config.xml').read_bytes(); (self.source / 'inputmap.xml').write_bytes(b'<invalid')
        with self.assertRaisesRegex(ValueError, 'Invalid XML'): settings.import_settings(self.package)
        self.assertEqual((self.target / 'config.xml').read_bytes(), old)
        self.assertFalse((self.package / 'settings-import.json').exists())

    def test_saved_target_or_wrong_installation_is_refused(self):
        (self.target / 'save').mkdir(); (self.target / 'save/slot.xml.gz').write_bytes(b'save')
        with self.assertRaisesRegex(RuntimeError, 'contains saves'): settings.import_settings(self.package)
        (self.game / 'extensions/x4_lan_lab/.x4lan-package-id').write_text('foreign')
        with self.assertRaisesRegex(RuntimeError, 'another package'): settings.import_settings(self.package)

    def test_failed_multi_file_import_restores_all_previous_bytes_and_removes_new_files(self):
        old = {p.name:p.read_bytes() for p in self.target.iterdir()}; real_write = settings.atomic_write
        def injected(path, data):
            if path.name == 'settings-import.json': raise OSError('injected journal failure')
            real_write(path, data)
        with patch.object(settings, 'atomic_write', side_effect=injected):
            with self.assertRaisesRegex(OSError, 'injected'): settings.import_settings(self.package)
        self.assertEqual({p.name:p.read_bytes() for p in self.target.iterdir()}, old)
        self.assertFalse((self.package / 'settings-import.json').exists())

    def test_linked_settings_and_test_source_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'normal game profile'): settings.import_settings(self.package, source=self.target)
        (self.target / 'inputmap.xml').unlink(); (self.target / 'inputmap.xml').symlink_to(self.source / 'inputmap.xml')
        with self.assertRaisesRegex(RuntimeError, 'linked test settings'): settings.import_settings(self.package)

    def test_no_normal_profile_keeps_current_configuration(self):
        (self.source / 'config.xml').unlink(); old = (self.target / 'config.xml').read_bytes()
        record = settings.import_settings(self.package)
        self.assertIsNone(record['source']); self.assertEqual((self.target / 'config.xml').read_bytes(), old)
