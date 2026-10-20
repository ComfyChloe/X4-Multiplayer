"""Cross-PC test-session preparation and pinned transfer, without a game."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from dev import shared_seed_session as shared
from x4lan import seed


class SharedSeedSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.extension = self.root / "extension"
        self.extension.mkdir()
        (self.extension / "content.xml").write_text("<content/>")
        (self.extension / "config.lua").write_text("inert")
        self.source = self.root / "source.xml"
        self.source.write_bytes(b"<savegame>disposable fixture</savegame>")
        self.bundle = self.root / "sealed"
        self.manifest = seed.create_bundle(self.source, self.bundle, seed_id="b" * 32,
            game_build="world1-test", mod_sha256=shared.production_digest(self.extension),
            extensions=[dict(id="x4_lan_lab", version="8")])
        self.pinned = self.root / "manifest.json"
        self.pinned.write_text(json.dumps(self.manifest))
        self.session = self.root / "package-session.json"
        self.session.write_text(json.dumps(dict(token="a" * 32, build="old", bubble="old")),
                                encoding="utf-8-sig")

    def prepare(self, name):
        run = self.root / name
        shared.prepare(run, self.pinned, self.session, self.extension)
        return run

    def transfer(self, run):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        errors = []
        def serve():
            try:
                connection, address = listener.accept()
                with connection:
                    shared.serve_connection(connection, address, peer="127.0.0.1",
                                            bundle=self.bundle, token="a" * 32)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            result = shared.receive(run, self.extension, *listener.getsockname())
        finally:
            worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        return result

    def test_matched_private_packages_produce_same_new_session_and_exact_tcp_copy(self):
        host, guest = self.prepare("host"), self.prepare("guest")
        self.assertEqual((host / "play-session.json").read_bytes(),
                         (guest / "play-session.json").read_bytes())
        cfg = shared.read_json(host / "play-session.json")
        self.assertEqual((cfg["bubble"], cfg["build"]), ("b" * 32, "world1-test"))
        self.assertNotIn("token", shared.read_json(host / "manifest.json"))
        shared.adopt(host, self.bundle, self.extension)
        self.assertEqual(self.transfer(guest), self.manifest)
        self.assertEqual((host / "received/seed.bin").read_bytes(),
                         (guest / "received/seed.bin").read_bytes())

    def test_changed_production_and_invalid_key_refuse_before_preparation(self):
        (self.extension / "content.xml").write_text("<different/>")
        with self.assertRaises(seed.SeedError):
            self.prepare("changed")
        self.assertFalse((self.root / "changed").exists())
        (self.extension / "content.xml").write_text("<content/>")
        self.session.write_text('{"token":"bad"}')
        with self.assertRaises(seed.SeedError):
            self.prepare("invalid")
        self.assertFalse((self.root / "invalid").exists())

    def test_signed_but_unexpected_snapshot_is_not_published_for_staging(self):
        changed = {**self.manifest, "sha256": "d" * 64}
        self.pinned.write_text(json.dumps(changed))
        run = self.prepare("guest")
        with self.assertRaises(seed.SeedError):
            self.transfer(run)
        self.assertFalse((run / "received").exists())
        self.assertFalse((run / "receive-result.json").exists())
        self.assertTrue((run / "received-transfer/manifest.json").exists())

    def test_existing_run_and_unexpected_peer_preserve_existing_data(self):
        run = self.prepare("host")
        before = (run / "play-session.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.prepare("host")
        self.assertEqual((run / "play-session.json").read_bytes(), before)
        # Refuse the address before even accessing a connection or bundle.
        with self.assertRaises(seed.SeedError):
            shared.serve_connection(None, ("127.0.0.2", 1), peer="127.0.0.1",
                                    bundle=self.bundle, token="a" * 32)


if __name__ == "__main__":
    unittest.main()
