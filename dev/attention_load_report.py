"""Summarize an attention load test log (FRAMEPROBE sim cost per ATTNLOAD step).

Reads x4lan-debug.txt in order. Each FRAMEPROBE record (one per second, from
fixed_tick_probe.lua) belongs to the ATTNLOAD step active when it was logged. A record
is excluded when it is
  * within the first --settle records after a step change,
  * inside an MD scan window (scan_begin .. first FRAMEPROBE after scan_end), or
  * empty (simFrames=0), without the sim brackets (simHooks=0) or paused.
Per step the report prints the frame-weighted mean of simMeanMs, the median and max of
the per-second simP95Ms, the max simMaxMs, frames per second (mean simFrames per
one-second window), objects at attention >= 9 in the proxy sectors (census_total), and
the cost per added sector relative to the first N=0 step. The divisor is the number of
sectors that actually got a proxy (`available` on the step line).
"""
from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from pathlib import Path

FRAME = re.compile(r"\[X4LAN\] FRAMEPROBE frames=(?P<frames>\d+) .*?realTime=(?P<real>[\d.]+) .*?"
                   r"paused=(?P<paused>\d+).*?simHooks=(?P<hooks>\d+) simFrames=(?P<sim_frames>\d+) "
                   r"simMeanMs=(?P<mean>[\d.]+) simP95Ms=(?P<p95>[\d.]+) simMaxMs=(?P<max>[\d.]+)")
ATTN = re.compile(r"\[X4LAN\] ATTNLOAD (?P<rest>.*)$")
HOOKS = re.compile(r"\[X4LAN\] (?P<kind>HOOKS_DISABLED|HOOKS_ATTN_REFUSED|HOOKS_READY) ?(?P<rest>.*)$")
COLUMNS = ("index", "step", "sectors", "phase", "windows", "excluded", "fps", "sim_mean_ms", "sim_p95_median_ms",
           "sim_p95_max_ms", "sim_max_ms", "hi9_proxy_sectors", "hi9_player_sector", "proxies_resolved",
           "replacements", "delta_mean_ms", "cost_per_sector_ms", "marginal_per_sector_ms")


def fields(text):
    return dict(part.split("=", 1) for part in text.split() if "=" in part)


class Step:
    def __init__(self, index, n, phase="step"):
        self.index, self.n, self.phase = index, n, phase
        self.available = n
        self.windows, self.excluded = [], 0
        self.hi9, self.player_hi9, self.resolved = [], [], []
        self.replacements = 0


def parse(lines, settle=10):
    steps, events = [], {"disabled": [], "refused": 0, "ready": 0, "errors": [], "done": False}
    current = None
    since_step = 0
    scanning = False
    dirty_next = False
    last_paused = None
    for line in lines:
        match = ATTN.search(line)
        if match:
            rest = match.group("rest").strip()
            values = fields(rest)
            if rest.startswith("step="):
                current = Step(int(values.get("index", len(steps) + 1)), int(values["step"]))
                current.available = int(values.get("available", current.n))
                steps.append(current)
                since_step = 0
            elif rest.startswith("done"):
                events["done"] = True
                current = Step(len(steps) + 1, 0, "post")
                steps.append(current)
                since_step = 0
            elif rest.startswith("scan_begin"):
                scanning = True
            elif rest.startswith("scan_end") or rest.startswith("scan_timeout"):
                scanning = False
                dirty_next = True
            elif rest.startswith("census_total") and current is not None:
                current.hi9.append(int(values.get("hi9", 0)))
                if int(values.get("player_hi9", -1)) >= 0:
                    current.player_hi9.append(int(values["player_hi9"]))
            elif rest.startswith("tick") and current is not None:
                if "attn_resolved" in values and int(values["attn_resolved"]) >= 0:
                    current.resolved.append(int(values["attn_resolved"]))
            elif rest.startswith("replace") and current is not None:
                current.replacements += 1
            elif rest.startswith("error"):
                events["errors"].append(values.get("reason", rest))
            continue
        match = HOOKS.search(line)
        if match:
            kind = match.group("kind")
            if kind == "HOOKS_DISABLED":
                events["disabled"].append(fields(match.group("rest")).get("reason", ""))
            elif kind == "HOOKS_ATTN_REFUSED":
                events["refused"] += 1
            else:
                events["ready"] += 1
            continue
        match = FRAME.search(line)
        if not match or current is None:
            continue
        since_step += 1
        record = {k: float(v) for k, v in match.groupdict().items()}
        paused = last_paused is not None and record["paused"] > last_paused
        last_paused = record["paused"]
        bad = (since_step <= settle or scanning or dirty_next or paused or record["hooks"] == 0
               or record["sim_frames"] == 0)
        dirty_next = False
        if bad:
            current.excluded += 1
        else:
            current.windows.append(record)
    return steps, events


