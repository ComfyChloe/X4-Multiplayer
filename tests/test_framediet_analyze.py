"""Frame-diet log analysis (native/engine/framediet_analyze.py) on synthetic debug logs."""
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native.engine import framediet_analyze as fa  # noqa: E402


def write(lines):
    handle = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8')
    handle.write('\n'.join(lines) + '\n')
    handle.close()
    return Path(handle.name)


class PhaseTests(unittest.TestCase):
    def test_phases_weight_by_frames_and_skip_transients(self):
        log = write([
            '[=ERROR=] 1.00 [X4LAN] FRAMEDIET armed mode=diet sites=ui_scenes,draw period=0 digest=0 t=1.000',
            '[=ERROR=] 1.00 [X4LAN] FRAMEDIET phase=pass t=1.000 frames=0',
            '[=ERROR=] 2.00 [X4LAN] FRAMEPROBE frames=60 simFrames=60 simMeanMs=99.000 simP95Ms=99.00',  # skipped
            '[=ERROR=] 3.00 [X4LAN] FRAMEPROBE frames=120 simFrames=60 simMeanMs=10.000 simP95Ms=12.00',
            '[=ERROR=] 4.00 [X4LAN] FRAMEPROBE frames=180 simFrames=30 simMeanMs=16.000 simP95Ms=20.00',
            '[=ERROR=] 4.50 [X4LAN] FRAMEDIET phase=server t=4.500 frames=180',
            '[=ERROR=] 5.00 [X4LAN] FRAMEPROBE frames=240 simFrames=60 simMeanMs=50.000 simP95Ms=50.00',  # skipped
            '[=ERROR=] 6.00 [X4LAN] FRAMEPROBE frames=300 simFrames=60 simMeanMs=6.000 simP95Ms=7.00',
            '[=ERROR=] 6.50 [X4LAN] ATTNLOAD step=8 index=2 sectors=8',
            '[=ERROR=] 7.00 [X4LAN] FRAMEPROBE frames=330 simFrames=30 simMeanMs=30.000 simP95Ms=31.00',  # skipped
            '[=ERROR=] 8.00 [X4LAN] FRAMEPROBE frames=370 simFrames=40 simMeanMs=20.000 simP95Ms=24.00',
        ])
        rows = {(p, s): tuple(rest) for p, s, *rest in fa.phases(log, skip=1)}
        samples, frames, seconds, ms, p95 = rows[('pass', '-')]
        self.assertEqual((samples, frames), (2, 90))
        self.assertAlmostEqual(ms, (10 * 60 + 16 * 30) / 90)
        self.assertAlmostEqual(seconds, 2.0)
        self.assertEqual(rows[('server', '-')][:2], (1, 60))
        self.assertAlmostEqual(rows[('server', '-')][3], 6.0)
        self.assertEqual(rows[('server', '8')][:2], (1, 40))
        self.assertAlmostEqual(rows[('server', '8')][4], 24.0)

    def test_compare_reports_saving_per_step(self):
        rows = [('pass', '0', 10, 600, 10.0, 10.0, 12.0), ('server', '0', 10, 600, 10.0, 8.0, 10.0),
                ('pass', '8', 10, 400, 10.0, 25.0, 30.0), ('all', '8', 10, 480, 10.0, 20.0, 24.0)]
        lines = fa.compare(rows, 'pass')
        self.assertEqual(len(lines), 2)
        self.assertIn('step 0: server vs pass: 8.000 vs 10.000 ms/frame, saved 2.000 ms (20.0 %), fps 60.0 vs 60.0', lines[0])
        self.assertIn('step 8: all vs pass: 20.000 vs 25.000 ms/frame, saved 5.000 ms (20.0 %), fps 48.0 vs 40.0', lines[1])

    def test_audit_keeps_last_row_per_region(self):
        log = write([
            '[=ERROR=] 10.00 [X4LAN] FRAMEDIET_AUDIT row=camera audited=20 rngCalls=1',
            '[=ERROR=] 20.00 [X4LAN] FRAMEDIET_AUDIT row=camera audited=40 rngCalls=2',
            '[=ERROR=] 20.00 [X4LAN] FRAMEDIET_AUDIT row=control audited=2 rngCalls=0',
            '[=ERROR=] 20.00 [X4LAN] FRAMEDIET mode=audit sites=camera phase=audit t=20.000 frames=1200',
        ])
        rows, last = fa.audit(log)
        self.assertIn('audited=40', rows['camera'])
        self.assertIn('row=control', rows['control'])
        self.assertIn('frames=1200', last)

    def test_audit_phases_diff_cumulative_counters(self):
        log = write([
            '[=ERROR=] 10.00 [X4LAN] FRAMEDIET_FRAMES audited=600 rngSteps=90000 rngUnknown=0 ids=10',
            '[=ERROR=] 10.00 [X4LAN] FRAMEDIET_AUDIT row=camera audited=1200 rngCalls=600 rngSteps=90000 ids=10',
            '[=ERROR=] 10.00 [X4LAN] FRAMEDIET phase=audit:camera,rng_split t=10.000 frames=600',
            '[=ERROR=] 20.00 [X4LAN] FRAMEDIET_FRAMES audited=1200 rngSteps=95000 rngUnknown=0 ids=12',
            '[=ERROR=] 20.00 [X4LAN] FRAMEDIET_AUDIT row=camera audited=2400 rngCalls=1200 rngSteps=180000 ids=14',
            '[=ERROR=] 20.00 [X4LAN] FRAMEDIET mode=audit sites=camera,rng_split phase=x t=20 frames=1200 splitCalls=600 splitRenderSteps=90000',
        ])
        phases = dict(fa.audit_phases(log))
        self.assertEqual(phases['start']['frames']['rngSteps'], 90000)
        split = phases['audit:camera,rng_split']
        self.assertEqual(split['frames']['rngSteps'], 5000)        # simulation stream only
        self.assertEqual(split['audit:camera']['rngSteps'], 90000)  # render stream, measured inside
        self.assertEqual(split['frames']['ids'], 2)
        self.assertEqual(split['stats']['splitRenderSteps'], 90000)

    def test_stability_reports_cadence_commit_and_errors(self):
        log = write([
            '[=ERROR=] 10.00 [X4LAN] FRAMEDIET mode=diet sites=all phase=all t=10.000 frames=600 commitMb=2000.0',
            '[=ERROR=] 40.00 [X4LAN] FRAMEDIET mode=diet sites=all phase=all t=40.000 frames=2400 commitMb=2010.5',
            '[=ERROR=] 40.00 [X4LAN] FRAMEDIET phase=pass t=40.000 frames=2400',
            '[=ERROR=] 75.00 [X4LAN] FRAMEDIET mode=diet sites=all phase=pass t=75.000 frames=4500 commitMb=2011.0',
            '[General] 50.00 Lua error: ui/addons/foo.lua:12: attempt to index nil',
            '[General] 60.00 Lua error: ui/addons/foo.lua:12: attempt to index nil',
            '[=ERROR=] 80.00 [X4LAN] FEATURE framediet DISABLED reason=vtable entry changed',
        ])
        r = fa.stability(log)
        self.assertEqual(r['reports'], 3)
        self.assertAlmostEqual(r['max_gap_s'], 35.0)
        self.assertEqual((r['frames_first'], r['frames_last']), (600, 4500))
        self.assertEqual(r['commit_mb']['start'], (2000.0, 2010.5))
        self.assertEqual(r['commit_mb']['pass'], (2011.0, 2011.0))
        self.assertEqual(len(r['features']), 1)
        self.assertEqual(sum(r['errors'].values()), 2)
        self.assertEqual(len(r['errors']), 1)

    def test_stability_check_rejects_recovered_watchdog_without_disabled_marker(self):
        log = write([
            '[=ERROR=] 0 [X4LAN] FRAMEDIET mode=diet phase=ui_elements t=0 frames=1 stalls=0',
            '[=ERROR=] 3 [X4LAN] FRAMEDIET stall sites=ui_elements frame=180 stalls=1',
            '[=ERROR=] 10 [X4LAN] FRAMEDIET mode=pass phase=ui_elements t=10 frames=600 stalls=1',
        ])
        failures = fa.stability_failures(fa.stability(log), 5, 15)
        self.assertIn('UI stall watchdog fired', failures)
        self.assertIn('actual mode differs from requested phase', failures)
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(fa.main(['stability', str(log), '--check', '--min-duration', '5']), 1)
        self.assertIn('STABILITY FAIL', out.getvalue())

    def test_stability_check_catches_counter_only_stall_and_mode_drift_separately(self):
        for mode, count, expected in [('diet', 1, 'UI stall watchdog fired'),
                                      ('pass', 0, 'actual mode differs from requested phase')]:
            with self.subTest(mode=mode, count=count):
                log = write([
                    '[=ERROR=] 0 [X4LAN] FRAMEDIET mode=diet phase=server t=0 frames=1 stalls=0',
                    f'[=ERROR=] 10 [X4LAN] FRAMEDIET mode={mode} phase=server t=10 frames=600 stalls={count}',
                ])
                self.assertIn(expected, fa.stability_failures(fa.stability(log), 5, 15))

    def test_stability_check_requires_valid_progress_and_report_coverage(self):
        prefix = '[=ERROR=] 0 [X4LAN] FRAMEDIET mode=diet phase=server t=0 frames=10'
        for tail, expected in [
            ([], 'insufficient report duration'),
            (['t=10 frames=10'], 'no frame progress'),
            (['t=50 frames=20'], 'UI report gap exceeded'),
            (['t=10 frames=5'], 'invalid or regressing counters'),
            (['t=nan frames=20'], 'invalid or regressing counters'),
            (['t=10 frames=inf'], 'invalid or regressing counters'),
        ]:
            with self.subTest(tail=tail):
                log = write([prefix] + ['[=ERROR=] 10 [X4LAN] FRAMEDIET mode=diet phase=server ' + x for x in tail])
                self.assertIn(expected, fa.stability_failures(fa.stability(log), 5, 15))

    def test_stability_check_accepts_continuing_ui_reports_in_requested_modes(self):
        log = write([
            '[=ERROR=] 0 [X4LAN] FRAMEDIET mode=diet phase=server,rng_split t=0 frames=1 stalls=0',
            '[=ERROR=] 10 [X4LAN] FRAMEDIET mode=diet phase=server,rng_split t=10 frames=600 stalls=0',
            '[=ERROR=] 10 [X4LAN] FRAMEDIET mode=pass phase=pass t=10 frames=600 stalls=0',
            '[=ERROR=] 20 [X4LAN] FRAMEDIET mode=pass phase=pass t=20 frames=1200 stalls=0',
        ])
        self.assertEqual(fa.stability_failures(fa.stability(log), 15, 15), [])
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(fa.main(['stability', str(log), '--check', '--min-duration', '15']), 0)
        self.assertIn('STABILITY PASS (reported interval only)', out.getvalue())

    def test_attribution_splits_lua_from_presentation(self):
        log = write([
            '[=ERROR=] 9.00 [X4LAN] FRAMEPROBE frames=600 simFrames=100 simMeanMs=10',
            '[=ERROR=] 10.00 [X4LAN] SIMPROF window seconds=10.00 samples=1000',
            '[=ERROR=] 10.00 [X4LAN] SIMPROF fold slot6;ef4400;b3bad0;b36300;13a5f90;138f2c0;1399c40 300',
            '[=ERROR=] 10.00 [X4LAN] SIMPROF fold slot6;ef4400;b3bad0;b36300;13a5f90;138f2c0;af85e0;lua51_64+1000 50',
            '[=ERROR=] 10.00 [X4LAN] SIMPROF fold slot10;ef4400;121c1f0;122bfc0;983160;779a00 200',
            '[=ERROR=] 10.00 [X4LAN] SIMPROF fold slot3;ef4400;9fdef0;3d4a80 450',
        ])
        (lo, hi, total, frames, row), = fa.attribution(log, [(0, 100)])
        self.assertEqual((total, frames), (1000, 100))
        got = {rva: ms for (rva, _label), (ms, _share) in zip(fa.ATTRIBUTION, row)}
        self.assertAlmostEqual(got['b3bad0'], 3.5)
        self.assertAlmostEqual(got['lua'], 0.5)
        self.assertAlmostEqual(got['1399c40'], 3.0)
        self.assertAlmostEqual(got['983160'], 2.0)
        self.assertAlmostEqual(got['3d4a80'], 4.5)
        self.assertAlmostEqual(got['af8e50'], 0.0)


if __name__ == '__main__':
    unittest.main()
