"""Cargo request replay, authority and reconciliation across the real relay."""
import unittest
from x4lan.universe import UniverseSession
from test_host import TOKEN, P1, P2, FOREIGN


class CargoRelayTests(unittest.TestCase):
    def setUp(self):
        self.s=UniverseSession(TOKEN,'world1-test','cargo')
        self.leases={}
        for slot,addr in (('p1',P1),('p2',P2)):
            packet=f'HELLO|1|{TOKEN}|world1-test|sector_macro|cargo|{slot}|{slot[-1]*32}'.encode()
            self.leases[slot]=self.s.receive(packet,addr,0)[0].decode().split('|')[3]
            lease=self.leases[slot]
            self.s.receive(f'CREW|1|{lease}|1|{slot[-1]}|{slot[-1]}'.encode(),addr,.01)
        self.a,self.b=self.leases['p1'],self.leases['p2']
        self.id='d'*32

    def request(self,ship=2,epoch=1,amount=20):
        return f'CARGOREQ|1|{self.b}|{self.id}|{self.a}|{ship}|{epoch}|DROP|0|energycells|{amount}'.encode()

    def executions(self,now):
        return [p for _,p in self.s.snapshots(now) if p.startswith(b'CARGOEXEC')]

    def test_retry_is_idempotent_and_completion_retains_visibility_fence(self):
        packet=self.request()
        self.assertIn(b'PENDING',self.s.receive(packet,P2,.1)[0])
        self.assertEqual(len(self.executions(.1)),1)
        self.assertEqual(self.executions(.2),[])
        self.assertEqual(len(self.executions(.4)),1)
        self.s.receive(f'CARGODONE|1|{self.a}|{self.id}|OK|7'.encode(),P1,.5)
        self.assertTrue(self.s.receive(packet,P2,.6)[0].endswith(b'|OK|7'))
        self.assertEqual(self.executions(.7),[])
        self.assertTrue(self.s.receive(self.request(amount=21),P2,.8)[0].endswith(b'|REJECTED|0'))

    def test_foreign_endpoint_wrong_ship_epoch_and_forged_completion_rejected(self):
        self.assertFalse(self.s.receive(self.request(),FOREIGN,.1))
        self.assertIn(b'REJECTED',self.s.receive(self.request(ship=1),P2,.1)[0])
        self.assertIn(b'REJECTED',self.s.receive(self.request(epoch=2),P2,.1)[0])
        self.s.receive(self.request(),P2,.1)
        self.s.receive(f'CARGODONE|1|{self.b}|{self.id}|OK|7'.encode(),P2,.2)
        self.assertEqual(self.s.cargo.requests[self.id]['result'],'PENDING')

    def test_pending_cargo_blocks_ship_handoff(self):
        self.s.receive(self.request(),P2,.1)
        self.s.receive(f'CREW|1|{self.b}|2|0|2'.encode(),P2,.2)
        denied=self.s.receive(f'CREW|1|{self.a}|2|2|2'.encode(),P1,.3)
        self.assertTrue(denied[0].startswith(b'PILOT_DENIED'))
        self.s.receive(f'CARGODONE|1|{self.a}|{self.id}|OK|7'.encode(),P1,.4)
        self.s.receive(f'CREW|1|{self.a}|3|2|2'.encode(),P1,.5)
        self.assertEqual(self.s.owners[2],'p1')

    def test_host_loss_is_uncertain_and_does_not_reexecute(self):
        self.s.receive(self.request(),P2,.1)
        self.s.receive(f'IDLE|1|{self.b}|1'.encode(),P2,29)
        self.s.expire(31)
        self.assertEqual(self.executions(31),[])
        self.assertIn(b'UNCERTAIN',self.s.receive(self.request(),P2,31.1)[0])

    def test_inventory_is_host_only_monotonic_and_stale_state_is_not_replayed(self):
        packet=f'CINV|1|{self.a}|{self.a}|2|energycells|10|20'.encode()
        self.s.receive(packet,FOREIGN,.1)
        self.s.receive(packet.replace(self.a.encode(),self.b.encode()),P2,.1)
        self.assertFalse(self.s.cargo.inventory)
        self.s.receive(packet,P1,.2)
        self.s.receive(packet.replace(b'|2|energy',b'|1|energy'),P1,.3)
        rows=[p for _,p in self.s.snapshots(.4) if p.startswith(b'CINV')]
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0].endswith(b'|2|energycells|10|20'))
        self.assertFalse([p for _,p in self.s.snapshots(3) if p.startswith(b'CINV')])
