"""Opt-in host-save feasibility experiment in isolated profile 45841.

Only this harness's newly generated save_010 may be loaded or removed. It never
selects a personal save. Gameplay adapters stay disabled; this is not a playable
shared-world mode. Native capture, real TCP transfer and native reload are
separate steps with retained evidence.
"""
from pathlib import Path
import argparse
import hashlib
import json
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dev import native_world_test as native
from dev.validate_native_seed import validate
from x4lan import seed


def digest(path):
    with Path(path).open("rb") as stream:
        return seed._digest(stream)


def content_identity():
    h = hashlib.sha256()
    for path in sorted((ROOT / "extension/x4_lan_lab").rglob("*")):
        if path.is_file() and path.name != "config.lua":
            h.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    enabled = {e.get("id"): e.get("enabled") == "true"
               for e in ET.parse(native.PROFILE / "content.xml").getroot().findall("extension")}
    extensions = []
    for path in (native.GAME / "extensions").glob("*/content.xml"):
        info = ET.parse(path).getroot()
        if enabled.get(info.get("id"), False):
            extensions.append({"id": info.get("id"), "version": info.get("version", "0")})
    return dict(game_build="world1-9.00-steam23660954", mod_sha256=h.hexdigest(),
                extensions=sorted(extensions, key=lambda row: row["id"]))


def install(run, mode):
    native.setup.ensure_closed()
    native.setup.check_game(native.GAME, "23660954")
    marker = native.TARGET / ".x4lan-package-id"
    if native.TARGET.is_symlink() or not marker.is_file() or not marker.read_text().startswith("lan-"):
        raise RuntimeError("Install the known internal package before using the seed probe.")
    if ET.parse(native.PROFILE / "config.xml").getroot().findtext("autosave") != "false":
        raise RuntimeError("The isolated profile must have autosave disabled.")
    if mode == "capture":
        if native.PROFILE.is_symlink() or (native.PROFILE / "save").is_symlink() or list((native.PROFILE / "save").glob("*.xml*")):
            raise RuntimeError("Capture requires an empty isolated test-save directory.")
        run.mkdir()
        state = dict(seed_id=secrets.token_hex(16), **content_identity())
        (run / "state.json").write_text(json.dumps(state, indent=2))
        (run / "saves-before.json").write_text(json.dumps(native.saves(), indent=2))
        shutil.copytree(native.TARGET, run / "extension-before")
        for name in ("config.xml", "content.xml", "x4lan-debug.txt"):
            if (native.PROFILE / name).exists():
                shutil.copy2(native.PROFILE / name, run / ("before-" + name))
    else:
        state = json.loads((run / "state.json").read_text())
        if content_identity() != {k: state[k] for k in ("game_build", "mod_sha256", "extensions")}:
            raise RuntimeError("Game content changed between seed capture and reload.")
        manifest, _ = seed.read_bundle(run / "received")
        for k in ("seed_id", "game_build", "mod_sha256", "extensions"):
            if manifest[k] != state[k]:
                raise RuntimeError("Received seed identity differs.")
        seed.stage_test_save(run / "received", native.PROFILE)
    # Remove only known opt-in diagnostics; retain the package marker.
    for probe in (ROOT / "dev").glob("*probe.*"):
        folder = {".lua": "ui", ".xml": "md"}.get(probe.suffix)
        if folder:
            (native.TARGET / folder / probe.name).unlink(missing_ok=True)
    shutil.copytree(ROOT / "extension/x4_lan_lab", native.TARGET, dirs_exist_ok=True)
    config = dict(enabled=False, test_profile="45841", seed_probe=mode, seed_id=state["seed_id"])
    (native.TARGET / "ui/config.lua").write_text("X4LAN_CONFIG={\n" + "".join(
        f" {k}={json.dumps(v)},\n" for k, v in config.items()) + "}\n")
    for suffix, folder in (("lua", "ui"), ("xml", "md")):
        shutil.copy2(ROOT / ("dev/seed_probe." + suffix), native.TARGET / folder / ("seed_probe." + suffix))
    tree = ET.parse(native.TARGET / "ui.xml")
    ET.SubElement(tree.find("environment"), "file", name="ui/seed_probe.lua")
    tree.write(native.TARGET / "ui.xml", encoding="utf-8", xml_declaration=True)
    (run / (mode + "-installed-hashes.json")).write_text(json.dumps({
        str(p.relative_to(native.TARGET)): digest(p) for p in native.TARGET.rglob("*") if p.is_file()}, indent=2))
    if (native.PROFILE / "x4lan-debug.txt").exists():
        shutil.copy2(native.PROFILE / "x4lan-debug.txt", run / ("before-" + mode + ".log"))
    print("Installed", mode, "probe; gameplay disabled; isolated profile 45841.")


