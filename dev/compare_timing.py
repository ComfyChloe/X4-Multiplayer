"""Compare TIMING lines from two X4 debug logs (engine_timing observe/join).

Windows are aligned by STATEHASH frame number (default) or by game time.
The report separates timing-dependent events (publish lag above one frame,
traffic skips, worker overlap, join timeouts, mixed worker clock reads) from
deferred-event differences: `ev_set` equal but `ev_ord` different means the
same commands arrived in a different order; a different `ev_set` means the
command contents differ (simulation divergence, not only timing).
Synthetic inputs are not native evidence.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

LINE = re.compile(r"\[X4LAN\] TIMING (?P<body>.*)$")
TIMING_EVENTS = ("grav_lagN", "grav_busy0", "grav_to", "grav_nolock", "mt_skip", "mt_busy0", "mt_busy15",
                 "mt_to", "rm_busy0", "rm_busy15", "rm_to", "path_to", "ev_mixed", "ev_skip")


def parse(path):
    rows = []
    for text in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE.search(text)
        if not match:
            continue
        fields = {}
        for part in match.group("body").split():
            if "=" in part:
                key, value = part.split("=", 1)
                fields[key] = value
        if "frame" in fields:
            rows.append(fields)
    return rows


def number(row, key):
    try:
        return int(row.get(key, "0"))
    except ValueError:
        return 0


# In join mode a worker that is still busy at the frame start is waited for,
# so *_busy0 only indicates timing dependence in observe mode.
JOIN_RESOLVED = ("grav_busy0", "mt_busy0", "rm_busy0")


def summarize(rows):
    totals = {key: sum(number(row, key) for row in rows
                       if not (key in JOIN_RESOLVED and row.get("mode") == "join"))
              for key in TIMING_EVENTS}
    totals["mixed_clock_windows"] = sum(
        1 for row in rows
        if (number(row, "mt_old") and number(row, "mt_new")) or (number(row, "rm_old") and number(row, "rm_new")))
    totals["windows"] = len(rows)
    return totals


def compare(left, right, align="frame"):
    def key(row):
        return row.get("frame") if align == "frame" else row.get("gt")

    right_by_key = {key(row): row for row in right}
    pairs = [(row, right_by_key[key(row)]) for row in left if key(row) in right_by_key]
    result = {"aligned": len(pairs), "order_only": [], "content": [], "jobs_differ": False,
              "clock_differs": [], "left": summarize(left), "right": summarize(right)}
    for a, b in pairs:
        label = key(a)
        if a.get("jobs") != b.get("jobs"):
            result["jobs_differ"] = True
        if a.get("ev_set") != b.get("ev_set"):
            result["content"].append(label)
        elif a.get("ev_ord") != b.get("ev_ord"):
            result["order_only"].append(label)
        clock = lambda row: (number(row, "mt_old") > 0, number(row, "mt_new") > 0,
                             number(row, "rm_old") > 0, number(row, "rm_new") > 0)
        if clock(a) != clock(b):
            result["clock_differs"].append(label)
    return result


def timing_clean(result):
    events = any(result[side][key] for side in ("left", "right") for key in TIMING_EVENTS)
    mixed = result["left"]["mixed_clock_windows"] or result["right"]["mixed_clock_windows"]
    return not (events or mixed or result["order_only"] or result["clock_differs"] or result["jobs_differ"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--align", choices=("frame", "gt"), default="frame")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any timing dependence remains")
    args = parser.parse_args(argv)
    left, right = parse(args.left), parse(args.right)
    if not left or not right:
        print("FAIL no TIMING lines in one or both logs", file=sys.stderr)
        return 1
    result = compare(left, right, args.align)
    print(f"aligned windows: {result['aligned']} (left {len(left)}, right {len(right)}) by {args.align}")
    print(f"job workers differ: {result['jobs_differ']}")
    for side in ("left", "right"):
        totals = result[side]
        print(f"{side}: " + " ".join(f"{key}={totals[key]}" for key in (*TIMING_EVENTS, "mixed_clock_windows")))
    print(f"deferred order-only differences: {len(result['order_only'])} first={result['order_only'][:5]}")
    print(f"deferred content differences: {len(result['content'])} first={result['content'][:5]}")
    print(f"worker clock-read pattern differences: {len(result['clock_differs'])} first={result['clock_differs'][:5]}")
    clean = timing_clean(result)
    print("TIMING CLEAN" if clean else "TIMING DEPENDENCE REMAINS")
    return 1 if args.strict and not clean else 0


if __name__ == "__main__":
    raise SystemExit(main())
