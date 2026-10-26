"""Offline comparator checks for TIMING lines (synthetic logs, not native evidence)."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev"))
import compare_timing  # noqa: E402

CLEAN = ("mode=join frames=30 grav_pub=30 grav_lag1=30 grav_lagN=0 grav_lagmax=1 grav_busy0=0 grav_to=0 "
         "grav_wait_us=10 grav_nolock=0 mt_skip=0 mt_busy0=1 mt_busy15=0 mt_old=30 mt_new=0 mt_to=0 mt_wait_us=5 "
         "rm_busy0=0 rm_busy15=0 rm_old=30 rm_new=0 rm_to=0 rm_wait_us=0 path_to=0 path_wait_us=3 ev_n=4 ev_seg=2 "
         "ev_moved=1 ev_groups=0 ev_mixed=0 ev_skip=0 ev_raw={raw} ev_ord={ord} ev_set={set} jobs={jobs} disabled=0 reason=none")


def line(frame, **values):
    fields = {"raw": "1" * 16, "ord": "2" * 16, "set": "3" * 16, "jobs": 14}
    fields.update(values)
    body = CLEAN.format(**fields)
    for key, value in values.items():
        if key not in fields or key in ("mt_skip", "mt_new"):
            body = body.replace(f" {key}=0 ", f" {key}={value} ")
    return f"[=ERROR=] [X4LAN] TIMING run=a session=s frame={frame} gt={frame / 60:.9f} {body}\n"


class CompareTimingTests(unittest.TestCase):
    def write(self, directory, name, lines):
        path = Path(directory) / name
        path.write_text("noise\n" + "".join(lines), encoding="utf-8")
        return path

    def test_identical_join_logs_are_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            left = self.write(directory, "a.log", [line(30), line(60)])
            right = self.write(directory, "b.log", [line(30), line(60)])
            self.assertEqual(compare_timing.main([str(left), str(right), "--strict"]), 0)
            result = compare_timing.compare(compare_timing.parse(left), compare_timing.parse(right))
            self.assertEqual(result["aligned"], 2)
            self.assertTrue(compare_timing.timing_clean(result))

    def test_order_only_content_skip_clock_and_jobs_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            left = self.write(directory, "a.log", [line(30), line(60), line(90)])
            right = self.write(directory, "b.log", [
                line(30, ord="f" * 16), line(60, set="e" * 16), line(90, mt_skip=2, mt_new=3, jobs=6)])
            result = compare_timing.compare(compare_timing.parse(left), compare_timing.parse(right))
            self.assertEqual(result["order_only"], ["30"])
            self.assertEqual(result["content"], ["60"])
            self.assertEqual(result["right"]["mt_skip"], 2)
            self.assertEqual(result["right"]["mixed_clock_windows"], 1)
            self.assertEqual(result["clock_differs"], ["90"])
            self.assertTrue(result["jobs_differ"])
            self.assertFalse(compare_timing.timing_clean(result))
            self.assertEqual(compare_timing.main([str(left), str(right), "--strict"]), 1)
            self.assertEqual(compare_timing.main([str(left), str(right)]), 0)

    def test_missing_lines_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            left = self.write(directory, "a.log", [line(30)])
            empty = self.write(directory, "b.log", [])
            self.assertEqual(compare_timing.main([str(left), str(empty)]), 1)


if __name__ == "__main__":
    unittest.main()