def summarize(steps):
    rows = []
    baseline = next((s for s in steps if s.n == 0 and s.phase == "step" and s.windows), None)
    base_mean = None
    previous = None
    for step in steps:
        row = {"index": step.index, "step": step.n, "sectors": step.available, "phase": step.phase,
               "windows": len(step.windows), "excluded": step.excluded}
        if step.windows:
            frames = sum(w["sim_frames"] for w in step.windows)
            mean = sum(w["mean"] * w["sim_frames"] for w in step.windows) / frames
            p95s = [w["p95"] for w in step.windows]
            row.update(fps=frames / len(step.windows), sim_mean_ms=mean,
                       sim_p95_median_ms=statistics.median(p95s), sim_p95_max_ms=max(p95s),
                       sim_max_ms=max(w["max"] for w in step.windows))
            if step is baseline:
                base_mean = mean
        row["hi9_proxy_sectors"] = statistics.mean(step.hi9) if step.hi9 else None
        row["hi9_player_sector"] = statistics.mean(step.player_hi9) if step.player_hi9 else None
        row["proxies_resolved"] = statistics.mean(step.resolved) if step.resolved else None
        row["replacements"] = step.replacements
        rows.append(row)
    for row in rows:
        if base_mean is not None and "sim_mean_ms" in row:
            row["delta_mean_ms"] = row["sim_mean_ms"] - base_mean
            if row["sectors"] > 0:
                row["cost_per_sector_ms"] = row["delta_mean_ms"] / row["sectors"]
        if "sim_mean_ms" in row and row["phase"] == "step":
            if previous is not None and row["sectors"] != previous["sectors"]:
                row["marginal_per_sector_ms"] = ((row["sim_mean_ms"] - previous["sim_mean_ms"])
                                                 / (row["sectors"] - previous["sectors"]))
            previous = row
    return rows


def fmt(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def table(rows):
    header = ("idx", "N", "occ", "phase", "win", "excl", "fps", "mean_ms", "p95_med", "p95_max", "max_ms",
              "hi9", "hi9_pl", "resolved", "repl", "delta_ms", "per_sector", "marginal")
    keys = COLUMNS
    out = [header] + [tuple(fmt(row.get(k)) for k in keys) for row in rows]
    widths = [max(len(r[i]) for r in out) for i in range(len(header))]
    return "\n".join("  ".join(cell.rjust(widths[i]) for i, cell in enumerate(r)) for r in out)


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: fmt(row.get(k)) if row.get(k) is not None else "" for k in COLUMNS})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path, help="x4lan-debug.txt from the test run")
    parser.add_argument("--csv", type=Path, help="also write the per-step table as CSV")
    parser.add_argument("--settle", type=int, default=10,
                        help="FRAMEPROBE records skipped after every step change (default 10)")
    args = parser.parse_args(argv)
    with open(args.log, encoding="utf-8", errors="replace") as handle:
        steps, events = parse(handle, args.settle)
    if not steps:
        print("no ATTNLOAD step lines found", file=sys.stderr)
        for reason in events["disabled"]:
            print(f"HOOKS_DISABLED reason={reason}", file=sys.stderr)
        for reason in events["errors"]:
            print(f"ATTNLOAD error reason={reason}", file=sys.stderr)
        return 1
    rows = summarize(steps)
    print(table(rows))
    print(f"hooks_ready={events['ready']} hooks_disabled={len(events['disabled'])} "
          f"refused={events['refused']} errors={len(events['errors'])} completed={int(events['done'])}")
    for reason in events["disabled"]:
        print(f"HOOKS_DISABLED reason={reason}")
    for reason in events["errors"]:
        print(f"ATTNLOAD error reason={reason}")
    if args.csv:
        write_csv(rows, args.csv)
        print(f"csv: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
