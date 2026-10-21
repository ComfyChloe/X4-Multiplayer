"""Windows seed harness: preflight, install, launch, finish and restore with temporary doubles only.

No game, Steam, PowerShell or real profile is touched; the doubles keep the real installer's
refusals (existing mod folder) and Remove's target selection from the CURRENT record.
"""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_seed_windows_test', ROOT / 'dev/native_seed_windows_test.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
import xml.etree.ElementTree as ET

DLC = [('ego_dlc_boron', '900'), ('ego_dlc_ventures', '127')]
SEED = b'pristine seed bytes'
SEED_ID = 'dd3547009bbbc2d0848e8c4fb8abfccb'
TOKEN = 'ab' * 16


class SeedWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.temp.name)
        self.game = d / 'game'; self.target = self.game / 'extensions/x4_lan_lab'
        for name, version in DLC:
            (self.game / 'extensions' / name).mkdir(parents=True)
            (self.game / 'extensions' / name / 'content.xml').write_text(f'<content id="{name}" version="{version}"/>')
        self.documents = d / 'Documents'
        self.profile = self.documents / 'Egosoft/X4/45841'; self.profile.mkdir(parents=True)
        self.write_content(DLC)
        (self.profile / 'config.xml').write_text('<root/>')
        (self.profile / 'x4lan-debug.txt').write_text('older log')
        self.normal = self.documents / 'Egosoft/X4/111/save/save_001.xml.gz'; self.normal.parent.mkdir(parents=True)
        self.normal.write_bytes(b'normal save')
        self.package = d / 'pkg'; self.package.mkdir()
        (self.package / 'package.json').write_text(json.dumps({'package_id': 'lan-x-p2', 'slot': 'p2'}))
        self.old_record = json.dumps({'game': str(self.game), 'profile': str(self.profile), 'steam': 'steam.exe', 'v': 0})
        (self.package / 'installation.json').write_text(self.old_record)
        self.run = d / 'run'; self.make_run()
        self.calls, self.args, self.launched, self.install = [], [], [], 'ok'
        names = ('HERE', 'STATE', 'is_windows', 'x4_running', 'documents_folder', 'git_head', 'setup_ps1')
        self.saved = {k: getattr(harness, k) for k in names}
        self.popen = harness.subprocess.Popen
        harness.HERE = d / 'journal'; harness.STATE = harness.HERE / 'state.json'
        harness.is_windows = lambda: True
        harness.x4_running = lambda: False
        harness.documents_folder = lambda: self.documents
        harness.git_head = lambda: 'abc1234'
        harness.setup_ps1 = self.setup_ps1
        harness.subprocess.Popen = lambda args: self.launched.append(args)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(harness, k, v)
        harness.subprocess.Popen = self.popen
        self.temp.cleanup()

    def write_content(self, rows):
        entries = ''.join(f'<extension id="{name}" enabled="true"/>' for name, _ in rows + [('x4_lan_lab', '')])
        (self.profile / 'content.xml').write_text(f'<content>{entries}</content>')

    def make_run(self):
        version = ET.parse(ROOT / 'extension/x4_lan_lab/content.xml').getroot().get('version')
        manifest = dict(protocol=1, seed_id=SEED_ID, sha256=hashlib.sha256(SEED).hexdigest(), size=len(SEED),
                        game_build='world1-9.00-steam23660954',
                        mod_sha256=harness.shared.production_digest(ROOT / 'extension/x4_lan_lab'),
                        extensions=sorted([{'id': n, 'version': v} for n, v in DLC] + [{'id': 'x4_lan_lab', 'version': version}],
                                          key=lambda r: r['id']), format='x4-xml-gzip')
        session = dict(token=TOKEN, build=manifest['game_build'], bubble=SEED_ID, port=45843, coop=True)
        (self.run / 'received').mkdir(parents=True)
        (self.run / 'manifest.json').write_text(json.dumps(manifest))
        (self.run / 'play-session.json').write_text(json.dumps(session))
        (self.run / 'received/manifest.json').write_text(json.dumps(manifest))
        (self.run / 'received/seed.bin').write_bytes(SEED)
        (self.run / 'receive-result.json').write_text(json.dumps(dict(received_verified=True, sha256=manifest['sha256'])))

    def setup_ps1(self, package_dir, *args):
        self.calls.append(args[1])
        self.args.append(args)
        if args[1] == 'Install':
            if self.install == 'fail':
                raise RuntimeError('install failed')
            if self.install == 'target-first':                  # real order: mod copied, record not yet written
                (self.target / 'ui').mkdir(parents=True)
                (self.target / '.x4lan-package-id').write_text('lan-x-p2')
                raise RuntimeError('install failed before its record')
            if self.install == 'partial':                       # mod and record written, then a failure
                (self.target / 'ui').mkdir(parents=True)
                (self.target / '.x4lan-package-id').write_text('lan-x-p2')
                (package_dir / 'installation.json').write_text(json.dumps({'game': str(self.game), 'profile': str(self.profile)}))
                raise RuntimeError('install failed after publishing')
            if self.target.exists():
                raise RuntimeError('The mod folder already exists.')
            (self.target / 'md').mkdir(parents=True); (self.target / 'ui').mkdir()
            (self.target / 'md/old_package_only.xml').write_text('<mdscript/>')   # stale 7.1.1 file
            (self.target / '.x4lan-package-id').write_text('lan-x-p2')
            (package_dir / 'installation.json').write_text(
                json.dumps({'game': str(self.game), 'profile': str(self.profile), 'steam': 'steam.exe', 'v': 1}))
            (self.profile / 'config.xml').write_text('<root><autosave>false</autosave></root>')
        elif args[1] == 'Remove':
            game = Path(json.loads((package_dir / 'installation.json').read_text())['game'])
            (game / 'archive').mkdir(exist_ok=True)
            (game / 'extensions/x4_lan_lab').rename(game / 'archive/x4_lan_lab')

    def native_log(self, ready=True):
        lines = [f'[X4LAN] SEED_GATE READY seed={SEED_ID}']
        if ready:
            lines += [f'[X4LAN] SEED_PLAY_READY seed={SEED_ID} slot=p2 controlled=true ownAlive=true proxyAlive=true',
                      f'[X4LAN] SEED_PLAY_NATIVE seed={SEED_ID} slot=2 host=0 tradeHost=false cargoHost=false '
                      'questHost=false shuttles=2 markets=2 player=1 p1=2 p2=3']
        (self.profile / 'x4lan-debug.txt').write_text('\n'.join(lines) + '\n')

    def test_preflight_refuses_missing_dlc_existing_mod_and_saves_without_changes(self):
        self.write_content(DLC[:1])                            # Ventures not enabled here
        with self.assertRaises(RuntimeError) as caught:
            harness.preflight(self.run, self.package)
        self.assertIn('ego_dlc_ventures: seed 127, here absent/disabled', str(caught.exception))
        self.write_content(DLC)
        self.target.mkdir(parents=True)
        with self.assertRaises(RuntimeError) as caught:
            harness.install(self.run, self.package, '192.0.2.109')
        self.assertIn('already installed', str(caught.exception))
        self.target.rmdir()
        (self.profile / 'save').mkdir(); (self.profile / 'save/save_003.xml.gz').write_bytes(b'test save')
        with self.assertRaises(RuntimeError):
            harness.install(self.run, self.package, '192.0.2.109')
        self.assertEqual(self.calls, [])
        self.assertFalse(harness.STATE.exists())
        with self.assertRaises(ValueError):
            harness.install(self.run, self.package, 'not-an-ip')

    def test_received_seed_must_be_the_verified_pinned_snapshot(self):
        (self.run / 'received/seed.bin').write_bytes(b'other bytes!!!!!!!!')
        with self.assertRaises(Exception):
            harness.preflight(self.run, self.package)
        self.assertEqual(self.calls, [])

    def test_full_cycle_installs_exact_source_launches_finishes_and_restores(self):
        harness.install(self.run, self.package, '192.0.2.109')
        self.assertEqual(self.args[0], ('-Action', 'Install', '-GamePath', str(self.game), '-ServerIP', '192.0.2.109'),
                         'Install uses the preflight-selected game')
        state = json.loads(harness.STATE.read_text())
        self.assertEqual((state['stage'], state['owned_target']), ('prepared', str(self.target)))
        self.assertFalse((self.target / 'md/old_package_only.xml').exists(), 'stale package files removed')
        self.assertIn('ui/seed_play_probe.lua', (self.target / 'ui.xml').read_text())
        config = (self.target / 'ui/config.lua').read_text()
        for part in (f'seed_id="{SEED_ID}"', 'slot="p2"', 'host="192.0.2.109"', 'seed_verified=true'):
            self.assertIn(part, config)
        staged = Path(state['owned_save'])
        self.assertEqual((staged.name, staged.read_bytes()), ('save_010.xml.gz', SEED))
        with self.assertRaises(RuntimeError):
            harness.launch(False)                           # only at an agreed rendezvous
        harness.launch(True)
        self.assertEqual(self.launched, [['steam.exe'] + harness.GAME_ARGS])
        self.native_log()
        (self.profile / 'save/steam_autocloud.vdf').write_text('steam')   # Steam's own metadata
        harness.finish()
        result = json.loads((self.run / 'windows-result.json').read_text())
        self.assertEqual((result['native_seed_loaded'], result['problems'], result['two_native_acceptance']), (True, [], False))
        self.assertFalse(staged.exists(), 'exact run-owned seed removed after evidence')
        harness.restore()
        self.assertEqual(self.calls, ['Install', 'Remove'])
        self.assertFalse(self.target.exists())
        self.assertEqual((self.package / 'installation.json').read_text(), self.old_record)
        self.assertEqual((self.profile / 'config.xml').read_text(), '<root/>')
        self.assertEqual((self.profile / 'x4lan-debug.txt').read_text(), 'older log')
        self.assertIn('SEED_PLAY_READY', (self.run / 'native.log').read_text(), 'raw native log retained')
        self.assertEqual(self.normal.read_bytes(), b'normal save')
        before = json.loads((self.run / 'saves-before.json').read_text())
        after = json.loads((self.run / 'saves-after.json').read_text())
        self.assertIn(str(self.normal), before, 'the pre-install inventory survives the deleted journal')
        self.assertEqual(before, after, 'same saves, bytes and timestamps after restore')
        summary = json.loads((self.run / 'windows-restore.json').read_text())
        self.assertEqual(summary['save_inventories'], ['saves-before.json', 'saves-after.json'])
        self.assertFalse(harness.STATE.exists())

    def test_evidence_bundle_is_sanitized_complete_and_only_after_restore(self):
        harness.install(self.run, self.package, '192.0.2.109')
        harness.launch(True)
        self.native_log()
        with open(self.profile / 'x4lan-debug.txt', 'a') as log:
            log.write(f'lease {"c" * 32} key {TOKEN} at {Path.home()}\\X4\n')
        harness.finish()
        with self.assertRaises(RuntimeError):
            harness.bundle(self.run)                        # no restore summary yet
        harness.restore()
        path = harness.bundle(self.run)
        raw = path.read_text()
        evidence = json.loads(raw)
        for secret in (TOKEN, 'c' * 32, str(Path.home()), json.dumps(str(Path.home()))[1:-1]):
            self.assertNotIn(secret, raw)
        self.assertIn(SEED_ID, raw, 'the seed id is kept')
        self.assertIn('SEED_PLAY_READY', evidence['native_log_sanitized'])
        self.assertEqual(evidence['native_log_sha256'], hashlib.sha256((self.run / 'native.log').read_bytes()).hexdigest())
        keys = set(evidence['saves_before'])
        self.assertTrue(keys and all(k.startswith('profile-') and '111' not in k for k in keys), keys)
        self.assertEqual(evidence['saves_before'], evidence['saves_after'])
        restoration = evidence['restoration_hashes']
        self.assertTrue(all(v['restored_sha256'] == v['backup_sha256'] for v in restoration['profile'].values()))
        self.assertEqual(restoration['package_record']['restored_sha256'], restoration['package_record']['backup_sha256'])
        self.assertEqual(evidence['inventories']['installed_after_overlay'], json.loads(
            (self.run / 'windows-install.json').read_text())['overlaid'])
        self.assertNotIn('token', json.dumps(evidence['install_record']))

    def test_bundle_keeps_run_time_evidence_after_later_changes(self):
        # A:primary review of 3e405cc: the bundle must not describe a later checkout or later files.
        harness.install(self.run, self.package, '192.0.2.109')
        harness.launch(True)
        self.native_log()
        harness.finish()
        harness.restore()
        recorded = json.loads((self.run / 'windows-install.json').read_text())['inventories']
        restored = json.loads((self.run / 'windows-restore.json').read_text())['restoration_hashes']
        later = self.d / 'later-tree'; (later / 'ui').mkdir(parents=True)
        (later / 'ui/later_only.lua').write_text('-- a file the run never had')
        saved_source = harness.SOURCE
        try:
            harness.SOURCE = later                          # the checkout moves on after the run
            (self.profile / 'config.xml').write_text('<root><changed/></root>')
            (self.package / 'installation.json').write_text('{"changed": true}')
            evidence = json.loads(harness.bundle(self.run).read_text())
        finally:
            harness.SOURCE = saved_source
        self.assertEqual(evidence['inventories']['recorded_at_install'], recorded)
        self.assertNotIn('ui/later_only.lua', json.dumps(evidence['inventories']))
        self.assertIn('ui/seed_play_probe.lua', recorded['probes'])
        self.assertEqual(evidence['restoration_hashes'], restored, 'restoration hashes from restore time')

    def test_bundle_labels_evidence_a_run_never_recorded(self):
        harness.install(self.run, self.package, '192.0.2.109')
        harness.launch(True)
        self.native_log()
        harness.finish()
        harness.restore()
        for name, key in (('windows-install.json', 'inventories'), ('windows-restore.json', 'restoration_hashes')):
            path = self.run / name
            data = json.loads(path.read_text()); data.pop(key); path.write_text(json.dumps(data))   # older harness
        evidence = json.loads(harness.bundle(self.run).read_text())
        self.assertEqual(evidence['inventories']['recorded_at_install'], harness.UNAVAILABLE)
        self.assertEqual(evidence['restoration_hashes'], harness.UNAVAILABLE)

    def test_failed_readiness_keeps_the_seed_for_audit(self):
        harness.install(self.run, self.package, '192.0.2.109')
        harness.launch(True)
        self.native_log(ready=False)
        harness.finish()
        result = json.loads((self.run / 'windows-result.json').read_text())
        self.assertFalse(result['native_seed_loaded'])
        self.assertTrue(any('readiness' in p for p in result['problems']))
        self.assertTrue(Path(json.loads(harness.STATE.read_text())['owned_save']).exists())
        harness.restore()                                   # exact bytes: the run-owned seed is removed here
        self.assertFalse((self.profile / 'save/save_010.xml.gz').exists())

    def test_refused_install_removes_nothing(self):
        self.install = 'fail'
        with self.assertRaises(RuntimeError):
            harness.install(self.run, self.package, '192.0.2.109')
        self.assertEqual(json.loads(harness.STATE.read_text())['stage'], 'install-failed')
        harness.restore()
        self.assertEqual(self.calls, ['Install'])
        self.assertEqual((self.package / 'installation.json').read_text(), self.old_record)
        summary = json.loads((self.run / 'windows-restore.json').read_text())
        self.assertEqual(summary['install_outcome'], 'no install effects found', 'early refusal')

    def test_partial_install_keeps_the_unproven_mod_record_and_journal_until_resolved(self):
        self.install = 'partial'
        with self.assertRaises(RuntimeError):
            harness.install(self.run, self.package, '192.0.2.109')
        state = json.loads(harness.STATE.read_text())
        self.assertEqual((state['stage'], state['owned_target']), ('install-failed', None))
        new_record = (self.package / 'installation.json').read_text()
        with self.assertRaises(RuntimeError) as caught:
            harness.restore()
        self.assertIn('outcome unproven', str(caught.exception))
        self.assertEqual(self.calls, ['Install'], 'an unproven mod is never removed')
        self.assertTrue(self.target.exists())
        self.assertEqual((self.package / 'installation.json').read_text(), new_record, 'record kept for 5-REMOVE-MOD')
        self.assertTrue(harness.STATE.exists(), 'journal kept')
        summary = json.loads((self.run / 'windows-restore.json').read_text())
        self.assertEqual((summary['mod'], summary['installation_json']), ('unproven, kept', 'kept: Install outcome unproven'))
        self.assertEqual(summary['install_outcome'], 'partial: mod folder and new record')
        self.assertEqual((self.profile / 'config.xml').read_text(), '<root/>', 'profile files still restored')
        self.target.rename(self.game / 'removed-by-5-REMOVE-MOD')  # the tester resolves it with the package
        harness.restore()
        self.assertEqual((self.package / 'installation.json').read_text(), self.old_record)
        self.assertFalse(harness.STATE.exists())

    def test_target_created_before_the_record_is_kept_with_the_journal(self):
        self.install = 'target-first'
        with self.assertRaises(RuntimeError):
            harness.install(self.run, self.package, '192.0.2.109')
        with self.assertRaises(RuntimeError) as caught:
            harness.restore()
        self.assertIn('outcome unproven', str(caught.exception))
        self.assertEqual(self.calls, ['Install'])
        self.assertTrue(self.target.exists() and harness.STATE.exists())
        self.assertEqual((self.package / 'installation.json').read_text(), self.old_record, 'record untouched')
        summary = json.loads((self.run / 'windows-restore.json').read_text())
        self.assertEqual(summary['install_outcome'], 'partial: mod folder without a new record')

    def test_install_record_naming_another_game_fails_closed(self):
        def install_elsewhere(package_dir, *args):
            self.calls.append(args[1])
            other = self.d / 'other-game/extensions/x4_lan_lab'; (other / 'ui').mkdir(parents=True)
            (other / '.x4lan-package-id').write_text('lan-x-p2')
            (package_dir / 'installation.json').write_text(
                json.dumps({'game': str(self.d / 'other-game'), 'profile': str(self.profile)}))
        harness.setup_ps1 = install_elsewhere
        with self.assertRaises(RuntimeError):
            harness.install(self.run, self.package, '192.0.2.109')
        state = json.loads(harness.STATE.read_text())
        self.assertEqual((state['stage'], state['owned_target']), ('backed-up', None), 'never claimed as run-owned')
        with self.assertRaises(RuntimeError):
            harness.restore()                               # the mod named by the current record stays
        self.assertTrue((self.d / 'other-game/extensions/x4_lan_lab').exists())

    def test_remove_is_not_delegated_for_another_record_and_the_record_is_kept(self):
        harness.install(self.run, self.package, '192.0.2.109')
        other_game = self.d / 'other'; other = other_game / 'extensions/x4_lan_lab'
        other.mkdir(parents=True); (other / '.x4lan-package-id').write_text('lan-x-p2')
        changed = json.dumps({'game': str(other_game), 'profile': str(self.profile), 'steam': 'steam.exe'})
        (self.package / 'installation.json').write_text(changed)
        with self.assertRaises(RuntimeError):
            harness.restore()
        self.assertEqual(self.calls, ['Install'])
        self.assertTrue(other.exists() and self.target.exists())
        self.assertEqual((self.package / 'installation.json').read_text(), changed)
        self.assertTrue(harness.STATE.exists())

    def test_changed_staged_seed_is_never_removed_or_launched(self):
        harness.install(self.run, self.package, '192.0.2.109')
        staged = Path(json.loads(harness.STATE.read_text())['owned_save'])
        staged.write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            harness.launch(True)
        self.assertEqual(self.launched, [])
        with self.assertRaises(RuntimeError):
            harness.restore()
        self.assertEqual(staged.read_bytes(), b'changed')

    def test_journal_is_replaced_atomically(self):
        harness.install(self.run, self.package, '192.0.2.109')
        self.assertFalse(harness.STATE.with_name('state.json.tmp').exists())
        self.assertEqual(json.loads(harness.STATE.read_text())['stage'], 'prepared')


if __name__ == '__main__':
    unittest.main()
