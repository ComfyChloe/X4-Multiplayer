"""Native damage harness: schedule, overlay, refusal and evidence logic with temporary fixtures only."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_damage_test', ROOT / 'dev/native_damage_test.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def probe_line(reason, detail, run='r1', frame=1):
    return (f'[X4LAN] DAMAGE_PROBE run={run} seq=1 frame={frame} rt=1 gt=1 slot=p2 reason={reason} detail={detail} paused=false '
            'auth=1:p1/1,p2/1 wrev=1 ship1=50.00/50.00 ship2=100.00/100.00 target1=none target2=none shots=0,0 '
            'trigger1=0,0 trigger2=0,0 nav=-,- fire2_1=[-] fire2_2=[-] mode=legacy\n')


class ScheduleTests(unittest.TestCase):
    def test_phases_cover_identical_different_rapid_and_settle(self):
        names = [p[0] for p in harness.schedule()]
        self.assertEqual(names[0], 'settle')
        for phase in ('identical', 'different', 'rapid', 'burst', 'final'):
            self.assertIn(phase, names)
        self.assertEqual(harness.health_at(0, 0), ('settle', (100.0, 100.0)))
        self.assertEqual(harness.health_at(harness.SETTLE + 1, 0), ('identical', (50.0, 50.0)))
        self.assertEqual(harness.health_at(harness.SETTLE + 6.5, 0), ('different', (20.0, 20.0)))
        self.assertEqual(harness.health_at(harness.SETTLE + 8.5, 0), ('different', (90.0, 90.0)))
        rapid = harness.SETTLE + 12.5
        self.assertNotEqual(harness.health_at(rapid, 1)[1], harness.health_at(rapid, 2)[1], 'new health every tick')
        self.assertEqual(harness.health_at(harness.total_seconds() + 1, 0)[0], 'done')

    def test_world_frame_keeps_guest_ship_and_targets_unharmed(self):
        fields = harness.world_fields((20.0, 30.0))
        self.assertEqual(len(fields), 17)
        self.assertEqual(fields[:2], [20.0, 30.0])
        self.assertEqual(fields[3:5], [100.0, 100.0])
        self.assertTrue(all(v > 1.01 for v in (fields[0], fields[3])), 'never a death snapshot')


class OverlayAndRefusalTests(unittest.TestCase):
    def test_overlay_adds_probe_once_after_coop_game(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            (target / 'ui').mkdir(); (target / 'md').mkdir()
            (target / 'ui.xml').write_text('<files>\n    <file name="ui/coop_game.lua" />\n    <file name="ui/universe_game.lua" />\n</files>\n')
            harness.overlay(target); harness.overlay(target)
            text = (target / 'ui.xml').read_text()
            self.assertEqual(text.count('ui/damage_probe.lua'), 1)
            self.assertLess(text.index('coop_game.lua'), text.index('damage_probe.lua'))
            self.assertLess(text.index('damage_probe.lua'), text.index('universe_game.lua'))
            self.assertTrue((target / 'ui/damage_probe.lua').is_file() and (target / 'md/damage_probe.xml').is_file())

    def test_overlay_refuses_unknown_layout(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d); (target / 'ui').mkdir(); (target / 'md').mkdir()
            (target / 'ui.xml').write_text('<files/>')
            with self.assertRaises(RuntimeError):
                harness.overlay(target)

    def test_only_the_isolated_profile_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            package = Path(d)
            (package / 'installation.json').write_text(json.dumps({'game': 'G', 'profile': str(Path(d) / '12345678')}))
            with self.assertRaises(RuntimeError):
                harness.windows_paths(package)
            (package / 'installation.json').write_text(json.dumps({'game': 'G', 'profile': str(Path(d) / '45841')}))
            self.assertEqual(harness.windows_paths(package)[1].name, '45841')

    def test_save_check_tolerates_only_steam_metadata_timestamps(self):
        save = r'C:\X4\259869033\save\save_001.xml.gz'
        meta = r'C:\X4\259869033\save\steam_autocloud.vdf'
        before = {save: {'sha256': 'a', 'mtime_ns': 1}, meta: {'sha256': 'm', 'mtime_ns': 1}}
        self.assertEqual(harness.saves_changed(before, {save: {'sha256': 'a', 'mtime_ns': 1}, meta: {'sha256': 'm', 'mtime_ns': 9}}), [])
        self.assertEqual(harness.saves_changed(before, {save: {'sha256': 'a', 'mtime_ns': 1}, meta: {'sha256': 'x', 'mtime_ns': 9}}), [meta])
        self.assertEqual(harness.saves_changed(before, {save: {'sha256': 'a', 'mtime_ns': 2}, meta: {'sha256': 'm', 'mtime_ns': 1}}), [save])
        self.assertEqual(harness.saves_changed(before, {meta: {'sha256': 'm', 'mtime_ns': 1}}), [save])

    def test_local_address_is_never_loopback(self):
        try:
            address = harness.local_ipv4()
        except (OSError, RuntimeError):
            self.skipTest('no network route on this machine')
        self.assertFalse(address.startswith('127.'))


class PrepareSafetyTests(unittest.TestCase):
    @unittest.skipUnless(harness.os.name == 'nt', 'Windows Known Folder API')
    def test_documents_come_from_the_known_folder_api(self):
        self.assertTrue(harness.documents_folder().is_dir())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.temp.name)
        self.package = d / 'pkg'; self.package.mkdir()
        (self.package / 'package.json').write_text(json.dumps({'package_id': 'lan-x-p2', 'slot': 'p2'}))
        (self.package / 'VERSION.txt').write_text('7.1.1')
        self.documents = d / 'Documents'
        self.profile = self.documents / 'Egosoft/X4/45841'; self.profile.mkdir(parents=True)
        (self.profile / 'config.xml').write_text('<root/>')
        self.save = self.documents / 'Egosoft/X4/111/save/save_001.xml.gz'; self.save.parent.mkdir(parents=True)
        self.save.write_bytes(b'normal save')
        self.game = d / 'game'; self.target = self.game / 'extensions/x4_lan_lab'
        self.calls, self.args = [], []
        self.saved = {k: getattr(harness, k) for k in ('HERE', 'STATE', 'git_head', 'x4_running', 'documents_folder',
                                                       'local_ipv4', 'setup_ps1', 'is_windows')}
        harness.HERE = d / 'evidence'; harness.STATE = harness.HERE / 'state.json'
        harness.git_head = lambda: 'abc1234def'
        harness.x4_running = lambda: False
        harness.documents_folder = lambda: self.documents
        harness.local_ipv4 = lambda: '192.168.1.5'
        harness.is_windows = lambda: True
        harness.setup_ps1 = self.setup_ps1
        self.install = 'ok'

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(harness, k, v)
        self.temp.cleanup()

    def record(self):
        return json.dumps({'game': str(self.game), 'profile': str(self.profile)})

    def make_mod(self, package_id='lan-x-p2'):
        (self.target / 'ui').mkdir(parents=True); (self.target / 'md').mkdir()
        (self.target / 'ui.xml').write_text('<files>\n <file name="ui/coop_game.lua" />\n</files>\n')
        (self.target / '.x4lan-package-id').write_text(package_id)

    def setup_ps1(self, package_dir, *args):
        """Mock of the package setup.ps1 with the real installer's refusal of an existing mod folder."""
        self.calls.append(args[1])
        self.args.append(args)
        if args[1] == 'Install':
            if self.install == 'target-first':                  # real order: mod copied, record not yet written
                self.make_mod()
                raise RuntimeError('install failed before its record')
            if self.install == 'fail-after-record':
                (package_dir / 'installation.json').write_text('{}')
                raise RuntimeError('install failed')
            if self.install == 'partial':                       # mod and record written, then a failure
                self.make_mod()
                (package_dir / 'installation.json').write_text(self.record())
                raise RuntimeError('install failed after publishing')
            if self.target.exists():
                raise RuntimeError('The mod folder already exists.')
            self.make_mod()
            (package_dir / 'installation.json').write_text(self.record())
            (self.profile / 'config.xml').write_text('<root><autosave>false</autosave></root>')
        elif args[1] == 'Remove':
            game = Path(json.loads((package_dir / 'installation.json').read_text())['game'])
            (game / 'archive').mkdir(parents=True, exist_ok=True)
            (game / 'extensions/x4_lan_lab').rename(game / 'archive/x4_lan_lab')

    def test_failed_install_leaves_restorable_state_sampled_before_install(self):
        self.install = 'fail-after-record'
        with self.assertRaises(RuntimeError):
            harness.prepare(self.package, self.game)
        state = json.loads(harness.STATE.read_text())
        self.assertEqual(state['stage'], 'install-failed')
        self.assertIsNone(state['owned_target'])
        self.assertFalse(state['had_installation_json'], 'sampled before Install created the record')
        with self.assertRaises(RuntimeError):
            harness.run(1)                                      # an incomplete prepare never runs
        harness.restore()
        self.assertEqual(self.calls, ['Install'], 'nothing is removed after a failed Install')
        self.assertFalse((self.package / 'installation.json').exists())
        self.assertFalse(harness.STATE.exists())

    def test_existing_mod_named_by_an_earlier_record_is_refused_before_any_change(self):
        self.make_mod()
        (self.package / 'installation.json').write_text(self.record())
        with self.assertRaises(RuntimeError) as caught:
            harness.prepare(self.package, self.game)
        self.assertIn('already installed', str(caught.exception))
        self.assertEqual(self.calls, [])
        self.assertFalse(harness.HERE.exists(), 'no journal, no backup folder')

    def test_preexisting_mod_at_the_pinned_game_is_refused_before_any_change(self):
        self.make_mod()                                         # e.g. installed from another copy of the package
        marker = (self.target / '.x4lan-package-id').read_bytes()
        with self.assertRaises(RuntimeError) as caught:
            harness.prepare(self.package, self.game)
        self.assertIn('already installed', str(caught.exception))
        self.assertEqual(self.calls, [])
        self.assertFalse(harness.HERE.exists(), 'no journal, no backup folder')
        self.assertEqual((self.target / '.x4lan-package-id').read_bytes(), marker, 'pre-existing mod untouched')

    def test_game_is_pinned_before_any_change_and_passed_to_install(self):
        with self.assertRaises(RuntimeError) as caught:
            harness.prepare(self.package)                       # no earlier record and no --game
        self.assertIn('--game', str(caught.exception))
        (self.package / 'installation.json').write_text(self.record())
        with self.assertRaises(RuntimeError):
            harness.prepare(self.package, self.d / 'other-game')  # contradicts the earlier record
        self.assertEqual(self.calls, [])
        self.assertFalse(harness.HERE.exists())
        harness.prepare(self.package)                           # the earlier record names the game
        self.assertEqual(self.args[0], ('-Action', 'Install', '-GamePath', str(self.game.resolve()),
                                        '-ServerIP', '192.168.1.5'))

    def test_folder_before_record_crash_keeps_mod_record_and_journal(self):
        self.install = 'target-first'
        with self.assertRaises(RuntimeError):
            harness.prepare(self.package, self.game)
        state = json.loads(harness.STATE.read_text())
        self.assertEqual((state['stage'], state['owned_target'], state['game']),
                         ('install-failed', None, str(self.game.resolve())), 'game journaled before Install')
        with self.assertRaises(RuntimeError) as caught:
            harness.restore()
        self.assertIn('outcome unproven', str(caught.exception))
        self.assertEqual(self.calls, ['Install'], 'an unproven mod is never removed')
        self.assertTrue(self.target.exists() and harness.STATE.exists())
        self.assertFalse((self.package / 'installation.json').exists(), 'no record was ever written')
        summary = json.loads((Path(state['run']) / 'restore.json').read_text())
        self.assertEqual(summary['mod'], 'unproven, kept')
        self.target.rename(self.game / 'removed-by-5-REMOVE-MOD')  # the tester resolves it with the package
        harness.restore()
        self.assertFalse(harness.STATE.exists())

    def test_install_recording_another_game_fails_closed(self):
        def install_elsewhere(package_dir, *args):
            self.calls.append(args[1])
            other = self.d / 'other-game/extensions/x4_lan_lab'; (other / 'ui').mkdir(parents=True)
            (other / '.x4lan-package-id').write_text('lan-x-p2')
            (package_dir / 'installation.json').write_text(
                json.dumps({'game': str(self.d / 'other-game'), 'profile': str(self.profile)}))
        harness.setup_ps1 = install_elsewhere
        with self.assertRaises(RuntimeError):
            harness.prepare(self.package, self.game)
        state = json.loads(harness.STATE.read_text())
        self.assertEqual((state['stage'], state['owned_target']), ('backed-up', None), 'never claimed as run-owned')
        with self.assertRaises(RuntimeError):
            harness.restore()                                   # the mod named by the current record stays
        self.assertTrue((self.d / 'other-game/extensions/x4_lan_lab').exists())

    def test_run_owned_mod_is_removed_and_everything_restored(self):
        old = json.dumps({'game': str(self.game), 'profile': str(self.profile), 'host': 'old'})
        (self.package / 'installation.json').write_text(old)
        harness.prepare(self.package, self.game)
        state = json.loads(harness.STATE.read_text())
        self.assertEqual((state['stage'], state['owned_target']), ('prepared', str(self.target)))
        self.assertFalse(harness.STATE.with_name('state.json.tmp').exists(), 'atomic journal leaves no temp file')
        self.assertTrue((self.target / 'ui/damage_probe.lua').is_file())
        harness.restore()
        self.assertEqual(self.calls, ['Install', 'Remove'])
        self.assertFalse(self.target.exists())
        self.assertEqual((self.package / 'installation.json').read_text(), old)
        self.assertEqual((self.profile / 'config.xml').read_text(), '<root/>')
        summary = json.loads((Path(state['run']) / 'restore.json').read_text())
        self.assertEqual((summary['mod'], summary['saves_changed'], summary['problems']),
                         ('removed by the package setup.ps1', 0, []))
        before = json.loads((Path(state['run']) / 'saves-before.json').read_text())
        after = json.loads((Path(state['run']) / 'saves-after.json').read_text())
        self.assertIn(str(self.save), before, 'the pre-install inventory survives the deleted journal')
        self.assertEqual(before, after, 'same saves, bytes and timestamps after restore')
        self.assertEqual(summary['save_inventories'], ['saves-before.json', 'saves-after.json'])
        self.assertFalse(harness.STATE.exists())

    def test_changed_run_owned_mod_is_not_removed_and_its_record_is_kept(self):
        harness.prepare(self.package, self.game)
        record = (self.package / 'installation.json').read_bytes()
        (self.target / 'ui.xml').write_text('<files/>')       # no longer what this run installed
        with self.assertRaises(RuntimeError):
            harness.restore()
        self.assertEqual(self.calls, ['Install'])
        self.assertTrue(self.target.exists())
        self.assertTrue(harness.STATE.exists(), 'journal kept for a manual decision')
        self.assertEqual((self.package / 'installation.json').read_bytes(), record,
                         'the record stays so the package Remove can still recover the mod')
        summary = json.loads((Path(json.loads(harness.STATE.read_text())['run']) / 'restore.json').read_text())
        self.assertEqual(summary['installation_json'], 'kept: run-owned mod unresolved')

    def test_remove_is_not_delegated_when_the_record_names_another_installation(self):
        harness.prepare(self.package, self.game)
        other_game = self.d / 'other-game'; other = other_game / 'extensions/x4_lan_lab'
        (other / 'ui').mkdir(parents=True); (other / '.x4lan-package-id').write_text('lan-x-p2')   # same marker
        changed = json.dumps({'game': str(other_game), 'profile': str(self.profile)})
        (self.package / 'installation.json').write_text(changed)
        with self.assertRaises(RuntimeError) as caught:
            harness.restore()
        self.assertIn('not the record this run installed', str(caught.exception))
        self.assertEqual(self.calls, ['Install'], 'Remove would have archived the other installation')
        self.assertTrue(other.exists() and self.target.exists())
        self.assertEqual((self.package / 'installation.json').read_text(), changed, 'records untouched')
        self.assertTrue(harness.STATE.exists())

    def test_unknown_install_outcome_removes_nothing_and_keeps_the_new_record(self):
        harness.prepare(self.package, self.game)
        state = json.loads(harness.STATE.read_text())
        state.update(stage='backed-up', owned_target=None)    # as if the harness died inside Install
        harness.write_state(state)
        with self.assertRaises(RuntimeError) as caught:
            harness.restore()
        self.assertIn('outcome unproven', str(caught.exception))
        self.assertEqual(self.calls, ['Install'])
        self.assertTrue(self.target.exists() and (self.package / 'installation.json').exists())
        self.assertEqual((self.profile / 'config.xml').read_text(), '<root/>', 'profile files still restored')

    def test_partial_install_keeps_the_unproven_mod_record_and_journal_until_resolved(self):
        self.install = 'partial'
        with self.assertRaises(RuntimeError):
            harness.prepare(self.package, self.game)
        state = json.loads(harness.STATE.read_text())
        self.assertEqual((state['stage'], state['owned_target']), ('install-failed', None))
        with self.assertRaises(RuntimeError) as caught:
            harness.restore()
        self.assertIn('outcome unproven', str(caught.exception))
        self.assertEqual(self.calls, ['Install'], 'an unproven mod is never removed')
        self.assertTrue(self.target.exists())
        self.assertEqual((self.package / 'installation.json').read_text(), self.record(), 'record kept for 5-REMOVE-MOD')
        self.assertTrue(harness.STATE.exists(), 'journal kept')
        summary = json.loads((Path(state['run']) / 'restore.json').read_text())
        self.assertEqual((summary['mod'], summary['installation_json']), ('unproven, kept', 'kept: Install outcome unproven'))
        self.target.rename(self.game / 'removed-by-5-REMOVE-MOD')  # the tester resolves it with the package
        harness.restore()
        self.assertFalse((self.package / 'installation.json').exists(), 'absent before the run, absent again')
        self.assertFalse(harness.STATE.exists())

    def test_changed_save_fails_restore(self):
        harness.prepare(self.package, self.game)
        self.save.write_bytes(b'changed')
        with self.assertRaises(RuntimeError) as caught:
            harness.restore()
        self.assertIn('saves changed', str(caught.exception))


