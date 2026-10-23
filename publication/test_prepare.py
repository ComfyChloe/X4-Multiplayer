"""Focused checks for publication boundaries; no game, network or account changes."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('source_publication', Path(__file__).with_name('prepare.py'))
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)

class PublicationTests(unittest.TestCase):
    def test_excludes_history_reports_instructions_and_binaries(self):
        for path in ('reports/private.txt', 'CONTINUE-HERE.txt', 'AGENTS.md', 'CLAUDE.md',
                     '.github/workflows/publish.yml', 'publication/private.json', *pub.BINARIES):
            with self.subTest(path=path):
                self.assertFalse(pub.selected(path))

    def test_private_state_is_refused_even_under_allowed_source(self):
        for path in ('tests/JOIN-SESSION.json', 'x4lan/.env', 'dev/logs/private.txt',
                     'extension/save/save_010.xml', 'distribution/installation.json',
                     '/x4lan/absolute.py', 'x4lan/../../escape.py', 'x4lan/..\\escape.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                pub.selected(path)

    def test_unknown_file_type_requires_manual_review(self):
        with self.assertRaisesRegex(ValueError, 'Unreviewed'):
            pub.selected('dev/archive.tar')

    def test_secret_and_personal_markers_block_export(self):
        samples = ['ghp_' + 'A' * 36, '-----BEGIN PRIVATE KEY-----',
                   'https://example.invalid/private-coordination-withheld', '/home/developer/private',
                   '192.0.2.109', 'person@example.com', 'https://user:password@example.com',
                   'token = "' + 'a' * 32 + '"']
        for sample in samples:
            with self.subTest(sample=sample[:12]), self.assertRaises(ValueError):
                pub.check_text('sample.txt', sample.encode())

    def test_only_reviewed_test_fixtures_are_substituted(self):
        source = b'address="192.0.2.109"; profile="70000001"'
        self.assertEqual(pub.transform('tests/test_game_adapter.py', source),
                         b'address="192.0.2.109"; profile="70000001"')
        for path in ('x4lan/host.py', 'tests/unreviewed.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                pub.transform(path, source)

    def test_production_source_is_not_rewritten(self):
        data = b'-- inert\nX4LAN_CONFIG = { enabled = false, token = "" }\n'
        self.assertEqual(pub.transform('extension/x4_lan_lab/ui/config.lua', data), data)

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp); (path/'keep.txt').write_text('owned by someone else')
            with self.assertRaisesRegex(ValueError, 'Refusing'):
                pub.export('HEAD', path)
            self.assertEqual((path/'keep.txt').read_text(), 'owned by someone else')

    def test_symlink_is_refused_before_any_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'output'
            replies = [b'a'*40+b'\n', b'7.3.0\n', b'120000 blob '+b'b'*40+b'\tx4lan/link.py\0']
            with patch.object(pub, 'git', side_effect=replies), self.assertRaisesRegex(ValueError, 'Symlink'):
                pub.export('HEAD', path)
            self.assertFalse(path.exists())

if __name__ == '__main__':
    unittest.main()
