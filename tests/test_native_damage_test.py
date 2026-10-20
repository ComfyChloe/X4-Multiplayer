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


def pair(n, rev, hull):
    v = f'{hull:.2f}/{hull:.2f}'
    return (probe_line('apply_requested', f'apply={n} wrev={rev} requested[ship1={v} ship2=100.00/100.00 target1=- target2=-]') +
            probe_line('apply_readback', f'apply={n} wrev={rev} delay=0.020 requested[ship1={v} ship2=100.00/100.00] observed[ship1={v} ship2=100.00/100.00]'))


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

    def test_failed_install_leaves_restorable_state_sampled_before_install(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); package = d / 'pkg'; package.mkdir()
            (package / 'package.json').write_text(json.dumps({'package_id': 'lan-x-p2', 'slot': 'p2'}))
            (package / 'VERSION.txt').write_text('7.1.1')
            documents = d / 'Documents'; (documents / 'Egosoft/X4/45841').mkdir(parents=True)
            saved = dict(HERE=harness.HERE, STATE=harness.STATE, os=harness.os)
            calls = []

            def install(package_dir, *args):
                calls.append(args)
                (package_dir / 'installation.json').write_text('{}')    # Install writes the record...
                raise RuntimeError('install failed')                    # ...then fails
            patches = dict(git_head=lambda: 'abc1234def', x4_running=lambda: False, documents_folder=lambda: documents,
                           local_ipv4=lambda: '192.168.1.5', setup_ps1=install)
            originals = {k: getattr(harness, k) for k in patches}
            try:
                harness.HERE = d / 'evidence'; harness.STATE = harness.HERE / 'state.json'
                harness.os = type('os', (), {'name': 'nt', 'environ': {}})
                for k, v in patches.items():
                    setattr(harness, k, v)
                with self.assertRaises(RuntimeError):
                    harness.prepare(package)
                state = json.loads(harness.STATE.read_text())
                self.assertEqual(state['stage'], 'backed-up', 'recovery state written before Install')
                self.assertFalse(state['had_installation_json'], 'sampled before Install created the record')
                self.assertEqual(calls[0][:2], ('-Action', 'Install'))
                with self.assertRaises(RuntimeError):
                    harness.run(1)                                      # an incomplete prepare never runs
            finally:
                for k, v in originals.items():
                    setattr(harness, k, v)
                harness.HERE, harness.STATE, harness.os = saved['HERE'], saved['STATE'], saved['os']


class EvaluateTests(unittest.TestCase):
    def sent(self):
        return [dict(rt=1, revision=1, phase='identical', ship1=(50.0, 50.0)),
                dict(rt=2, revision=2, phase='different', ship1=(20.0, 20.0)),
                dict(rt=3, revision=3, phase='rapid', ship1=(61.0, 61.0)),
                dict(rt=4, revision=4, phase='burst', ship1=(31.0, 31.0)),
                dict(rt=5, revision=5, phase='final', ship1=(100.0, 100.0))]

    def evaluate(self, text, joined=True):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'log.txt'
            log.write_text(text)
            return harness.evaluate(log, self.sent(), joined)

    def test_complete_correlation_passes(self):
        report = self.evaluate(pair(1, 1, 50) + pair(2, 2, 50) + pair(3, 3, 20))
        self.assertEqual(report['result'], 'PASS', report['reasons'])
        self.assertEqual((report['requests'], report['confirmed']), (3, 3))

    def test_missing_orphan_and_unrequested_health_fail(self):
        missing = pair(1, 1, 50) + probe_line('apply_requested', 'apply=2 wrev=2 requested[ship1=20.00/20.00 ship2=100.00/100.00 target1=- target2=-]')
        self.assertEqual(self.evaluate(missing)['result'], 'FAIL')
        orphan = pair(1, 1, 50) + pair(2, 2, 20) + probe_line('apply_readback', 'apply=9 wrev=9 delay=0.010 requested[ship1=1.00/1.00 ship2=100.00/100.00] observed[ship1=1.00/1.00 ship2=100.00/100.00]')
        self.assertEqual(self.evaluate(orphan)['result'], 'FAIL')
        never_requested = pair(1, 1, 50)
        report = self.evaluate(never_requested)
        self.assertEqual(report['result'], 'FAIL')
        self.assertTrue(any('never requested' in r for r in report['reasons']))

    def test_no_guest_no_log_or_no_probe_fail(self):
        self.assertEqual(self.evaluate(pair(1, 1, 50) + pair(2, 2, 20), joined=False)['result'], 'FAIL')
        self.assertEqual(harness.evaluate(None, self.sent(), True)['result'], 'FAIL')
        self.assertEqual(self.evaluate('no probe here\n')['result'], 'FAIL')

    def test_burst_requires_several_requests_in_one_frame(self):
        burst = self.sent() + [dict(rt=5, revision=5, phase='burst', ship1=(31.0, 31.0))]

        def pair_in(n, rev, hull, frame):
            v = f'{hull:.2f}/{hull:.2f}'
            return (probe_line('apply_requested', f'apply={n} wrev={rev} requested[ship1={v} ship2=100.00/100.00 target1=- target2=-]', frame=frame) +
                    probe_line('apply_readback', f'apply={n} wrev={rev} delay=0.020 requested[ship1={v} ship2=100.00/100.00] observed[ship1={v} ship2=100.00/100.00]', frame=frame + 1))
        base = pair_in(1, 1, 50, 1) + pair_in(2, 2, 20, 2)
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'log.txt'
            log.write_text(base + pair_in(3, 3, 31, 5) + pair_in(4, 4, 32, 7))
            report = harness.evaluate(log, burst, True)
            self.assertEqual(report['result'], 'FAIL')
            self.assertTrue(any('queued Mark/Apply' in r for r in report['reasons']))
            log.write_text(base + pair_in(3, 3, 31, 5) + pair_in(4, 4, 32, 5))
            report = harness.evaluate(log, burst, True)
            self.assertEqual(report['result'], 'PASS', report['reasons'])
            self.assertEqual(report['max_requests_per_frame'], 2)

    def test_observed_mismatch_is_reported_not_hidden(self):
        bad = (probe_line('apply_requested', 'apply=1 wrev=1 requested[ship1=50.00/50.00 ship2=100.00/100.00 target1=- target2=-]') +
               probe_line('apply_readback', 'apply=1 wrev=1 delay=0.020 requested[ship1=50.00/50.00 ship2=100.00/100.00] observed[ship1=70.00/70.00 ship2=100.00/100.00]') +
               pair(2, 2, 20))
        report = self.evaluate(bad)
        self.assertEqual(report['observed_mismatched'], 1)


if __name__ == '__main__':
    unittest.main()
