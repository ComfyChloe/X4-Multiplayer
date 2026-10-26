import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "dynload_evidence", Path(__file__).resolve().parents[1] / "dev/check_dynload_evidence.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class DynloadEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.summary = {"token": "fixture", "result": "done", "max_other_x4": 0}
        self.log = ("DYNLOAD CENSUS tag=load ships=10 stations=2\n"
                    "DYNLOAD CENSUS tag=final ships=12 stations=2 new_ships=2 gone=0\n"
                    "DYNLOAD DONE token=fixture\n")

    def test_fail_and_skip_are_not_done(self):
        for marker in ("FAIL bad", "SKIP wrong profile", "STREAM TIMEOUT op=ships"):
            self.assertFalse(M.audit(self.summary, self.log + "DYNLOAD " + marker)["clean_evidence"])

    def test_matching_token_required(self):
        self.assertFalse(M.audit(self.summary, self.log.replace("token=fixture", "token=stale"))["clean_evidence"])

    def test_reloaded_baseline_is_not_continuous(self):
        log = "DYNLOAD CENSUS tag=load ships=5 stations=2\n" + self.log
        result = M.audit(self.summary, log)
        self.assertFalse(result["clean_evidence"])
        self.assertEqual(result["ship_net_from_initial"], 7)

    def test_zero_other_processes_does_not_prove_timing(self):
        self.assertEqual(M.audit(self.summary, self.log)["timing"], "unverified")
        self.summary["max_other_x4"] = 5
        self.assertEqual(M.audit(self.summary, self.log)["timing"], "contended")

    def test_count_matches_are_not_identity_equivalence(self):
        plan = {"stations": [{"entries": 3}, {"entries": 4}]}
        log = self.log + ("DYNLOAD STATION plan=nil after_s=0.1 modules=4 with_constructions=4\n"
                          "DYNLOAD STATION plan=nil after_s=0.1 modules=3 with_constructions=3\n"
                          "DYNLOAD STATION plan=nil after_s=5 modules=0 with_constructions=0\n")
        result = M.audit(self.summary, log, plan)["station_counts"]
        self.assertTrue(result["count_multiset_matches"])
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["expected_modules"], 7)
        self.assertFalse(M.audit(self.summary, log.replace("modules=3", "modules=2"), plan)
                         ["station_counts"]["count_multiset_matches"])

    def test_operation_failure_not_silently_accepted(self):
        log = self.log + "DYNLOAD STREAM op=ships end=ships|10|1\n"
        self.assertFalse(M.audit(self.summary, log)["clean_evidence"])

    def test_numbered_metric_key_retained(self):
        self.assertEqual(M.fields("p95_frame_ms=27.3"), {"p95_frame_ms": "27.3"})


if __name__ == "__main__":
    unittest.main()
