"""Attention load report parser on a synthetic x4lan-debug.txt."""
import csv
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("attention_load_report", ROOT / "dev/attention_load_report.py")
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def frame(t, mean, p95, maxms, frames=60, paused=0, hooks=1):
    return (f"[=ERROR=] {t:.2f} [X4LAN] FRAMEPROBE frames={int(t * 60)} wallDt=0.016000 maxWallDt=0.300000 "
            f"gameDt=0.016000000 realDt=0.016000000 gameTime={t:.6f} realTime={t:.6f} speed=1.000 paused={paused} "
            f"loadingSkips=0 regressions=0 fixed=0 fixedFrames=0 wallElapsed=0.000000 gameElapsed=0.000000 "
            f"catchup=0 steps=0 zeroStep=0 multiStep=0 dropped=0 lastSteps=0 acc=0.000000 "
            f"simHooks={hooks} simFrames={frames} simMeanMs={mean:.3f} simP95Ms={p95:.2f} simMaxMs={maxms:.3f}")


def attn(t, text):
    return f"[=ERROR=] {t:.2f} [X4LAN] ATTNLOAD {text}"


def synthetic():
    lines = ["[=ERROR=] 1.00 [X4LAN] HOOKS_READY slot=12 cadence=3600 run=attnload session=x maxships=4000 "
             "budget_ms=8.000 hashing=0 profile=45842",
             attn(2, "armed warmup=20 steps=0,8 durations=60,180 prefer=docked"),
             frame(3, 99, 99, 99)]  # before the first step: ignored
    t = 30.0
    lines.append(attn(t, "step=0 index=1 sectors=- proxies=- duration=60 available=0 t=30.000 gt=30.000"))
    for i in range(60):
        t += 1
        if i == 30:
            lines.append(attn(t, "scan_begin seq=14 kind=Sector sector=cluster_02_sector001_macro"))
            lines.append(attn(t, "scan_end seq=14 kind=Sector sector=cluster_02_sector001_macro ms=12.0"))
            lines.append(frame(t, 50, 60, 80))  # dirty: excluded
            continue
        if i == 40:
            lines.append(frame(t, 70, 70, 70, paused=5))  # paused window: excluded
            continue
        lines.append(frame(t, 10.0 if i >= 10 else 30.0, 12.0, 15.0, paused=5 if i > 40 else 0))
    lines.append(attn(t, "census_total round=1 step=0 proxy_sectors=0 hi9=0 player_hi9=40"))
    lines.append(attn(t, "step=8 index=2 sectors=a,b,c,d,e,f,g,h proxies=1,2,3,4,5,6,7,8 duration=180 available=8 t=90 gt=90"))
    for i in range(180):
        t += 1
        if i % 20 == 0:
            lines.append(attn(t, "tick step=8 t=1 gt=1 proxies=1:1@a attn_sources=8 attn_resolved=8 attn_raises=10"))
        if i == 50:
            lines.append(attn(t, "replace slot=3 sector=c old=3 new=33 reason=dead"))
        lines.append(frame(t, 18.0 if i >= 10 else 40.0, 22.0 + (i % 2), 30.0 if i != 100 else 45.0, frames=50,
                           paused=5))
    lines.append(attn(t, "census_total round=2 step=8 proxy_sectors=8 hi9=300 player_hi9=40"))
    lines.append(attn(t, "census_total round=3 step=8 proxy_sectors=8 hi9=320 player_hi9=42"))
    lines.append(attn(t, "done t=1 gt=1"))
    for i in range(15):
        t += 1
        lines.append(frame(t, 11.0, 12.0, 14.0, paused=5))
    lines.append("[=ERROR=] 300.00 [X4LAN] HOOKS_ATTN_REFUSED reason=remote%20proxy ids=1,2")
    return lines


class AttentionLoadReportTests(unittest.TestCase):
    def test_parse_and_summarize(self):
        steps, events = report.parse(synthetic(), settle=10)
        self.assertEqual([(s.index, s.n, s.phase) for s in steps], [(1, 0, "step"), (2, 8, "step"), (3, 0, "post")])
        base, eight, post = report.summarize(steps)
        self.assertEqual(base["windows"], 60 - 10 - 2)
        self.assertEqual(base["excluded"], 12)
        self.assertAlmostEqual(base["sim_mean_ms"], 10.0)
        self.assertAlmostEqual(base["fps"], 60.0)
        self.assertEqual(base["hi9_player_sector"], 40)
        self.assertEqual(eight["windows"], 170)
        self.assertAlmostEqual(eight["sim_mean_ms"], 18.0)
        self.assertAlmostEqual(eight["fps"], 50.0)
        self.assertAlmostEqual(eight["sim_p95_median_ms"], 22.5)
        self.assertAlmostEqual(eight["sim_p95_max_ms"], 23.0)
        self.assertAlmostEqual(eight["sim_max_ms"], 45.0)
        self.assertAlmostEqual(eight["delta_mean_ms"], 8.0)
        self.assertAlmostEqual(eight["cost_per_sector_ms"], 1.0)
        self.assertAlmostEqual(eight["marginal_per_sector_ms"], 1.0)
        self.assertEqual(eight["hi9_proxy_sectors"], 310)
        self.assertEqual(eight["proxies_resolved"], 8)
        self.assertEqual(eight["replacements"], 1)
        self.assertEqual(post["phase"], "post")
        self.assertEqual(post["windows"], 5)
        self.assertAlmostEqual(post["delta_mean_ms"], 1.0)
        self.assertNotIn("cost_per_sector_ms", post)
        self.assertEqual((events["ready"], events["refused"], events["done"]), (1, 1, True))

    def test_short_step_divides_by_occupied_sectors(self):
        lines = [l.replace("available=8", "available=4") for l in synthetic()]
        _, eight, _ = report.summarize(report.parse(lines, settle=10)[0])
        self.assertEqual((eight["step"], eight["sectors"]), (8, 4))
        self.assertAlmostEqual(eight["cost_per_sector_ms"], 2.0)

    def test_cli_table_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x4lan-debug.txt"
            log.write_text("\n".join(synthetic()) + "\n", encoding="utf-8")
            out = Path(tmp) / "report.csv"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(report.main([str(log), "--csv", str(out)]), 0)
            text = buffer.getvalue()
            self.assertIn("per_sector", text)
            self.assertIn("completed=1", text)
            with out.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["step"] for r in rows], ["0", "8", "0"])
            self.assertEqual(rows[1]["cost_per_sector_ms"], "1.000")
            self.assertEqual(rows[0]["cost_per_sector_ms"], "")

    def test_missing_steps_reports_disable_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x4lan-debug.txt"
            log.write_text("[=ERROR=] 0.5 [X4LAN] HOOKS_DISABLED reason=inactive%20outside%20isolated%20profile%2045841\n",
                           encoding="utf-8")
            self.assertEqual(report.main([str(log)]), 1)


if __name__ == "__main__":
    unittest.main()