def seal_and_transfer(run):
    native.setup.ensure_closed()
    state = json.loads((run / "state.json").read_text())
    log = (native.PROFILE / "x4lan-debug.txt").read_text(errors="replace")
    shutil.copy2(native.PROFILE / "x4lan-debug.txt", run / "capture.log")
    if not any(f"SEED_PROBE saved success={flag} id={state['seed_id']}" in log for flag in ("true", "1")):
        raise RuntimeError("Native save completion for this seed was not observed.")
    files = list((native.PROFILE / "save").glob("*.xml*"))
    if len(files) != 1 or files[0].name not in ("save_010.xml", "save_010.xml.gz"):
        raise RuntimeError("Unexpected save inventory; nothing removed.")
    source = files[0]
    manifest = seed.create_bundle(source, run / "sealed", **state)
    # The private key is generated for this isolated local transfer, never logged.
    token = secrets.token_hex(16)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(10)
    errors = []
    received = None
    def serve():
        try:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(10)
                seed.send_bundle(connection, run / "sealed", token)
        except Exception as error:
            errors.append(str(error))
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        with socket.create_connection(server.getsockname(), timeout=10) as client:
            received = seed.receive_bundle(client, run / "received", token, **state)
    finally:
        worker.join(15)
        server.close()
    if errors or worker.is_alive() or received != manifest:
        raise RuntimeError("Local TCP transfer did not complete: " + repr(errors))
    # Archive and remove ONLY the exact new save that this run just generated.
    archived = run / ("native-capture" + (".xml.gz" if source.suffix == ".gz" else ".xml"))
    shutil.copy2(source, archived)
    if digest(source) != manifest["sha256"] or digest(archived) != manifest["sha256"]:
        raise RuntimeError("Captured save changed; retained in profile.")
    source.unlink()
    (run / "transfer.json").write_text(json.dumps(dict(
        native_save_sha256=manifest["sha256"], size=manifest["size"],
        real_loopback_tcp=True, received_verified=True, gameplay_acceptance=False), indent=2))
    print("Native seed transferred and checksum-verified; capture archived, test slot freed.")


def launch(run):
    native.setup.ensure_closed()
    state = json.loads((run / "state.json").read_text())
    config = (native.TARGET / "ui/config.lua").read_text()
    if f'seed_id="{state["seed_id"]}"' not in config or "enabled=false" not in config:
        raise RuntimeError("Installed seed probe does not match this run.")
    if 'seed_probe="load"' in config:
        manifest, _ = seed.read_bundle(run / "received")
        files = list((native.PROFILE / "save").glob("*.xml*"))
        if len(files) != 1 or files[0].name not in ("save_010.xml", "save_010.xml.gz") or digest(files[0]) != manifest["sha256"]:
            raise RuntimeError("Only the exact transferred test save may be loaded.")
    elif list((native.PROFILE / "save").glob("*.xml*")):
        raise RuntimeError("Capture slot is already occupied; not starting.")
    subprocess.run(["steam"] + native.setup.GAME_ARGS, check=True)


def verify_load(run):
    """Preserve native evidence, validate, then remove only our unchanged copy."""
    native.setup.ensure_closed()
    state = json.loads((run / "state.json").read_text())
    shutil.copy2(native.PROFILE / "x4lan-debug.txt", run / "load.log")
    result = validate((run / "capture.log").read_text(errors="replace"),
                      (run / "load.log").read_text(errors="replace"), state["seed_id"])
    manifest, _ = seed.read_bundle(run / "received")
    files = list((native.PROFILE / "save").glob("*.xml*"))
    if (len(files) != 1 or files[0].name not in ("save_010.xml", "save_010.xml.gz")
            or digest(files[0]) != manifest["sha256"]):
        raise RuntimeError("Loaded test slot differs; nothing removed.")
    before = json.loads((run / "saves-before.json").read_text())
    after = native.saves()
    (run / "saves-after-load.json").write_text(json.dumps(after, indent=2))
    without_seed = {p: v for p, v in after.items() if p != str(files[0])}
    if before.keys() != without_seed.keys():
        raise RuntimeError("Unexpected save inventory change; nothing removed.")
    for path, values in before.items():
        if (values["sha256"] != without_seed[path]["sha256"]
                or (Path(path).name != "steam_autocloud.vdf"
                    and values["mtime_ns"] != without_seed[path]["mtime_ns"])):
            raise RuntimeError("A preexisting save changed; nothing removed.")
    result.update(json.loads((run / "transfer.json").read_text()))
    result["preexisting_save_bytes_and_timestamps_unchanged"] = True
    # Both sealed and received copies retain these exact bytes for reinspection.
    files[0].unlink()
    (run / "saves-after-cleanup.json").write_text(json.dumps(native.saves(), indent=2))
    result["owned_test_slot_removed"] = True
    (run / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install-capture", "install-load", "launch", "transfer", "verify-load"))
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    if args.action.startswith("install-"):
        install(args.run, args.action.removeprefix("install-"))
    elif args.action == "launch":
        launch(args.run)
    elif args.action == "verify-load":
        verify_load(args.run)
    else:
        seal_and_transfer(args.run)
