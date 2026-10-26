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


def attention_line(frame=1, attention="a" * 16, levels="0.0.0.0.0.1.0.0.0.2.0.0", changed=False):
    line = state_line(frame)
    combined = "d" * 16 if changed else "f" * 16
    return (line.replace("h=" + "f" * 16, "h=" + combined)
                .replace(" seed=", f" attention={attention} seed=")
                .replace(" weak=0 ", f" weak=0 attn_rows=3 attn_levels={levels} "))


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

    def test_attention_bucket_reports_first_mismatch_with_rows(self):
        host = [attention_line(1), attention_line(2), attention_line(3)]
        guest = [attention_line(1), attention_line(2, "b" * 16, "0.0.0.0.0.1.0.0.1.1.0.0", True), attention_line(3)]
        rows_host = "[X4LAN] ATTNROW frame=2 row=sec|code:ship:argon:A|s|9\n[X4LAN] ATTNROW frame=2 row=sec|*|sector|5\n"
        rows_guest = "[X4LAN] ATTNROW frame=2 row=sec|code:ship:argon:A|s|8\n[X4LAN] ATTNROW frame=2 row=sec|*|sector|5\n"
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for name, lines in (("host", host + [rows_host]), ("guest", guest + [rows_guest])):
                path = Path(folder) / name
                path.write_text("".join(lines), encoding="utf-8")
                paths.append(path)
            result = compare.compare_statehash(compare.read_statehash(paths[0]), compare.read_statehash(paths[1]),
                                               compare.read_attention_rows(paths[0]), compare.read_attention_rows(paths[1]))
        self.assertEqual(result["first"]["buckets"], ["attention"])
        self.assertEqual(result["first_attention"]["frame"], 2)
        self.assertEqual(result["bucket_stats"]["attention"]["differences"], 1)
        self.assertEqual(result["attention_compared"], 3)
        report = compare.statehash_report(result)
        self.assertIn("FIRST ATTENTION MISMATCH frame=2", report)
        self.assertIn("host=0.0.0.0.0.1.0.0.0.2.0.0 guest=0.0.0.0.0.1.0.0.1.1.0.0", report)
        self.assertIn("ATTENTION sec|code:ship:argon:A|s host=9 guest=8", report)
        self.assertNotIn("sector host=", report)

    def test_equal_attention_and_absent_attention_reports(self):
        result = self.logs([attention_line(1)], [attention_line(1)])
        self.assertIsNone(result["first"])
        self.assertIn("No attention mismatch in 1 compared frames", compare.statehash_report(result))
        legacy = self.logs([state_line(1)], [state_line(1)])
        self.assertIn("WARNING attention bucket absent", compare.statehash_report(legacy))
        with self.assertRaisesRegex(compare.ProbeLogError, "present in only one log"):
            self.logs([attention_line(1)], [state_line(1)])

    def test_malformed_attention_fields_are_rejected(self):
        for line in (attention_line(1, attention="xyz"), attention_line(1, levels="1.2.3"),
                     state_line(1).replace(" weak=0 ", " weak=0 attn_rows=3 "),
                     state_line(1, complete=0).replace(" seed=", " attention=" + "a" * 16 + " seed="),
                     "[X4LAN] ATTNROW frame=0 row=x\n" + attention_line(1)):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "bad.log"
                path.write_text(line, encoding="utf-8")
                with self.assertRaises(compare.ProbeLogError):
                    compare.read_statehash(path)
                    compare.read_attention_rows(path)

    def test_time_alignment_pairs_simultaneous_games_with_frame_offsets(self):
        host = [attention_line(frame, levels="0.0.0.0.0.1.0.0.0.2.0.0") for frame in (1, 2, 3)]
        guest = [attention_line(frame + 40, levels="0.0.0.0.0.1.0.0.0.2.0.0").replace(
            f"gt={100.0 + (frame + 40) / 60:.9f}", f"gt={100.0 + frame / 60 + 0.004:.9f}") for frame in (1, 2, 3)]
        guest[1] = guest[1].replace("attention=" + "a" * 16, "attention=" + "b" * 16).replace("h=" + "f" * 16, "h=" + "d" * 16)
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for name, lines in (("host", host), ("guest", guest)):
                path = Path(folder) / name
                path.write_text("".join(lines), encoding="utf-8")
                paths.append(path)
            by_frame = compare.compare_statehash(compare.read_statehash(paths[0]), compare.read_statehash(paths[1]))
            by_time = compare.compare_statehash(compare.read_statehash(paths[0]), compare.read_statehash(paths[1]),
                                                align_time=0.01)
            too_tight = compare.compare_statehash(compare.read_statehash(paths[0]), compare.read_statehash(paths[1]),
                                                  align_time=0.001)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = compare.main([str(paths[0]), str(paths[1]), "--statehash", "--align-time", "0.01"])
        self.assertEqual(by_frame["compared"], 0)
        self.assertEqual(by_time["compared"], 3)
        self.assertEqual((by_time["first_attention"]["frame"], by_time["first_attention"]["guest_frame"]), (2, 42))
        self.assertEqual((too_tight["compared"], too_tight["missing"]), (0, 6))
        self.assertEqual(code, 0)
        self.assertIn("paired by nearest game time within 0.010 s", output.getvalue())
        self.assertIn("FIRST ATTENTION MISMATCH frame=2 guest_frame=42", output.getvalue())

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
