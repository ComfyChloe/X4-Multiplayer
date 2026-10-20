"""Compare independent native station readbacks around an isolated save reload.

This checks the initial station layout, not ongoing simulation authority. Local
component IDs are reported separately: network identity must not depend on them.
"""
import argparse
import json
import math
from pathlib import Path
import re


def _read(text, mode, seed_id):
    tag = f"mode={mode} id={seed_id} count="
    begin = re.findall(r"SEED_PROBE begin " + re.escape(tag) + r"(\d+)", text)
    end = re.findall(r"SEED_PROBE end " + re.escape(tag) + r"(\d+)", text)
    if len(begin) != 1 or end != begin or int(begin[0]) < 1:
        raise ValueError("missing, repeated or incomplete station observation")
    rows = {}
    for line in text.splitlines():
        if "SEED_PROBE station " not in line:
            continue
        match = re.search(
            r"SEED_PROBE station object=(\d+) code=(\S+) macro=(\S+) "
            r"owner=(\S+) sector=(\S+) pose=(\S+)$", line)
        if not match:
            raise ValueError("malformed native station row")
        object_id, code, macro, owner, sector, pose = match.groups()
        values = tuple(map(float, pose.split(",")))
        if (len(values) != 6 or not all(math.isfinite(v) for v in values)
                or any(v in ("nil", "unknown", "null") for v in (code, macro, owner, sector))
                or code in rows or int(object_id) == 0):
            raise ValueError("invalid or duplicate station identity/pose")
        rows[code] = dict(object=object_id, macro=macro, owner=owner, sector=sector, pose=values)
    if len(rows) != int(begin[0]):
        raise ValueError("station count differs from observation boundary")
    if re.search(r"(?:Error in MD cue|Lua error).*?(?:X4LAN|x4lan|seed_probe)", text):
        raise ValueError("native mod error during seed observation")
    completion = (f"SEED_PROBE saved success=true id={seed_id}" if mode == "capture"
                  else f"SEED_PROBE loaded id={seed_id}")
    if completion not in text:
        raise ValueError("native save/load completion absent")
    return rows


def validate(capture, loaded, seed_id):
    if re.fullmatch(r"[a-f0-9]{32}", seed_id) is None:
        raise ValueError("invalid expected seed identity")
    original, restored = _read(capture, "capture", seed_id), _read(loaded, "load", seed_id)
    if original.keys() != restored.keys():
        raise ValueError("native station identity set changed")
    position_error = angle_error = 0.0
    local_ids_equal = True
    for code, before in original.items():
        after = restored[code]
        if any(before[k] != after[k] for k in ("macro", "owner", "sector")):
            raise ValueError("native station definition/topology changed: " + code)
        position_error = max(position_error, math.dist(before["pose"][:3], after["pose"][:3]))
        for a, b in zip(before["pose"][3:], after["pose"][3:]):
            angle_error = max(angle_error, abs((a - b + math.pi) % (2 * math.pi) - math.pi))
        local_ids_equal &= before["object"] == after["object"]
    if position_error > 0.05 or angle_error > 0.00001:
        raise ValueError("native station transform changed")
    return dict(seed_id=seed_id, stations=len(original), native_reload=True,
                station_identity_topology_equal=True, max_position_error_m=position_error,
                max_angle_error_rad=angle_error, local_component_ids_equal=local_ids_equal,
                ongoing_simulation_authority=False, two_native_acceptance=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("loaded", type=Path)
    parser.add_argument("--seed-id", required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.capture.read_text(errors="replace"),
                              args.loaded.read_text(errors="replace"), args.seed_id), indent=2))
