import math
from pathlib import Path
import socket
import tempfile
import unittest
import xml.etree.ElementTree as ET

from prepare import prepare
from x4lan.host import Relay, Session

TOKEN = "a" * 32
P1, P2, FOREIGN = ("127.0.0.1", 10001), ("127.0.0.1", 10002), ("127.0.0.1", 10003)


def hello(slot="p1", nonce="1" * 32, token=TOKEN, build="test", sector="sector_macro", bubble="bubble"):
    return f"HELLO|1|{token}|{build}|{sector}|{bubble}|{slot}|{nonce}".encode()


def packet(lease, seq=1, pose=(1, 2, 3, 0, 0, 0)):
    return (f"STATE|1|{lease}|{seq}|" + "|".join(map(str, pose))).encode()


class HostTests(unittest.TestCase):
    def setUp(self):
        self.s = Session(TOKEN, "test", "bubble")

    def join(self, slot="p1", address=P1, nonce="1" * 32, now=0):
        return self.s.receive(hello(slot, nonce), address, now)[0].decode().split("|")[3]

    def test_two_clients_receive_only_remote_pose(self):
        lease1 = self.join()
        lease2 = self.join("p2", P2, "2" * 32)
        self.s.receive(packet(lease1, pose=(10, 20, 30, .1, .2, .3)), P1, .1)
        self.s.receive(packet(lease2, pose=(-10, -20, -30, -.1, -.2, -.3)), P2, .1)
        snapshots = dict(self.s.snapshots(.2))
        self.assertIn(b"|-10|-20|-30|", snapshots[P1])
        self.assertIn(b"|10|20|30|", snapshots[P2])
        self.assertEqual(snapshots[P1].decode().split("|")[2], lease1)

    def test_auth_build_sector_bubble_and_slot_mismatch(self):
        for message in (hello(token="b" * 32), hello(build="other"), hello(bubble="else"), hello(slot="p3")):
            self.assertEqual(self.s.receive(message, P1, 0), [])
        self.join()
        self.assertEqual(self.s.receive(hello("p2", "2"*32, sector="other"), P2, .1), [])
        self.assertEqual(len(self.s.peers), 1)

    def test_endpoint_cannot_take_another_slot(self):
        self.join()
        self.assertEqual(self.s.receive(hello("p2", "2"*32), P1, 1), [])
        self.assertEqual(self.s.receive(hello("p1", "2"*32), P2, 1), [])

    def test_source_binding_and_sequence_order(self):
        lease = self.join()
        self.s.receive(packet(lease, 3), FOREIGN, .1)
        self.assertIsNone(self.s.peers["p1"].pose)
        self.s.receive(packet(lease, 3), P1, .2)
        self.s.receive(packet(lease, 2, (99, 0, 0, 0, 0, 0)), P1, .3)
        self.assertEqual(self.s.peers["p1"].pose[0], 1)

    def test_nonfinite_and_bounds_are_rejected(self):
        lease = self.join()
        for pose in ((math.nan, 0, 0, 0, 0, 0), (math.inf, 0, 0, 0, 0, 0),
                     (50001, 0, 0, 0, 0, 0), (0, 0, 0, 10, 0, 0)):
            self.s.receive(packet(lease, 1, pose), P1, .1)
        self.assertIsNone(self.s.peers["p1"].pose)

    def test_malformed_packets_do_not_change_state(self):
        self.join()
        for raw in (b"", b"\xff", b"a"*769, b"STATE|1", b"HELLO|2|bad", b"HELLO|1|"+b"a"*700):
            self.assertEqual(self.s.receive(raw, FOREIGN, .1), [])
        self.assertEqual(len(self.s.peers), 1)

    def test_missing_state_expires_even_if_peer_heartbeats(self):
        lease = self.join()
        self.join("p2", P2, "2"*32)
        self.s.receive(packet(lease), P1, .1)
        self.s.receive(hello(), P1, 1)
        self.assertTrue(dict(self.s.snapshots(1))[P2].startswith(b"EMPTY|"))

    def test_idle_removes_pose_and_reordered_state_cannot_restore_it(self):
        lease = self.join()
        self.s.receive(packet(lease, 2), P1, .1)
        self.s.receive(f"IDLE|1|{lease}|3".encode(), P1, .2)
        self.s.receive(packet(lease, 2), P1, .3)
        self.assertIsNone(self.s.peers["p1"].pose)

    def test_expiry_new_lease_and_old_packet_rejection(self):
        old = self.join()
        self.s.expire(30)
        self.assertEqual(self.s.receive(hello(), P1, 30), [])
        new = self.join(nonce="3"*32, now=30)
        self.assertNotEqual(old, new)
        self.s.receive(packet(old), P1, 30.1)
        self.assertIsNone(self.s.peers["p1"].pose)

    def test_thirty_second_lease_and_shorter_pose_freshness(self):
        lease = self.join()
        self.s.receive(packet(lease), P1, 0)
        self.s.expire(29.999)
        self.assertEqual(self.s.peers['p1'].lease, lease)
        self.s.expire(30)
        self.assertNotIn('p1', self.s.peers)

    def test_updates_after_twenty_second_gap_keep_lease(self):
        lease = self.join()
        self.s.receive(packet(lease), P1, 20)
        self.s.expire(49.999)
        self.assertEqual(self.s.peers['p1'].lease, lease)
        self.s.expire(50)
        self.assertNotIn('p1', self.s.peers)

    def test_lost_welcome_can_be_retried(self):
        lease = self.join()
        again = self.s.receive(hello(), P1, 1)[0].decode().split("|")[3]
        self.assertEqual(lease, again)

    def test_real_udp(self):
        relay = Relay("127.0.0.1", 0, self.s)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(1)
        try:
            client.sendto(hello(), relay.socket.getsockname())
            relay.step(0)
            data, _ = client.recvfrom(768)
            self.assertTrue(data.startswith(b"WELCOME|1|"))
        finally:
            client.close()
            relay.close()

    def test_preparation_makes_distinct_clients_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            session = prepare(output, "192.168.1.10", "9.00-test")
            for slot in ("p1", "p2"):
                root = output / slot / "extensions" / "x4_lan_lab"
                config = (root / "ui" / "config.lua").read_text()
                self.assertIn(f'slot = "{slot}"', config)
                self.assertIn(session["token"], config)
                self.assertIn('mode = "telemetry"', config)
                for xml in root.rglob("*.xml"):
                    ET.parse(xml)
            with self.assertRaises(FileExistsError):
                prepare(output, "192.168.1.10", "9.00-test")


if __name__ == "__main__":
    unittest.main()
