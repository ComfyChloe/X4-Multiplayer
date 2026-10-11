import unittest
from x4lan.economy import EconomySession,Assembly,parse_snapshot,parse_lines
from test_host import TOKEN,P1,P2,FOREIGN,hello
SNAP='M,10000000,10000000,100000000,100000000;W,energycells,1,20,20,200,300,1200,1600,2000,1800,2200,2000;W,hullparts,12,0,0,100,500,22000,25000,2000,18000,20000,2000'
class EconomyTests(unittest.TestCase):
 def setUp(self):
  self.s=EconomySession(TOKEN,'test','bubble')
  self.a=self.s.receive(hello(),P1,0)[0].decode().split('|')[3]
  self.b=self.s.receive(hello('p2','2'*32),P2,0)[0].decode().split('|')[3]
 def publish(self,lease=None,address=P1,revision=1):
  return self.s.receive(f'ECON|1|{lease or self.a}|{revision}|1|1|{SNAP}'.encode(),address,.1)
 def order(self,request='c'*32,lines='energycells,10,1600',owner=None,now=.2):
  return self.s.receive(f'ORDER|1|{self.b}|{request}|{owner or self.a}|m1|{lines}'.encode(),P2,now)
 def test_only_host_publishes_complete_snapshot(self):
  self.publish(self.b,P2);self.assertIsNone(self.s.economy)
  self.s.receive(f'ECON|1|{self.a}|1|2|2|{SNAP[80:]}'.encode(),P1,.1);self.assertIsNone(self.s.economy)
  self.s.receive(f'ECON|1|{self.a}|1|1|2|{SNAP[:80]}'.encode(),P1,.2);self.assertEqual(self.s.economy,SNAP)
 def test_snapshot_reordering_duplicate_conflict_and_bounds(self):
  a=Assembly();self.assertIsNone(a.add(1,1,2,SNAP[:80],0));self.assertIsNone(a.add(1,1,2,'corrupt',.1))
  self.assertEqual(a.add(1,2,2,SNAP[80:],.2),SNAP);self.assertIsNone(a.add(0,1,1,SNAP,.3))
  self.assertIsNone(a.add(2,9,9,SNAP,.3))
  for malformed in [SNAP.replace('10000000','-1',1),SNAP.replace('energycells','bad name'),SNAP+';'+SNAP.split(';')[1]]:
   with self.assertRaises(ValueError):parse_snapshot(malformed)
 def test_basket_contains_sell_and_buy_and_rejects_bad_numbers(self):
  self.assertEqual(parse_lines('energycells,-10,1200;hullparts,2,25000'),[('energycells',-10,1200),('hullparts',2,25000)])
  for malformed in ['energycells,nan,1','energycells,0,1','energycells,1,-1','energycells,1,2;energycells,2,3','energycells,999999999,1']:
   with self.assertRaises(ValueError):parse_lines(malformed)
 def test_order_exactly_once_and_only_host_may_finish(self):
  self.publish();self.assertTrue(self.order()[0].endswith(b'PENDING'));self.order();self.assertEqual(len(self.s.orders),1)
  self.s.receive(f'ORDER_DONE|1|{self.b}|{"c"*32}|OK'.encode(),P2,.3);self.assertEqual(self.s.orders['c'*32]['result'],'PENDING')
  self.s.receive(f'ORDER_DONE|1|{self.a}|{"c"*32}|OK'.encode(),P1,.4);self.assertTrue(self.order(now=.5)[0].endswith(b'OK'))
  self.assertEqual(self.order(lines='energycells,20,1600'),[])
 def test_stale_host_world_never_executes_and_disconnect_is_uncertain(self):
  self.publish();self.assertTrue(self.order(owner='f'*32)[0].endswith(b'UNCERTAIN'));self.assertFalse(self.s.orders)
  self.order();self.s.expire(31);self.assertEqual(self.s.orders['c'*32]['result'],'UNCERTAIN');self.assertIsNone(self.s.economy)
 def test_snapshot_and_dock_forwarding_are_bounded_and_authenticated(self):
  self.publish();self.s.receive(f'DOCKSTATE|1|{self.b}|1|m1'.encode(),FOREIGN,.1);self.assertFalse(self.s.docks)
  self.s.receive(f'DOCKSTATE|1|{self.b}|1|m1'.encode(),P2,.2)
  packets=self.s.snapshots(.3);self.assertTrue(any(b'DOCKSTATE' in data for _,data in packets));self.assertTrue(all(len(data)<=768 for _,data in packets))
  self.assertFalse(any(data.startswith(b'ECON') for _,data in self.s.snapshots(.31)))
