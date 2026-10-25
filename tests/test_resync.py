"""Authenticated STATEHASH narrowing and baseline-only repair; synthetic peers only."""
import unittest

from x4lan.resync import HashReconciler
from x4lan.universe import Baseline, Receiver, UniverseSession
from test_host import TOKEN, P1, P2, FOREIGN

SECTOR = 'cluster_14_sector001_macro'
OTHER = 'cluster_01_sector001_macro'
DIGESTS = dict(identity='1' * 16, pose='2' * 16, health='3' * 16, rng='4' * 16, time='5' * 16)


def row(i=1):
    return f'{i},ship,ship_arg_s_scout_01_a_macro,argon,{i},2,3,0,.1,-.1,none,0'


class ResyncRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = UniverseSession(TOKEN, 'world1-test', 'test')
        self.a = self.join('p1', P1, '1' * 32)
        self.b = self.join('p2', P2, '2' * 32)
        self.s.receive(f'UINTEREST|1|{self.b}|1|{SECTOR}'.encode(), P2, .01)

    def join(self, slot, address, nonce):
        packet = f'HELLO|1|{TOKEN}|world1-test|{SECTOR}|test|{slot}|{nonce}'.encode()
        return self.s.receive(packet, address, 0)[0].decode().split('|')[3]

    def hash(self, slot, frame=1, sector=SECTOR, epoch=None, **changes):
        lease = self.a if slot == 'p1' else self.b
        values = dict(DIGESTS)
        values.update(changes)
        if sector == 'global':
            fields = ['-', '-', '-', values['rng'], values['time']]
        else:
            fields = [values['identity'], values['pose'], values['health'], '-', '-']
        return f"UHASH|1|{lease}|{epoch or self.a}|{frame}|{sector}|{'|'.join(fields)}".encode()

    def pair(self, frame=1, sector=SECTOR, **changes):
        self.s.receive(self.hash('p1', frame, sector), P1, .1)
        self.s.receive(self.hash('p2', frame, sector, **changes), P2, .1)

    def requests(self, now):
        return [p for a, p in self.s.snapshots(now) if a == P1 and p.startswith(b'URESYNC|')]

    def publish(self, baseline):
        for i in range(len(baseline.rows) + 2):
            self.s.receive(baseline.packet(self.a, i), P1, .2)

    def test_authentication_epoch_and_payload_validation(self):
        self.s.receive(self.hash('p1'), FOREIGN, .1)
        self.s.receive(self.hash('p2', epoch='f' * 32), P2, .1)
        self.s.receive(self.hash('p1', identity='x' * 16), P1, .1)
        self.pair()
        self.assertEqual(self.requests(.1), [])

    def test_bucket_narrowing_dedup_rate_limit_and_retry(self):
        self.pair(pose='f' * 16)
        first = self.requests(.1)
        self.assertEqual(len(first), 1)
        self.assertIn(f'|{SECTOR}|0|pose'.encode(), first[0])
        self.s.receive(self.hash('p2', pose='f' * 16), P2, .2)
        self.pair(frame=2, pose='f' * 16)
        self.assertEqual(self.requests(.2), [])
        retry = self.requests(.36)
        self.assertEqual(retry, first)
        self.assertEqual(self.requests(.5), [])

    def test_reordered_hash_reports_align_and_repair_once(self):
        self.s.receive(self.hash('p2', frame=2, health='f' * 16), P2, .1)
        self.s.receive(self.hash('p1', frame=1), P1, .1)
        self.s.receive(self.hash('p1', frame=2), P1, .1)
        self.assertEqual(len(self.requests(.1)), 1)
        self.s.receive(self.hash('p2', frame=2, health='f' * 16), P2, .2)
        self.assertEqual(self.requests(.2), [])

    def test_conflicting_duplicate_is_not_comparison_evidence(self):
        self.s.receive(self.hash('p1'), P1, .1)
        self.s.receive(self.hash('p1', pose='f' * 16), P1, .1)
        self.s.receive(self.hash('p2'), P2, .1)
        self.assertEqual(self.requests(.1), [])

    def test_only_interested_sector_is_repaired_and_change_cancels_retry(self):
        self.pair(sector=OTHER, health='f' * 16)
        self.assertEqual(self.requests(.1), [])
        self.pair(health='f' * 16)
        self.assertEqual(len(self.requests(.1)), 1)
        self.s.receive(f'UINTEREST|1|{self.b}|2|{OTHER}'.encode(), P2, .2)
        self.assertEqual(self.requests(.4), [])

    def test_global_rng_time_mismatch_uses_bounded_sector_baseline_only(self):
        self.pair(sector='global', rng='f' * 16, time='e' * 16)
        request = self.requests(.1)
        self.assertEqual(len(request), 1)
        self.assertTrue(request[0].endswith(f'|{SECTOR}|0|rng,time'.encode()))

    def test_missing_then_complete_baseline_satisfies_and_delivers_repair(self):
        self.pair(identity='f' * 16)
        request = self.requests(.1)[0].decode().split('|')
        self.assertEqual(request[6], '0')
        baseline = Baseline.create(self.a, 1, SECTOR, [row()])
        for i in range(len(baseline.rows) + 1):
            self.s.receive(baseline.packet(self.a, i), P1, .2)
        self.assertTrue(self.s.resync.repairs)
        self.s.receive(baseline.packet(self.a, len(baseline.rows) + 1), P1, .2)
        self.assertFalse(self.s.resync.repairs)
        packets = [p for a, p in self.s.snapshots(.3) if a == P2 and p.startswith((b'UBEGIN|', b'UOBJECT|', b'UCOMMIT|'))]
        self.assertTrue(packets and packets[0].startswith(b'UBEGIN|'))

    def test_fresh_commit_reuses_normal_reliable_delivery_under_loss(self):
        original = Baseline.create(self.a, 1, SECTOR, [row()])
        self.publish(original)
        self.pair(pose='f' * 16)
        self.requests(.1)
        fresh = Baseline.create(self.a, 2, SECTOR, [row(2)])
        self.publish(fresh)
        receiver = Receiver()
        dropped = True
        for tick in range(100):
            now = .3 + tick * .05
            for address, packet in self.s.snapshots(now):
                if address != P2 or not packet.startswith((b'UBEGIN|', b'UOBJECT|', b'UCOMMIT|')):
                    continue
                if dropped and packet.startswith(b'UBEGIN|'):
                    dropped = False
                    continue
                f = packet.decode().split('|')
                ack, _ = receiver.receive(f, now)
                if ack is not None:
                    self.s.receive(f'UACK|1|{self.b}|{f[3]}|{f[4]}|{ack}'.encode(), P2, now)
            if self.s.delivered == 2:
                break
        self.assertEqual(receiver.latest, fresh)
        self.assertEqual(self.s.delivered, 2)

    def test_cargo_retry_and_fence_survive_sector_resync(self):
        for slot, lease, address in (('p1', self.a, P1), ('p2', self.b, P2)):
            self.s.receive(f'CREW|1|{lease}|1|{slot[-1]}|{slot[-1]}'.encode(), address, .02)
        request = 'd' * 32
        cargo = f'CARGOREQ|1|{self.b}|{request}|{self.a}|2|1|DROP|0|energycells|20'.encode()
        self.s.receive(cargo, P2, .03)
        self.pair(identity='f' * 16)
        packets = self.s.snapshots(.1)
        self.assertTrue(any(p.startswith(b'URESYNC|') for _, p in packets))
        self.assertTrue(any(p.startswith(b'CARGOEXEC|') for _, p in packets))
        self.s.receive(f'CARGODONE|1|{self.a}|{request}|OK|7'.encode(), P1, .2)
        self.publish(Baseline.create(self.a, 7, SECTOR, [row()]))
        self.assertTrue(self.s.receive(cargo, P2, .3)[0].endswith(b'|OK|7'))
        self.assertFalse(any(p.startswith(b'CARGOEXEC|') for _, p in self.s.snapshots(.4)))


class HashReconcilerTests(unittest.TestCase):
    def test_samples_and_mismatch_history_are_bounded(self):
        r = HashReconciler()
        for frame in range(1, 400):
            r.observe('p1', 'a' * 32, frame, SECTOR, DIGESTS, frame)
        self.assertLessEqual(len(r.samples['p1']), 128)
        for frame in range(1, 400):
            r.observe('p1', 'a' * 32, frame, SECTOR, DIGESTS, frame)
            r.observe('p2', 'a' * 32, frame, SECTOR, dict(DIGESTS, pose='f' * 16), frame)
        self.assertLessEqual(len(r.mismatches), 256)

    def test_stale_or_same_revision_does_not_satisfy_request(self):
        r = HashReconciler()
        repair = r.request_repair('a' * 32, SECTOR, 4, ('identity',), 0)
        self.assertIsNotNone(repair)
        self.assertFalse(r.committed(Baseline.create('a' * 32, 4, SECTOR, [])))
        self.assertTrue(r.committed(Baseline.create('a' * 32, 5, SECTOR, [])))


if __name__ == '__main__':
    unittest.main()
