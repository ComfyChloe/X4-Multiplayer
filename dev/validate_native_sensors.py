"""Validate native known-state/HUD discovery beyond the starter ship's coverage."""
import argparse
import json
from pathlib import Path
import re


def analyze(text):
    rows = {}
    for line in text.splitlines():
        if "[X4LAN] SENSOR_PROBE " not in line:
            continue
        value = line.split("[X4LAN] SENSOR_PROBE ", 1)[1]
        match = re.fullmatch(
            r"phase=(\w+) index=(\d+) object=(\d+)(?:ULL)? known=(true|false) radar=(true|false) "
            r"range=([0-9.]+) name=(.*?) header=(.*?) position=([-0-9.,]+)", value)
        if not match:
            raise ValueError("malformed native sensor row")
        phase, index, component, known, radar, radius, name, header, pose = match.groups()
        key = phase, int(index)
        if key in rows:
            raise ValueError("duplicate native sensor row")
        rows[key] = dict(component=component, known=known == "true", radar=radar == "true",
                         range=float(radius), name=name, header=header,
                         pose=[float(v) for v in pose.split(",")])
    phases = ("remote1", "remote2", "activated1", "activated2", "native_ship", "native_pilot")
    complete = all((phase, index) in rows for phase in phases for index in (1, 2, 6, 7))
    errors = [line for line in text.splitlines()
              if "Error in MD cue md.X4LAN" in line or "BUBBLE STOPPED" in line or "FATAL ERROR" in line]
    def visible(phase, index):
        row = rows.get((phase, index), {})
        return row.get("known", False) and row.get("radar", False) and "Unknown" not in row.get("header", "Unknown")
    distant = complete and all(
        any(abs(rows[phase, index]["pose"][0] - 399000) < 10 and rows[phase, index]["range"] >= 6000
            for index in (1, 2))
        and abs(rows[phase, 6]["pose"][2] - 2000) < 10
        and abs(rows[phase, 7]["pose"][2] - 6000) < 10
        and all(abs(rows[phase, i]["pose"][0] - 399000) < 10 for i in (6, 7))
        for phase in phases)
    production_discovery = complete and all(visible(phase, i) for phase in phases[:4] for i in (6, 7))
    control_discovery = complete and all(visible(phase, i) for phase in phases[4:] for i in (6, 7))
    reproduction = complete and all(not rows[phase, i]["known"] for phase in phases[:4] for i in (6, 7)) and control_discovery
    return dict(rows=len(rows), complete=complete, isolated_distance=distant,
                production_discovery=production_discovery, native_control_discovery=control_discovery,
                original_failure_reproduced=reproduction, native_errors=errors,
                success=bool(complete and distant and production_discovery and control_discovery and not errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    result = analyze(args.log.read_text(errors="replace"))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["success"] else 1)
