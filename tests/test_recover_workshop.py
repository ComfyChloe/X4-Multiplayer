import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/distribution'))
spec = importlib.util.spec_from_file_location('recover_workshop', ROOT / 'tools/distribution/recover_workshop.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
sys.path.pop(0)


class RecoverWorkshop(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'package'; self.root.mkdir()
        self.profile = self.base / 'profiles/45841'; self.profile.mkdir(parents=True)
        (self.profile / 'config.xml').write_bytes(b'config sentinel')
        self.target = self.profile / 'content.xml'
        self.game = self.base / 'game'; self.game.mkdir()
        package = dict(package_id='owned', channel='internal', slot='p2', version='7.2.0')
        (self.root / 'package.json').write_text(json.dumps(package))
        self.state = dict(package_id='owned', version='7.2.0', profile=str(self.profile), game=str(self.game))
        self.state_file = self.root / 'installation.json'; self.state_file.write_text(json.dumps(self.state))
        self.before = b'<content><extension id="ws_123456" enabled="true"/></content>'
        self.after = b'<content><extension id="ws_123456" enabled="false"/></content>'
        self.normal = self.base / 'profiles/23456'; self.normal.mkdir()
        (self.normal / 'content.xml').write_bytes(b'normal content sentinel')
        (self.normal / 'save.xml').write_bytes(b'normal save sentinel')
        self.closed = patch.object(helper, 'ensure_closed'); self.closed.start(); self.addCleanup(self.closed.stop)

    def record(self, before=None, after=None, index=1, ids=None):
        after = self.after if after is None else after
        folder = self.root / 'backups' / ('test-mod-isolation-20261022-%02d' % index)
        folder.mkdir(parents=True)
        if before is None:
            (folder / 'BEFORE-WAS-ABSENT.txt').write_text('absent')
        else:
            (folder / 'content.xml').write_bytes(before)
        record = dict(profile=str(self.profile), before_exists=before is not None,
                      before_sha256=helper.digest(before), after_sha256=helper.digest(after),
                      disabled_ids=ids if ids is not None else ['ws_123456', 'not_workshop'])
        (folder / 'change.json').write_text(json.dumps(record))
        self.target.write_bytes(after)
        return folder

    def test_exact_existing_file_restored_with_current_backup_and_normal_files_unchanged(self):
        self.record(self.before)
        normal_before = {p.name: p.read_bytes() for p in self.normal.iterdir()}
        report, status = helper.recover(self.root, helper.prepare(self.root))
        self.assertEqual(status, 'original_local_selection_restored')
        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertEqual((report.parent / 'content-before-recovery.xml').read_bytes(), self.after)
        self.assertEqual(normal_before, {p.name: p.read_bytes() for p in self.normal.iterdir()})
        self.assertEqual(json.loads((report.parent / 'recovery.json').read_text())['subscription_status'], 'not_checked_or_modified')

    def test_original_absence_restored_and_repeat_is_idempotent(self):
        self.record()
        helper.recover(self.root, helper.prepare(self.root))
        self.assertFalse(self.target.exists())
        _, status = helper.recover(self.root, helper.prepare(self.root))
        self.assertEqual(status, 'already_original')

    def test_game_modified_selection_not_overwritten_but_links_available(self):
        self.record(self.before); self.target.write_bytes(b'<content game="modified"/>')
        report, status = helper.recover(self.root, helper.prepare(self.root))
        self.assertEqual(status, 'manual_review_required')
        self.assertIn('filedetails/?id=123456', report.read_text())
        self.assertEqual(self.target.read_bytes(), b'<content game="modified"/>')

    def test_report_only_preserves_exact_helper_output(self):
        self.record(self.before)
        helper.recover(self.root, helper.prepare(self.root), restore=False)
        self.assertEqual(self.target.read_bytes(), self.after)

    def test_chain_restores_earliest_original(self):
        self.record(self.before)
        self.record(self.after, b'<content second="true"/>', index=2, ids=['ws_654321'])
        plan = helper.prepare(self.root)
        self.assertEqual(plan['ids'], ['123456', '654321'])
        helper.recover(self.root, plan)
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_intervening_user_change_breaks_chain_and_is_not_rolled_back(self):
        self.record(self.before)
        self.record(b'<content user="edit"/>', b'<content second="true"/>', index=2)
        plan = helper.prepare(self.root)
        self.assertEqual(plan['status'], 'manual_review_required')
        helper.recover(self.root, plan)
        self.assertEqual(self.target.read_bytes(), b'<content second="true"/>')

    def test_corrupt_backup_refused_before_output_or_writes(self):
        folder = self.record(self.before)
        (folder / 'content.xml').write_bytes(b'<content changed="true"/>')
        with self.assertRaisesRegex(RuntimeError, 'checksum'): helper.prepare(self.root)
        self.assertFalse((self.root / 'workshop-recovery').exists())
        self.assertEqual(self.target.read_bytes(), self.after)

    def test_unexpected_owner_or_normal_profile_refused(self):
        self.record(self.before)
        for delta in ({'package_id': 'another'}, {'profile': str(self.normal)}):
            self.state_file.write_text(json.dumps(self.state | delta))
            with self.assertRaises(RuntimeError): helper.prepare(self.root)
        self.assertEqual((self.normal / 'content.xml').read_bytes(), b'normal content sentinel')

    def test_no_backup_refused_without_guessing_installed_mods(self):
        with self.assertRaisesRegex(RuntimeError, 'No Fix-1 backups'): helper.prepare(self.root)
        self.assertFalse((self.root / 'workshop-recovery').exists())

    def test_symlink_target_and_output_refused(self):
        self.record(self.before)
        self.target.unlink(); self.target.symlink_to(self.normal / 'content.xml')
        with self.assertRaisesRegex(RuntimeError, 'linked'): helper.prepare(self.root)
        self.target.unlink(); self.target.write_bytes(self.after)
        (self.root / 'workshop-recovery').symlink_to(self.normal, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'linked'): helper.recover(self.root, helper.prepare(self.root))
        self.assertEqual(self.target.read_bytes(), self.after)

    def test_race_between_prepare_and_recover_refused(self):
        self.record(self.before); plan = helper.prepare(self.root)
        self.target.write_bytes(b'user change')
        with self.assertRaisesRegex(RuntimeError, 'changed'): helper.recover(self.root, plan)
        self.assertFalse((self.root / 'workshop-recovery').exists())
        self.assertEqual(self.target.read_bytes(), b'user change')

    def test_failed_atomic_replace_preserves_file_and_evidence(self):
        self.record(self.before)
        with patch.object(helper.os, 'replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError): helper.recover(self.root, helper.prepare(self.root))
        self.assertEqual(self.target.read_bytes(), self.after)
        records = list((self.root / 'workshop-recovery').glob('*/content-before-recovery.xml'))
        self.assertEqual(len(records), 1); self.assertEqual(records[0].read_bytes(), self.after)
        self.assertEqual(list(self.profile.glob('content-recovery-*.tmp')), [])

    def test_valid_affected_ids_only_and_display_name_is_html_escaped(self):
        self.record(ids=['ws_123456', 'ws_123456', 'ws_0', 'ws_18446744073709551616', 'ws_1<script>', 'other'])
        mod = self.game / 'extensions/example'; mod.mkdir(parents=True)
        manifest = b'<content id="ws_123456" name="&lt;script&gt;BAD&lt;/script&gt;"/>'
        (mod / 'content.xml').write_bytes(manifest)
        plan = helper.prepare(self.root); report, _ = helper.recover(self.root, plan)
        self.assertEqual(plan['ids'], ['123456'])
        self.assertNotIn('<script>', report.read_text())
        self.assertIn('&lt;script&gt;', report.read_text())
        self.assertEqual((mod / 'content.xml').read_bytes(), manifest)

    def test_running_game_blocks_rollback_after_report_before_profile_change(self):
        self.record(self.before)
        with patch.object(helper, 'ensure_closed', side_effect=RuntimeError('Close X4')):
            with self.assertRaisesRegex(RuntimeError, 'Close X4'): helper.recover(self.root, helper.prepare(self.root))
        self.assertEqual(self.target.read_bytes(), self.after)
