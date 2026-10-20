"""Regression for a plateau-only log falsely passing recorded health replay."""
import unittest
from dev.native_health_replay_test import analyze


class NativeHealthValidatorTests(unittest.TestCase):
    def fixture(self):
        recorded=[{'values':[100,100,0,100,100,0,100,100,100,97.913,300,1,1,0,0,0,6]}]
        plateau='HEALTH_PROBE expected=65,45,70,50,75,55,80,60 actual=65,45,70,50,75,55,80,60\n'
        log='HEALTH_PROBE perturb actual=42,5\n'+plateau*40
        return recorded,log

    def test_plateau_and_drift_alone_do_not_prove_recorded_trace_replay(self):
        recorded,log=self.fixture();r=analyze(log,recorded)
        self.assertTrue(r['local_drift_recovered'])
        self.assertEqual(r['samples'],40);self.assertEqual(r['max_error'],0)
        self.assertEqual(r['recorded_trace_readbacks'],0);self.assertFalse(r['success'])

    def test_actual_recorded_nonconstant_state_and_complete_readbacks_are_required(self):
        recorded,log=self.fixture()
        log+='HEALTH_PROBE expected=100,100,100,100,100,100,100,97.913 actual=100,100,100,100,100,100,100,97.913\n'
        r=analyze(log,recorded);self.assertTrue(r['success']);self.assertEqual(r['recorded_trace_readbacks'],1)
        r=analyze(log+'HEALTH_PROBE expected=100,100 actual=100,100\n',recorded)
        self.assertFalse(r['success']);self.assertEqual(r['malformed_readbacks'],1)