def req(n, rev, hull, frame):
    v = f'{hull:.2f}/{hull:.2f}'
    return probe_line('apply_requested', f'apply={n} wrev={rev} requested[ship1={v} ship2=100.00/100.00 target1=- target2=-]', frame=frame)


def back(n, rev, hull, seen, frame, md='100/116'):
    v, o = f'{hull:.2f}/{hull:.2f}', f'{seen:.2f}/{seen:.2f}'
    marker = f' md={md}' if md else ''
    return probe_line('apply_readback', f'apply={n} wrev={rev} delay=0.020 requested[ship1={v} ship2=100.00/100.00] '
                      f'observed[ship1={o} ship2=100.00/100.00]{marker}', frame=frame)


def pair(n, rev, hull, frame):
    return req(n, rev, hull, frame) + back(n, rev, hull, hull, frame + 1, md=f'{frame}00/{frame}16')


SENT = [dict(rt=1, revision=1, phase='identical', ship1=(50.0, 50.0)),
        dict(rt=2, revision=2, phase='different', ship1=(20.0, 20.0)),
        dict(rt=3, revision=3, phase='rapid', ship1=(61.0, 61.0)),
        dict(rt=4, revision=4, phase='burst', ship1=(31.0, 31.0)),
        dict(rt=4, revision=5, phase='burst', ship1=(32.0, 32.0)),
        dict(rt=4, revision=6, phase='burst', ship1=(31.0, 31.0)),
        dict(rt=5, revision=7, phase='final', ship1=(100.0, 100.0))]
