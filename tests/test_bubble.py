import unittest
from x4lan.bubble import BubbleSession
from test_host import TOKEN,P1,P2,FOREIGN,hello

class BubbleTests(unittest.TestCase):
 def setUp(self):
  self.s=BubbleSession(TOKEN,'test','bubble')
  self.a=self.s.receive(hello(),P1,0)[0].decode().split('|')[3]
  self.b=self.s.receive(hello('p2','2'*32),P2,0)[0].decode().split('|')[3]
  self.world=[100,100,0,100,100,0,100,100,100,100,200,1,1,0,0,0,0]
 def publish(self,lease=None,address=P1,rev=1,values=None):
  packet=f'WORLD|1|{lease or self.a}|{rev}|'+'|'.join(map(str,values or self.world))
  self.s.receive(packet.encode(),address,.1)
 def action(self,request='c'*32,verb='PICKUP',address=P2,now=.2):
  return self.s.receive(f'ACTION|1|{TOKEN}|p2|{request}|{verb}|energycells'.encode(),address,now)
 def test_world_only_from_host_and_monotonic(self):
  self.publish(self.b,P2);self.assertIsNone(self.s.world)
  self.publish(address=FOREIGN);self.assertIsNone(self.s.world)
  self.publish();self.assertEqual(self.s.world[10],200)
  v=self.world.copy();v[10]=0
  self.publish(rev=0,values=v);self.assertEqual(self.s.world[10],200)
 def test_action_is_not_a_cargo_transaction_in_relay(self):
  self.publish();self.action();self.action()
  self.assertEqual(len(self.s.commands),1);self.assertEqual(self.s.world[5],0)
  self.assertEqual(sum(b'COMMAND|' in p for _,p in self.s.snapshots(.3)),1)
  self.s.receive(f'DONE|1|{self.b}|{"c"*32}|OK'.encode(),P2,.4)
  self.assertEqual(self.s.commands['c'*32]['result'],'PENDING')
  self.s.receive(f'DONE|1|{self.a}|{"c"*32}|OK'.encode(),P1,.5)
  self.assertEqual(self.action()[0],f'RESULT|1|{"c"*32}|OK'.encode())
  self.assertFalse(any(b'COMMAND|' in p for _,p in self.s.snapshots(.6)))
 def test_wrong_controller_ip_and_changed_request_rejected(self):
  self.publish();self.assertIn(b'NO_PLAYER',self.action(address=('10.0.0.2',777))[0])
  self.action();self.assertEqual(self.action(verb='TRADE'),[])
 def test_host_disconnect_cancels_ambiguous_command(self):
  self.publish();self.action();self.s.expire(31)
  self.assertIsNone(self.s.world)
  self.assertEqual(self.s.commands['c'*32]['result'],'CANCELLED')
 def test_fire_replays_do_not_extend_trigger(self):
  self.s.receive(f'FIRE|1|{self.b}|4'.encode(),P2,.1)
  self.s.receive(f'FIRE|1|{self.b}|4'.encode(),P2,.7)
  self.assertFalse(any(p.startswith(b'FIRE|') for _,p in self.s.snapshots(.7)))
 def test_invalid_world_rejected(self):
  for index,value in [(0,101),(2,1.5),(11,2),(10,float('nan'))]:
   v=self.world.copy();v[index]=value;self.publish(values=v)
   self.assertIsNone(self.s.world)
 def test_readiness_probe_does_not_claim_slot(self):
  s=BubbleSession(TOKEN,'test','bubble')
  response=s.receive(f'PING|1|{TOKEN}|{"f"*32}'.encode(),P1,0)
  self.assertTrue(response[0].startswith(b'PONG|'));self.assertFalse(s.peers);self.assertIsNone(s.sector)
 def test_result_can_be_retrieved_after_requester_disconnect_without_reexecution(self):
  self.publish();self.action()
  self.s.expire(31)
  result=self.action(now=31.1)
  self.assertEqual(result,[b'RESULT|1|'+b'c'*32+b'|CANCELLED'])
  self.assertFalse(any(b'COMMAND' in packet for _,packet in self.s.snapshots(31.1)))
 def test_duplicate_completed_request_survives_new_client_lease(self):
  self.publish();self.action()
  self.s.receive(('DONE|1|'+self.a+'|'+'c'*32+'|OK').encode(),P1,.3)
  self.s.expire(31)
  self.s.receive(hello('p2','9'*32),P2,31.1)
  result=self.action(now=31.2)
  self.assertEqual(result,[b'RESULT|1|'+b'c'*32+b'|OK'])
  self.assertEqual(len(self.s.commands),1)
