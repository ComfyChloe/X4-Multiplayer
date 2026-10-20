"""Opt-in native pristine-seed admission and role initialization.

First capture/transfer with native_seed_test.py. Run one native role at a time
against native_world_test.py peer --config RUN/play-session.json. Existing saves
are never selected; only this run's exact transferred disposable copy is loaded.
"""
import argparse
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dev import native_seed_test as seed_test
from dev import native_world_test as native
from x4lan import seed


def identity(run):
    state = json.loads((run / "state.json").read_text())
    if seed_test.content_identity() != {k: state[k] for k in ("game_build", "mod_sha256", "extensions")}:
        raise RuntimeError("Content differs from the captured pristine seed.")
    manifest, _ = seed.read_bundle(run / "received")
    if any(manifest[k] != state[k] for k in state):
        raise RuntimeError("Received seed does not match this run.")
    return state, manifest


def install(run, slot):
    native.setup.ensure_closed()
    native.setup.check_game(native.GAME, "23660954")
    state, _ = identity(run)
    marker = native.TARGET / ".x4lan-package-id"
    if native.TARGET.is_symlink() or not marker.is_file() or not marker.read_text().startswith("lan-"):
        raise RuntimeError("A known internal test installation is required.")
    if ET.parse(native.PROFILE / "config.xml").getroot().findtext("autosave") != "false":
        raise RuntimeError("Isolated autosave must remain disabled.")
    folder = run / ("play-" + slot)
    folder.mkdir()
    (folder / "saves-before.json").write_text(json.dumps(native.saves(), indent=2))
    shutil.copytree(native.TARGET, folder / "extension-before")
    seed.stage_test_save(run / "received", native.PROFILE)
    session_file = run / "play-session.json"
    if session_file.exists():
        session = json.loads(session_file.read_text())
        if session["bubble"] != state["seed_id"] or session["build"] != state["game_build"]:
            raise RuntimeError("Relay session does not match seed.")
    else:
        session = dict(token=secrets.token_hex(16), build=state["game_build"], bubble=state["seed_id"],
                       port=45843, coop=True, fire2=True, world_sync=True, world_mode="replica",
                       ship_macro="ship_ter_s_xperimental_01_a_macro", ship_loadout="x4lan_coop_xperimental",
                       ship_weapons=6)
        session_file.write_text(json.dumps(session, indent=2))
    for probe in (ROOT / "dev").glob("*probe.*"):
        subdir = {".lua": "ui", ".xml": "md"}.get(probe.suffix)
        if subdir:
            (native.TARGET / subdir / probe.name).unlink(missing_ok=True)
    shutil.copytree(ROOT / "extension/x4_lan_lab", native.TARGET, dirs_exist_ok=True)
    config = dict(enabled=True, bubble_actions=True, trading=True, walking_avatars=False,
                  test_profile="45841", host="127.0.0.1", slot=slot, mode="proxy",
                  proxy_macro="ship_arg_s_scout_01_a_macro", seed_id=state["seed_id"],
                  seed_verified=True, **session)
    (native.TARGET / "ui/config.lua").write_text("X4LAN_CONFIG={\n" + "".join(
        f" {k}={json.dumps(v)},\n" for k, v in config.items()) + "}\n")
    for suffix, subdir in (("lua", "ui"), ("xml", "md")):
        shutil.copy2(ROOT / ("dev/seed_play_probe." + suffix),
                     native.TARGET / subdir / ("seed_play_probe." + suffix))
    tree = ET.parse(native.TARGET / "ui.xml")
    ET.SubElement(tree.find("environment"), "file", name="ui/seed_play_probe.lua")
    tree.write(native.TARGET / "ui.xml", encoding="utf-8", xml_declaration=True)
    (folder / "installed-hashes.json").write_text(json.dumps({
        str(p.relative_to(native.TARGET)): seed_test.digest(p)
        for p in native.TARGET.rglob("*") if p.is_file()}, indent=2))
    if (native.PROFILE / "x4lan-debug.txt").exists():
        shutil.copy2(native.PROFILE / "x4lan-debug.txt", folder / "before.log")
    print("Installed pristine-seed admission for", slot, "in isolated profile 45841.")


def installed_save(run, slot):
    state, manifest = identity(run)
    config = (native.TARGET / "ui/config.lua").read_text()
    if (f'seed_id="{state["seed_id"]}"' not in config or f'slot="{slot}"' not in config
            or "enabled=true" not in config or "seed_verified=true" not in config):
        raise RuntimeError("Installed seed role differs from this run.")
    files = list((native.PROFILE / "save").glob("*.xml*"))
    if (len(files) != 1 or files[0].name not in ("save_010.xml", "save_010.xml.gz")
            or seed_test.digest(files[0]) != manifest["sha256"]):
        raise RuntimeError("Only the run-owned verified seed may be loaded or removed.")
    return state, manifest, files[0]


def finish(run, slot):
    native.setup.ensure_closed()
    state, manifest, owned = installed_save(run, slot)
    folder = run / ("play-" + slot)
    shutil.copy2(native.PROFILE / "x4lan-debug.txt", folder / "native.log")
    log = (folder / "native.log").read_text(errors="replace")
    expected = f"SEED_PLAY_READY seed={state['seed_id']} slot={slot} controlled=true ownAlive=true proxyAlive=true"
    if log.count(expected) != 1 or log.count("SEED_GATE READY seed=" + state["seed_id"]) != 1:
        raise RuntimeError("Native admission/player readiness absent or duplicated; save retained.")
    if any(term in log for term in ("STOPPED", "Error in MD cue md.X4LAN", "Lua error")):
        raise RuntimeError("Native adapter/MD error; save retained.")
    match = re.search(r"SEED_PLAY_NATIVE seed=(\w+) slot=(\d) host=(\w+) tradeHost=(\w+) "
                      r"cargoHost=(\w+) questHost=(\w+) shuttles=(\d+) markets=(\d+)", log)
    truth = ("1", "true") if slot == "p1" else ("0", "false")
    if (not match or match[1] != state["seed_id"] or match[2] != slot[-1]
            or any(v not in truth for v in match.groups()[2:6])
            or match.groups()[6:] != ("2", "2")):
        raise RuntimeError("Native role/fixture readback differs; save retained.")
    before = json.loads((folder / "saves-before.json").read_text())
    after = native.saves()
    (folder / "saves-after.json").write_text(json.dumps(after, indent=2))
    without_owned = {p: v for p, v in after.items() if p != str(owned)}
    if before.keys() != without_owned.keys():
        raise RuntimeError("Unexpected save inventory change; save retained.")
    for path, value in before.items():
        if (value["sha256"] != without_owned[path]["sha256"]
                or (Path(path).name != "steam_autocloud.vdf"
                    and value["mtime_ns"] != without_owned[path]["mtime_ns"])):
            raise RuntimeError("Preexisting save changed; save retained.")
    owned.unlink()
    result = dict(slot=slot, native_seed_loaded=True, own_pilot_correct=True,
                  native_authority_roles_correct=True, shared_shuttles=2, markets=2,
                  seed_sha256=manifest["sha256"], normal_saves_unchanged=True,
                  two_native_acceptance=False, ongoing_world_authority=False)
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "launch", "finish"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--slot", choices=("p1", "p2"), required=True)
    args = parser.parse_args()
    if args.action == "install":
        install(args.run, args.slot)
    elif args.action == "launch":
        native.setup.ensure_closed()
        installed_save(args.run, args.slot)
        subprocess.run(["steam"] + native.setup.GAME_ARGS, check=True)
    else:
        finish(args.run, args.slot)
