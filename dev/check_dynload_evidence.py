"""Audit retained dynload logs without equating runner completion with acceptance.

No game process or save is modified. Use --require-clean to reject incomplete,
failed or harness-reloaded runs; timing and station identity remain separate
evidence dimensions even when that check passes.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def fields(line: str) -> dict[str, str]:
    return dict(re.findall(r"\b([A-Za-z_][A-Za-z_0-9]*)=([^\s]+)", line))


def audit(summary: dict, log: str, plan: dict | None = None) -> dict:
    token = summary.get("token", "")
    rows = [line.split("DYNLOAD ", 1)[1].strip()
            for line in log.splitlines() if "DYNLOAD " in line]
    problems = []
    if not token:
        problems.append("summary has no run token")
    if summary.get("result") != "done":
        problems.append("runner result is not done")
    done = [r for r in rows if r.startswith("DONE ") and fields(r).get("token") == token]
    if len(done) != 1:
        problems.append("expected exactly one matching DONE marker")
    failures = [r for r in rows if r.startswith(("FAIL ", "SKIP ", "STREAM TIMEOUT "))]
    if failures:
        problems.append("explicit failure, refusal or stream timeout")
    census = [fields(r) for r in rows if r.startswith("CENSUS ")]
    baselines = [r for r in census if r.get("tag") == "load"]
    finals = [r for r in census if r.get("tag") == "final"]
    if len(baselines) != 1:
        problems.append("missing or repeated load census: baseline continuity unproven")
    if len(finals) != 1:
        problems.append("expected exactly one final census")
    operations = [fields(r.split(" | idle_before", 1)[0]) for r in rows
                  if r.startswith("STREAM op=")]
    for op in operations:
        try:
            end = op["end"].split("|")
            if len(end) != 3 or end[0] != op["op"] or int(end[2]) != 0:
                problems.append("stream operation reported failures or mismatched kind")
        except (KeyError, ValueError):
            problems.append("malformed stream completion marker")
    # These samples have no persistent station identity in the legacy harness.
    # Compare the count multiset only, never label it full station equivalence.
    stations = [fields(r) for r in rows if r.startswith("STATION ")]
    early = []
    for s in stations:
        try:
            if float(s["after_s"]) < 1:
                early.append(s)
        except (KeyError, ValueError):
            problems.append("malformed station sample age")
    station_summary = {"early_samples": len(early), "identity_verified": False,
                       "per_station_equivalence": "unproven"}
    if plan is not None and early:
        expected = Counter(int(s["entries"]) for s in plan["stations"])
        try:
            observed = Counter(int(s["modules"]) for s in early)
            repeats, remainder = divmod(len(early), len(plan["stations"]))
            station_summary.update({
                "expected_stations": len(plan["stations"]),
                "expected_modules": sum(int(s["entries"]) for s in plan["stations"]),
                "count_multiset_repetitions": repeats,
                "count_multiset_matches": not remainder and repeats > 0
                    and observed == Counter({n: c * repeats for n, c in expected.items()}),
                "construction_counts_equal": all(s["modules"] == s["with_constructions"]
                                                  for s in early),
            })
        except (KeyError, ValueError, ZeroDivisionError):
            problems.append("malformed station count or empty plan")
    other = summary.get("max_other_x4")
    timing = "contended" if isinstance(other, int) and other > 0 else "unverified"
    # A zero process sample is insufficient to certify an exclusive timing window.
    out = {"token": token, "clean_evidence": not problems,
           "problems": list(dict.fromkeys(problems)), "failure_markers": failures,
           "timing": timing, "max_other_x4": other,
           "load_censuses": baselines, "final_censuses": finals,
           "stream_operations": operations, "station_counts": station_summary,
           "suppression": [r for r in rows if r.startswith("SUPPRESS")]}
    if baselines and finals:
        try:
            out["ship_net_from_initial"] = int(finals[-1]["ships"]) - int(baselines[0]["ships"])
            out["station_net_from_initial"] = int(finals[-1]["stations"]) - int(baselines[0]["stations"])
        except (KeyError, ValueError):
            out["clean_evidence"] = False
            out["problems"].append("malformed census counts")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig")) if args.plan else None
    failed = False
    for run in args.runs:
        try:
            result = audit(json.loads((run / "summary.json").read_text(encoding="utf-8-sig")),
                           (run / "x4log.txt").read_text(encoding="utf-8", errors="replace"), plan)
        except (OSError, ValueError, KeyError) as exc:
            result = {"run": run.name, "clean_evidence": False, "problems": [str(exc)]}
        print(json.dumps(result, sort_keys=True))
        failed |= not result["clean_evidence"]
    return int(args.require_clean and failed)


if __name__ == "__main__":
    raise SystemExit(main())
