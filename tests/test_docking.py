"""Transport arbitration only; actual bay registration needs native evidence."""
import unittest
from x4lan.docking import DockSession
from test_host import TOKEN, P1, P2, FOREIGN, hello


class DockRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = DockSession(TOKEN, 'test', 'bubble')
        self.a = self.s.receive(hello(), P1, 0)[0].decode().split('|')[3]
        self.b = self.s.receive(hello('p2', '2'*32), P2, 0)[0].decode().split('|')[3]

    def dock(self, entity=1, epoch=1, seq=1, mode='EXTERNAL', dock=12, now=.1,
             lease=None, address=None):
        lease = lease or (self.a if entity == 1 else self.b)
        address = address or (P1 if entity == 1 else P2)
        return self.s.receive(f'CDOCK|1|{lease}|{entity}|{epoch}|{seq}|{mode}|{dock}'.encode(), address, now)

    def rows(self, now=.2):
        return [(a, p.decode().split('|')) for a, p in self.s.snapshots(now) if p.startswith(b'CDOCK|')]

    def test_fresh_owner_state_goes_only_to_other_peer(self):
        self.dock()
        self.assertEqual(self.rows(), [(P2, ['CDOCK','1',self.b,self.a,'1','1','1','EXTERNAL','12'])])
        self.assertEqual(self.rows(1), [])

    def test_exact_pad_claim_is_exclusive_until_explicit_free(self):
        self.dock(mode='APPROACH')
        denied = self.dock(entity=2)
        self.assertEqual(denied, [f'CDOCK_DENIED|1|{self.b}|2|1|1|12'.encode()])
        self.assertNotIn(2, self.s.coop_docks)
        self.dock(seq=2, mode='FREE', dock=0)
        self.assertEqual(self.dock(entity=2, seq=2), [])
        self.assertEqual(self.s.coop_docks[2]['claim'], 12)

    def test_stale_or_unknown_state_does_not_free_an_occupied_pad(self):
        self.dock()
        self.dock(seq=2, mode='UNKNOWN', dock=0, now=.2)
        self.assertEqual(self.s.coop_docks[1]['claim'], 12)
        self.assertTrue(self.dock(entity=2, now=5))
        self.assertEqual(self.rows(5), [])

    def test_wrong_owner_epoch_address_order_and_invalid_state_are_rejected(self):
        self.dock()
        for extra in (dict(address=FOREIGN),dict(lease=self.b,address=P2),dict(epoch=2),
                      dict(seq=0),dict(seq=1),dict(mode='FREE',dock=12),dict(mode='STORED',dock=0),
                      dict(dock=250001),dict(mode='anything'),dict(dock=-1)):
            args=dict(seq=2,mode='FREE',dock=0);args.update(extra);self.dock(**args)
        self.assertEqual(self.s.coop_docks[1]['seq'], 1)
        self.assertEqual(self.s.coop_docks[1]['mode'], 'EXTERNAL')

    def test_new_movement_epoch_invalidates_old_claim_without_reusing_old_frames(self):
        self.dock()
        self.s.owners[1], self.s.epochs[1] = 'p2', 2
        self.assertEqual(self.dock(entity=2), [])
        self.dock(entity=1,epoch=1,seq=2,lease=self.a,address=P1)
        self.assertEqual(self.s.coop_docks[1]['epoch'], 1)
        self.assertEqual([r[1][4] for r in self.rows()], ['2'])

    def test_dead_ships_do_not_reserve_or_publish(self):
        self.dock()
        self.s.world = (0,)*17
        self.assertEqual(self.rows(), [])
        self.dock(seq=2)
        self.assertEqual(self.s.coop_docks[1]['seq'], 1)

    def test_denied_newer_request_fences_delayed_free_without_refreshing_claim(self):
        self.dock(entity=1, dock=12)
        self.dock(entity=2, dock=13)
        self.assertTrue(self.dock(entity=2, seq=3, dock=12, now=.5))
        self.dock(entity=2, seq=2, mode='FREE', dock=0, now=.6)
        self.assertEqual(self.s.coop_docks[2]['claim'], 13)
        self.assertEqual(self.rows(1), [])
        self.assertTrue(self.dock(entity=1, seq=2, dock=13, now=1))
        self.dock(entity=2, seq=4, mode='FREE', dock=0, now=1.1)
        self.assertEqual(self.s.coop_docks[2]['claim'], 0)


if __name__ == '__main__':
    unittest.main()
