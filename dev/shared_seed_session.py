"""Prepare and transfer an opt-in shared native seed; never install or launch X4.

Both testers use the private key from their already matched internal package.
Only the pinned, non-secret snapshot manifest belongs in Git. Prepared sessions,
received snapshots and transfer logs belong under .local or another private path.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from x4lan import seed

IDENTITY_KEYS = ("seed_id", "game_build", "mod_sha256", "extensions")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def production_digest(extension):
    extension = Path(extension)
    if not (extension / "content.xml").is_file():
        raise seed.SeedError("production extension is absent")
    digest = hashlib.sha256()
    for path in sorted(extension.rglob("*")):
        if path.is_file() and path.name != "config.lua":
            name = "extension/x4_lan_lab/" + path.relative_to(extension).as_posix()
            digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def verified_manifest(path, extension):
    manifest = seed.validate_manifest(read_json(path))
    if production_digest(extension) != manifest["mod_sha256"]:
        raise seed.SeedError("production bytes differ from the pinned snapshot; use the agreed source and line endings")
    return manifest


def prepare(run, manifest_path, session_path, extension, port=45843):
    manifest = verified_manifest(manifest_path, extension)
    original = read_json(session_path)
    token = original.get("token")
    seed._key(token)
    if not 1 <= port <= 65535:
        raise seed.SeedError("invalid relay port")
    session = dict(token=token, build=manifest["game_build"], bubble=manifest["seed_id"],
                   port=port, coop=True, fire2=True, world_sync=True, world_mode="replica",
                   ship_macro="ship_ter_s_xperimental_01_a_macro",
                   ship_loadout="x4lan_coop_xperimental", ship_weapons=6)
    run = Path(run)
    run.mkdir(mode=0o700)
    for name, value in (("manifest.json", manifest), ("play-session.json", session),
                        ("state.json", {k: manifest[k] for k in IDENTITY_KEYS})):
        path = run / name
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        path.chmod(0o600)
    return manifest


def prepared(run, extension):
    run = Path(run)
    manifest = verified_manifest(run / "manifest.json", extension)
    session = read_json(run / "play-session.json")
    seed._key(session.get("token"))
    if session.get("build") != manifest["game_build"] or session.get("bubble") != manifest["seed_id"]:
        raise seed.SeedError("prepared session differs from the pinned snapshot")
    return manifest, session


def adopt(run, bundle, extension):
    """Host-only private copy; publish no profile or game installation changes."""
    manifest, _ = prepared(run, extension)
    sealed, snapshot = seed.read_bundle(bundle)
    if sealed != manifest:
        raise seed.SeedError("local bundle differs from the pinned manifest")
    destination = Path(run) / "received"
    destination.mkdir(mode=0o700)
    shutil.copy2(snapshot, destination / "seed.bin")
    shutil.copy2(Path(bundle) / "manifest.json", destination / "manifest.json")
    seed.read_bundle(destination)


def serve_connection(connection, address, *, peer, bundle, token):
    if address[0] != peer:
        raise seed.SeedError("unexpected transfer peer")
    connection.settimeout(20)
    seed.send_bundle(connection, bundle, token)


def serve(run, bundle, extension, bind, peer, port=45844, duration=900):
    manifest, session = prepared(run, extension)
    sealed, _ = seed.read_bundle(bundle)
    if sealed != manifest:
        raise seed.SeedError("served bundle differs from the pinned manifest")
    bind, peer = str(ipaddress.IPv4Address(bind)), str(ipaddress.IPv4Address(peer))
    if bind == "0.0.0.0" or not 0 < duration <= 7200:
        raise seed.SeedError("select a specific interface and a bounded transfer window")
    deadline = time.monotonic() + duration
    attempts = []
    with socket.socket() as listener:
        listener.bind((bind, port))
        listener.listen(2)
        listener.settimeout(1)
        print(f"Seed transfer listening on {bind}:{port}, permitted peer {peer}; no game launched.", flush=True)
        while time.monotonic() < deadline and len(attempts) < 64:
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            with connection:
                try:
                    serve_connection(connection, address, peer=peer, bundle=bundle, token=session["token"])
                except (OSError, seed.SeedError) as error:
                    attempts.append(dict(peer=address[0], sent=False, error=type(error).__name__))
                    continue
            attempts.append(dict(peer=address[0], sent=True))
            Path(run, "serve-result.json").write_text(json.dumps(dict(
                attempts=attempts, sha256=manifest["sha256"], sent_bytes=manifest["size"],
                receiver_verification_required=True), indent=2))
            return
    raise seed.SeedError("transfer window expired without an authenticated send")


def receive(run, extension, host, port=45844):
    manifest, session = prepared(run, extension)
    host = str(ipaddress.IPv4Address(host))
    incoming = Path(run) / "received-transfer"
    destination = Path(run) / "received"
    if destination.exists():
        raise seed.SeedError("received bundle already exists")
    with socket.create_connection((host, port), timeout=20) as connection:
        actual = seed.receive_bundle(connection, incoming, session["token"],
                                     **{k: manifest[k] for k in IDENTITY_KEYS})
    if actual != manifest:
        raise seed.SeedError("received bytes differ from the pinned snapshot; do not stage this bundle")
    # Publish only after comparison with the independently pinned full manifest.
    # mkdir refuses a concurrently created destination; its manifest is last.
    destination.mkdir(mode=0o700)
    shutil.move(incoming / "seed.bin", destination / "seed.bin")
    shutil.move(incoming / "manifest.json", destination / "manifest.json")
    incoming.rmdir()
    Path(run, "receive-result.json").write_text(json.dumps(dict(
        received_verified=True, sha256=actual["sha256"], size=actual["size"],
        host=host, native_load_tested=False), indent=2))
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("prepare", "adopt", "serve", "receive"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--extension", type=Path, default=ROOT / "extension/x4_lan_lab")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--session", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--bind")
    parser.add_argument("--peer")
    parser.add_argument("--port", type=int, default=45844)
    parser.add_argument("--duration", type=float, default=900)
    args = parser.parse_args()
    required = {"prepare": ("manifest", "session"), "adopt": ("bundle",),
                "serve": ("bundle", "bind", "peer"), "receive": ("host",)}[args.action]
    if any(getattr(args, key) is None for key in required):
        parser.error("required for this action: " + ", ".join("--" + key for key in required))
    if args.action == "prepare":
        prepare(args.run, args.manifest, args.session, args.extension)
    elif args.action == "adopt":
        adopt(args.run, args.bundle, args.extension)
    elif args.action == "serve":
        serve(args.run, args.bundle, args.extension, args.bind, args.peer, args.port, args.duration)
    else:
        receive(args.run, args.extension, args.host, args.port)
    print("Completed", args.action, "without changing any game installation or save profile.")


if __name__ == "__main__":
    main()
