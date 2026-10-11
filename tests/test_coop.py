import unittest
from x4lan.coop import CoopSession
from test_host import TOKEN,P1,P2,FOREIGN,hello
from test_economy import SNAP

class CoopTests(unittest.TestCase):
 def setUp(self):
  self.s=CoopSession(TOKEN,'test','bubble')
  self.a=self.s.receive(hello(),P1,0)[0].decode().split('|')[3]
  self.b=self.s.receive(hello('p2','2'*32),P2,0)[0].decode().split('|')[3]
 def crew(self,slot=1,seq=1,control=None,aboard=None,now=.1):
  c=slot if control is None else control;b=c if aboard is None else aboard
  return self.s.receive(f'CREW|1|{self.a if slot==1 else self.b}|{seq}|{c}|{b}'.encode(),P1 if slot==1 else P2,now)
 def nav(self,slot=1,entity=1,epoch=1,seq=1,sector='cluster_14_sector001_macro',x=120000,address=None,now=.2):
  return self.s.receive(f'NAV|1|{self.a if slot==1 else self.b}|{entity}|{epoch}|{seq}|{sector}|{x}|10000|0|0|0|0|0|0'.encode(),address or (P1 if slot==1 else P2),now)
 def test_navigation_crosses_old_boundary_and_sector_without_identity_change(self):
  self.crew();self.nav();self.assertEqual(self.s.navigation[1]['fields'][4],'120000')
  self.nav(seq=2,sector='cluster_01_sector001_macro',x=900000)
  packets=[p.decode().split('|') for a,p in self.s.snapshots(.3) if a==P2 and p.startswith(b'NAV|')]
  self.assertEqual(len(packets),1);self.assertEqual(packets[0][3],self.a)
  self.assertEqual(packets[0][7],'cluster_01_sector001_macro')
  self.assertEqual(self.s.epochs[1],1)
 def test_foreign_wrong_owner_and_bad_coordinates_never_move_ship(self):
  self.nav(address=FOREIGN);self.nav(slot=2);self.nav(x='nan');self.nav(x=50000001)
  self.nav(sector='bad sector');self.nav(epoch=2)
  self.assertFalse(self.s.navigation)
  self.nav(seq=2);old=self.s.navigation[1].copy();self.nav(seq=1,x=0)
  self.assertEqual(self.s.navigation[1],old)
 def test_pilot_exclusivity_transfer_and_stale_epoch_rejection(self):
  self.crew();self.crew(2)
  self.assertTrue(self.crew(2,seq=2,control=1)[0].startswith(b'PILOT_DENIED'))
  self.assertEqual(self.s.owners[1],'p1');self.assertEqual(self.s.pilot(1),'p1')
  self.crew(seq=2,control=0,aboard=1);self.crew(2,seq=3,control=1)
  self.assertEqual(self.s.owners[1],'p2');self.assertEqual(self.s.epochs[1],2)
  self.nav();self.assertNotIn(1,self.s.navigation)
  self.nav(slot=2,epoch=2);self.assertEqual(self.s.navigation[1]['lease'],self.b)
  self.crew(seq=1);self.assertEqual(self.s.pilot(1),'p2')
  self.assertFalse(any(p.startswith(b'NAV|') for _,p in self.s.snapshots(1.5)))
 def test_expired_pilot_does_not_block_takeover_or_restore_old_authority(self):
  self.crew();self.s.receive(f'IDLE|1|{self.b}|1'.encode(),P2,29)
  self.s.expire(31);self.crew(2,seq=2,control=1,now=31.1)
  self.assertEqual(self.s.owners[1],'p2')
  answer=self.s.receive(f'RESUME|1|{TOKEN}|test|sector_macro|bubble|p1|{"1"*32}|{self.a}|{"3"*32}'.encode(),P1,31.2)
  self.assertTrue(answer[0].startswith(b'WELCOME'))
  self.nav(now=31.3);self.assertNotIn(1,self.s.navigation)
  self.assertEqual(self.s.pilot(1),'p2')
 def test_trade_uses_actual_ship_and_blocks_mid_order_takeover(self):
  self.s.receive(f'ECON|1|{self.a}|1|1|1|{SNAP}'.encode(),P1,.1)
  self.crew(control=0);self.crew(2,control=1)
  req='c'*32
  order=f'COOPORDER|1|{self.b}|{req}|{self.a}|1|2|m1|energycells,10,1600'.encode()
  self.assertTrue(self.s.receive(order,P2,.3)[0].endswith(b'PENDING'))
  packets=[p.decode().split('|') for _,p in self.s.snapshots(.4) if p.startswith(b'COOP_EXEC')]
  self.assertEqual(packets[0][4],'1');self.assertEqual(packets[0][5],'m1')
  self.crew(2,seq=2,control=0);self.crew(seq=2,control=1)
  self.assertEqual(self.s.owners[1],'p2')
  self.s.receive(f'ORDER_DONE|1|{self.a}|{req}|OK'.encode(),P1,.5)
  self.crew(seq=3,control=1);self.assertEqual(self.s.owners[1],'p1')
  self.assertTrue(self.s.receive(order,P2,.6)[0].endswith(b'OK'))
  self.assertEqual(len(self.s.orders),1)
 def test_body_sector_generation_and_host_only_suit_kill(self):
  body=f'BODY2|1|{self.b}|1|suit|1|1|cluster_01_sector001_macro|800000|0|0|0|0|0'.encode()
  self.s.receive(body,FOREIGN,.1);self.assertFalse(self.s.bodies)
  self.s.receive(body,P2,.1)
  self.s.receive(f'SUITKILL|1|{self.b}|1'.encode(),P2,.2);self.assertFalse(self.s.suit_kills)
  self.s.receive(f'SUITKILL|1|{self.a}|1'.encode(),P1,.3)
  packets=[p for _,p in self.s.snapshots(.4)]
  self.assertTrue(any(p.startswith(b'BODY2|') and b'cluster_01_sector001_macro' in p for p in packets))
  self.assertTrue(any(p.startswith(b'SUITKILL|') for p in packets))
  self.assertFalse(any(p.startswith(b'BODY|') for p in packets))
 def test_container_pickup_follows_transferred_ship_and_reserves_it_until_done(self):
  self.s.world=(100,100,0,100,100,0,100,100,100,100,300,1,1,0,0,0,0)
  self.crew(control=0);self.crew(2,control=1)
  request='d'*32
  action=f'ACTION|1|{TOKEN}|p2|{request}|PICKUP|energycells'.encode()
  reply=self.s.receive(action,P2,.2)
  self.assertTrue(reply[0].endswith(b'PENDING'))
  self.assertEqual(self.s.commands[request]['ship'],1)
  command=[p.decode().split('|') for _,p in self.s.snapshots(.3) if p.startswith(b'COMMAND|')]
  self.assertEqual(command[0][4],'p1') # Stable entity 1, although player 2 requested it.
  self.crew(2,seq=2,control=0);self.crew(seq=2,control=1)
  self.assertEqual(self.s.owners[1],'p2')
  self.s.receive(f'DONE|1|{self.a}|{request}|OK'.encode(),P1,.4)
  self.crew(seq=3,control=1);self.assertEqual(self.s.owners[1],'p1')
  self.assertTrue(self.s.receive(action,P2,.5)[0].endswith(b'OK'))
  self.assertEqual(len(self.s.commands),1)
 def test_container_pickup_on_foot_is_rejected_without_an_engine_command(self):
  self.s.world=(100,100,0,100,100,0,100,100,100,100,300,1,1,0,0,0,0)
  self.crew(2,control=0,aboard=1)
  request='e'*32
  reply=self.s.receive(f'ACTION|1|{TOKEN}|p2|{request}|PICKUP|energycells'.encode(),P2,.2)
  self.assertTrue(reply[0].endswith(b'REJECTED'))
  self.assertFalse(any(p.startswith(b'COMMAND|') for _,p in self.s.snapshots(.3)))
 def test_legacy_ship_channels_do_not_override_new_authority(self):
  for message in [f'STATE|1|{self.a}|1|0|0|0|0|0|0',f'FIRE|1|{self.a}|1',f'DOCKSTATE|1|{self.a}|1|m1']:
   self.s.receive(message.encode(),P1,.1)
  self.assertIsNone(self.s.peers['p1'].pose);self.assertFalse(self.s.fire);self.assertFalse(self.s.docks)
 def test_metrics_are_authenticated_finite_and_monotonic(self):
  good=f'PERF|1|{self.a}|2|12|83|150|ship|cluster_14_sector001_macro'.encode()
  self.s.receive(good,FOREIGN,.1);self.assertFalse(self.s.game_metrics)
  self.s.receive(good,P1,.2);old=self.s.game_metrics[self.a].copy()
  self.s.receive(good.replace(b'|2|12|',b'|1|12|'),P1,.3)
  self.s.receive(good.replace(b'|2|12|',b'|3|nan|'),P1,.4)
  self.assertEqual(self.s.game_metrics[self.a],old)