BASE = pair(1, 1, 50, 1) + pair(2, 2, 20, 3) + pair(3, 3, 61, 5) + pair(7, 7, 100, 20)
# Native shape: 31, 32, 31 queued in frame 10; one MD update handled them (md seen 1000) and one
# MD update read them back in frame 11 (md read 1016), observing the frame's last request.
ONE_UPDATE = (req(4, 4, 31, 10) + req(5, 5, 32, 10) + req(6, 6, 31, 10) +
              back(4, 4, 31, 31, 11, '1000/1016') + back(5, 5, 32, 31, 11, '1000/1016') + back(6, 6, 31, 31, 11, '1000/1016'))


class EvaluateTests(unittest.TestCase):
    def evaluate(self, text, joined=True, sent=SENT):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'log.txt'
            log.write_text(text)
            return harness.evaluate(log, sent, joined)

    def test_complete_correlation_passes(self):
        report = self.evaluate(BASE + ONE_UPDATE)
        self.assertEqual(report['result'], 'PASS', report['reasons'])
        self.assertEqual((report['requests'], report['confirmed']), (7, 7))
        self.assertEqual(report['observed_phases'], ['burst', 'different', 'final', 'identical', 'rapid'])
        self.assertEqual((report['max_requests_per_frame'], report['covered_batches']), (3, 1))
        self.assertEqual((report['raw_mismatched'], report['later_in_frame'], report['observed_mismatched']), (1, 1, 0))

    def test_missing_orphan_and_unrequested_health_fail(self):
        missing = BASE + ONE_UPDATE + req(8, 2, 20, 30)
        self.assertEqual(self.evaluate(missing)['result'], 'FAIL')
        orphan = BASE + ONE_UPDATE + back(9, 9, 1, 1, 31)
        self.assertEqual(self.evaluate(orphan)['result'], 'FAIL')
        report = self.evaluate(pair(1, 1, 50, 1) + pair(3, 3, 61, 5) + pair(7, 7, 100, 20) + ONE_UPDATE)
        self.assertEqual(report['result'], 'FAIL')
        self.assertTrue(any('never requested' in r for r in report['reasons']))

    def test_sent_but_unobserved_phase_fails(self):
        # The final phase was sent, but the guest never requested its revision.
        report = self.evaluate(pair(1, 1, 50, 1) + pair(2, 2, 20, 3) + pair(3, 3, 61, 5) + ONE_UPDATE)
        self.assertEqual(report['result'], 'FAIL')
        self.assertTrue(any('never observed' in r and 'final' in r for r in report['reasons']), report['reasons'])
        # A request of the right revision with other health does not count as observing it.
        report = self.evaluate(BASE.replace('ship1=100.00/100.00 ship2=100.00/100.00 target1', 'ship1=99.00/99.00 ship2=100.00/100.00 target1') + ONE_UPDATE)
        self.assertTrue(any('never observed' in r for r in report['reasons']), report['reasons'])

    def test_no_guest_no_log_or_no_probe_fail(self):
        self.assertEqual(self.evaluate(BASE + ONE_UPDATE, joined=False)['result'], 'FAIL')
        self.assertEqual(harness.evaluate(None, SENT, True)['result'], 'FAIL')
        self.assertEqual(self.evaluate('no probe here\n')['result'], 'FAIL')

    def test_burst_requires_identical_and_distinct_requests_in_one_md_update(self):
        cases = {
            'one update': (ONE_UPDATE, 'PASS'),
            'separate frames': (pair(4, 4, 31, 10) + pair(5, 5, 32, 12) + pair(6, 6, 31, 14), 'FAIL'),
            'distinct only': (req(4, 4, 31, 10) + req(5, 5, 32, 10) + back(4, 4, 31, 32, 11, '1000/1016') +
                              back(5, 5, 32, 32, 11, '1000/1016') + pair(6, 6, 31, 14), 'FAIL'),
            'readbacks split over Lua frames': (req(4, 4, 31, 10) + req(5, 5, 32, 10) + req(6, 6, 31, 10) +
                                                back(4, 4, 31, 31, 11, '1000/1016') + back(5, 5, 32, 31, 11, '1000/1016') +
                                                back(6, 6, 31, 31, 12, '1000/1016'), 'FAIL'),
            'one Lua frame, two MD read updates': (req(4, 4, 31, 10) + req(5, 5, 32, 10) + req(6, 6, 31, 10) +
                                                   back(4, 4, 31, 31, 11, '1000/1016') + back(5, 5, 32, 31, 11, '1000/1016') +
                                                   back(6, 6, 31, 31, 11, '1000/1032'), 'FAIL'),
            'no MD markers': (req(4, 4, 31, 10) + req(5, 5, 32, 10) + req(6, 6, 31, 10) +
                              back(4, 4, 31, 31, 11, None) + back(5, 5, 32, 31, 11, None) + back(6, 6, 31, 31, 11, None), 'FAIL'),
            'paused': (ONE_UPDATE.replace('paused=false', 'paused=true'), 'FAIL'),
            # Requests unpaused, all three readbacks paused: a frozen MD clock is no update evidence.
            'paused readbacks only': (ONE_UPDATE.replace('md=1000/1016 paused=false', 'md=1000/1016 paused=true'), 'FAIL'),
            'absent frame numbers': (ONE_UPDATE.replace(' frame=10 ', ' ').replace(' frame=11 ', ' '), 'FAIL'),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                report = self.evaluate(BASE + text)
                self.assertEqual(report['result'], expected, report['reasons'])
                if expected == 'PASS':
                    self.assertEqual(report['covered_batches'], 1)
                else:
                    self.assertEqual(report['covered_batches'], 0)
                    self.assertTrue(any('queued Mark/Apply' in r for r in report['reasons']), report['reasons'])

    def test_last_request_of_a_frame_must_match_its_own_values(self):
        text = (req(4, 4, 31, 10) + req(5, 5, 32, 10) +
                back(4, 4, 31, 40, 11, '1000/1016') + back(5, 5, 32, 40, 11, '1000/1016'))
        report = self.evaluate(BASE + text)
        self.assertEqual((report['later_in_frame'], report['raw_mismatched'], report['observed_mismatched']), (0, 2, 2),
                         'observing neither request is a mismatch, never later-in-frame')

    def test_observed_mismatch_is_reported_not_hidden(self):
        bad = BASE.replace('observed[ship1=50.00/50.00', 'observed[ship1=70.00/70.00') + ONE_UPDATE
        report = self.evaluate(bad)
        self.assertEqual((report['raw_mismatched'], report['observed_mismatched']), (2, 1))


class BundleTests(unittest.TestCase):
    def test_bundle_has_manifest_probe_only_trace_and_no_secrets(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            saved = harness.HERE
            try:
                harness.HERE = d / 'evidence'
                run_dir = harness.HERE / '20261020T000000Z-abc1234'; run_dir.mkdir(parents=True)
                (run_dir / 'profile-before').mkdir(); (run_dir / 'profile-before/config.xml').write_text('<root/>')
                package = d / 'pkg'; package.mkdir()
                token = 'a' * 32
                (package / 'session.json').write_text(json.dumps({'token': token}))
                home = str(Path.home())
                (run_dir / 'prepare.json').write_text(json.dumps({'address': '192.168.1.5', 'head': 'f' * 40,
                                                                   'profile': home + '\\Documents\\Egosoft\\X4\\45841'}))
                (run_dir / 'x4lan-debug.txt').write_text(
                    f'[X4LAN] DAMAGE_PROBE run=r1 token {token} lease {"b" * 32} at 192.168.1.5 head {"c" * 40}\n'
                    'other game line that must stay on this PC\n')
                with self.assertRaises(RuntimeError):
                    harness.bundle(run_dir, package)            # no restore summary yet
                (run_dir / 'restore.json').write_text(json.dumps({'problems': []}))
                archive = harness.bundle(run_dir, package)
                with zipfile.ZipFile(archive) as z:
                    names = {n.split('/', 1)[1] for n in z.namelist()}
                    trace = z.read(run_dir.name + '/' + harness.TRACE).decode()
                    prepare = z.read(run_dir.name + '/prepare.json').decode()
                    manifest = json.loads(z.read(run_dir.name + '/manifest.json'))
                    everything = b''.join(z.read(n) for n in z.namelist())
                self.assertEqual(names, {'prepare.json', 'restore.json', harness.TRACE, 'manifest.json', 'SHA256SUMS.txt'})
                self.assertNotIn('other game line', trace, 'probe lines only')
                for secret in (token, 'b' * 32, '192.168.1.5'):
                    self.assertNotIn(secret.encode(), everything)
                self.assertNotIn(json.dumps(home)[1:-1], prepare, 'home folder path removed')
                self.assertIn('c' * 40, trace, 'a 40-hex commit SHA is kept')
                self.assertEqual({f['name'] for f in manifest['files']}, {'prepare.json', 'restore.json', harness.TRACE})
                with self.assertRaises(RuntimeError):
                    harness.bundle(d, package)
            finally:
                harness.HERE = saved


if __name__ == '__main__':
    unittest.main()
