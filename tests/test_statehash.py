import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from dev import compare_divergence as compare


def state_line(frame=1, complete=1, gt=None, changed=(), session="build", run="test", seed="0011223344556677"):
    gt = 100.0 + frame / 60 if gt is None else gt
    prefix = f"[X4LAN] STATEHASH run={run} session={session} frame={frame} "
    if complete == 0:
        return prefix + f"scanned=2 ships=0 fresh=0 complete=0 reason=ship%20limit seed={seed} gt={gt:.9f} dt=0.016666667\n"
    values = {key: f"{index + 1:016x}" for index, key in enumerate(compare.HASH_BUCKETS)}
    values["h"] = "f" * 16
    for key in changed:
        values[key] = "0" * 16
    if changed:
        values["h"] = "e" * 16
    buckets = " ".join(f"{key}={values[key]}" for key in compare.HASH_BUCKETS)
    return prefix + f"h={values['h']} {buckets} seed={seed} ships=2 weak=0 fresh=1 complete=1 gt={gt:.9f} dt=0.016666667\n"


class StateHashTests(unittest.TestCase):
    def logs(self, host, guest):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for name, lines in (("host", host), ("guest", guest)):
                path = Path(folder) / name
                path.write_text("".join(lines), encoding="utf-8")
                paths.append(path)
            return compare.compare_statehash(compare.read_statehash(paths[0]), compare.read_statehash(paths[1]))

    def test_equal_logs_have_no_divergence(self):
        result = self.logs([state_line(1), state_line(2)], [state_line(1), state_line(2)])
        self.assertIsNone(result["first"])
        self.assertEqual((result["compared"], result["missing"], result["incomplete"]), (2, 0, 0))
        self.assertIn("No hash divergence", compare.statehash_report(result))

    def test_each_bucket_reports_first_frame_and_elapsed_time(self):
        for bucket in compare.HASH_BUCKETS:
            with self.subTest(bucket=bucket):
                result = self.logs([state_line(1), state_line(2), state_line(3)],
                                   [state_line(1), state_line(2, changed=(bucket,)), state_line(3)])
                self.assertEqual(result["first"]["frame"], 2)
                self.assertEqual(result["first"]["buckets"], [bucket])
                self.assertAlmostEqual(result["first"]["elapsed_s"], 1 / 60)
                self.assertEqual(result["bucket_stats"][bucket]["first"]["frame"], 2)
                self.assertEqual(result["bucket_stats"][bucket]["differences"], 1)
                self.assertIn("buckets=" + bucket, compare.statehash_report(result))

    def test_missing_and_incomplete_are_not_equality(self):
        result = self.logs([state_line(1), state_line(2), state_line(3)],
                           [state_line(1), state_line(2, complete=0)])
        self.assertIsNone(result["first"])
        self.assertEqual((result["missing"], result["incomplete"], result["compared"]), (1, 1, 1))
        self.assertIn("WARNING", compare.statehash_report(result))

    def test_incompatible_or_inconsistent_logs_are_rejected(self):
        with self.assertRaisesRegex(compare.ProbeLogError, "session or seed differs"):
            self.logs([state_line(1)], [state_line(1, session="other")])
        with self.assertRaisesRegex(compare.ProbeLogError, "session or seed differs"):
            self.logs([state_line(1)], [state_line(1, seed="fedcba9876543210")])
        with self.assertRaisesRegex(compare.ProbeLogError, "inconsistent combined"):
            line = state_line(1).replace("h=" + "f" * 16, "h=" + "e" * 16)
            self.logs([state_line(1)], [line])

    def test_parser_rejects_malformed_and_mixed_logs(self):
        malformed = [state_line(1).replace("identity=", "identity=xyz\nbroken="),
                     state_line(1).replace("complete=1", "complete=2"),
                     state_line(1).replace("fresh=1", "fresh=0"),
                     state_line(1, complete=0).replace("fresh=0", "fresh=1"),
                     state_line(1, complete=0).replace("scanned=2", "scanned=many"),
                     state_line(1) + state_line(1)]
        for lines in malformed:
            with self.subTest(lines=lines), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "bad.log"
                path.write_text("".join(lines), encoding="utf-8")
                with self.assertRaises(compare.ProbeLogError):
                    compare.read_statehash(path)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mixed.log"
            path.write_text(state_line(1) + state_line(2, run="other"), encoding="utf-8")
            with self.assertRaisesRegex(compare.ProbeLogError, "mixed run"):
                compare.read_statehash(path)

    def test_cli_statehash_report(self):
        with tempfile.TemporaryDirectory() as folder:
            host, guest, report = [Path(folder) / name for name in ("host.log", "guest.log", "report.txt")]
            host.write_text(state_line(1) + state_line(2), encoding="utf-8")
            guest.write_text(state_line(1) + state_line(2, changed=("pose",)), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = compare.main([str(host), str(guest), "--statehash", "--report", str(report)])
            self.assertEqual(code, 0)
            self.assertIn("FIRST DIVERGENCE frame=2", output.getvalue())
            self.assertEqual(report.read_text(), output.getvalue())


if __name__ == "__main__":
    unittest.main()
