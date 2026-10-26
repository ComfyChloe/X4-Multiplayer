"""Native transform acceptance must reject completed but failed/incomplete experiments."""
import unittest

from native.engine.xform_lane import validate_run


class TransformAcceptanceTests(unittest.TestCase):
    CLEAN = ('[X4LAN] XFORM done captures=6 failures=0 frames=5017 loadingSkips=0 '
             'foreignThread=0 shadowFrames=1001 shadowBad=0 shadowErrors=0 '
             'switchFrames=2504 switchFallbacks=0')

    def check(self, log):
        return validate_run(log, 6, 1000, 2500)

    def test_clean_completed_run(self):
        self.assertEqual(self.check(self.CLEAN), [])

    def test_done_does_not_hide_mismatches_or_failures(self):
        for counter in ('failures', 'foreignThread', 'shadowBad', 'shadowErrors', 'switchFallbacks'):
            with self.subTest(counter=counter):
                self.assertTrue(self.check(self.CLEAN.replace(f'{counter}=0', f'{counter}=1')))

    def test_refused_attempts_do_not_count_as_successful_work(self):
        self.assertTrue(self.check(self.CLEAN.replace('switchFrames=2504', 'switchFrames=1')
                                  .replace('switchFallbacks=0', 'switchFallbacks=2503')))
        self.assertTrue(self.check(self.CLEAN.replace('captures=6', 'captures=5')))
        self.assertTrue(self.check(self.CLEAN.replace('shadowFrames=1001', 'shadowFrames=999')))

    def test_missing_or_multiple_completion_records_fail(self):
        for log in ('', self.CLEAN + '\n' + self.CLEAN, self.CLEAN.replace(' shadowErrors=0', '')):
            self.assertTrue(self.check(log))

    def test_disabled_probe_cannot_pass(self):
        self.assertTrue(self.check('FEATURE xform DISABLED reason=shadow mismatch\n' + self.CLEAN))


if __name__ == '__main__':
    unittest.main()
