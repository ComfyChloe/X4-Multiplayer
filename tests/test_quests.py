import unittest
from x4lan.quests import parse_progress
import test_coop
from test_host import P1,P2,FOREIGN
import test_coop_adapter as adapter

class QuestProtocolTests(unittest.TestCase):
 def setUp(self):
  fixture=test_coop.CoopTests();fixture.setUp();self.s,self.a,self.b=fixture.s,fixture.a,fixture.b
 def put(self,progress,slot=1,addr=None,now=.1):
  return self.s.receive(('QUEST|1|'+(self.a if slot==1 else self.b)+'|'+progress).encode(),addr or (P1 if slot==1 else P2),now)
 def test_only_host_can_publish_progress_or_completion(self):
  self.put('1|3|20|2|1',slot=2);self.put('1|3|20|2|1',addr=FOREIGN)
  self.assertIsNone(self.s.quest.progress)
  self.put('1|1|0|0|0');self.assertEqual(self.s.quest.progress,(1,1,0,0,0))
 def test_duplicate_reordered_or_regressing_progress_never_rolls_back(self):
  self.put('1|1|10|0|0');self.put('2|2|20|1|0');self.put('3|3|20|2|1')
  for p in ['2|2|20|1|0','4|1|10|0|0','3|3|20|2|1','5|3|19|2|1','6|2|20|2|0']:
   self.put(p)
  self.assertEqual(self.s.quest.progress,(3,3,20,2,1))
 def test_completion_is_replayed_to_late_peer_without_new_reward_command(self):
  self.put('10|3|20|2|1')
  packets=[p for a,p in self.s.snapshots(.2) if a==P2 and p.startswith(b'QUEST|')]
  self.assertEqual(packets,[f'QUEST|1|{self.b}|{self.a}|10|3|20|2|1'.encode()])
  self.assertFalse(any(p.startswith(b'QUEST|') for a,p in self.s.snapshots(.3)))
  self.assertTrue(any(p.startswith(b'QUEST|') for a,p in self.s.snapshots(.8)))
  self.s.receive(f'IDLE|1|{self.b}|1'.encode(),P2,29)
  self.s.expire(31)
  self.assertFalse(any(p.startswith(b'QUEST|') for a,p in self.s.snapshots(31)))
 def test_malformed_states_cannot_create_a_valid_reward(self):
  for p in ['1|0|0|0|0','1|3|20|2|0','1|2|20|2|0','1|1|20|0|0','1|3|20|2|2','nan|1|0|0|0','1|1|21|0|0']:
   with self.assertRaises(ValueError):parse_progress(p.split('|'))

@unittest.skipUnless(adapter.LuaRuntime,'LuaJIT unavailable')
class QuestAdapterTests(unittest.TestCase):
 def make(self):
  l=adapter.CoopAdapterTests().make('p2');l.execute((adapter.UI/'quest_game.lua').read_text())
  l.execute('''X4LAN_BUBBLE_API.trade={state={ready=true},frame=function() end}
   now=11;frames.onUpdate();events['X4LAN_Quest.Ready']()
   function quest(rev,stage,n,k,paid)
    client:receiveExtension({'QUEST','1',client.lease,string.rep('a',32),tostring(rev),tostring(stage),tostring(n),tostring(k),tostring(paid)},now)
   end''');return l
 def test_guest_mirrors_delivery_combat_completion_once_and_no_reward(self):
  l=self.make();l.execute('''
   quest(1,1,10,0,0);now=11.1;frames.onUpdate();assert(countRequest('Apply')==1)
   quest(2,2,20,1,0);now=11.2;frames.onUpdate();assert(countRequest('Apply')==2)
   quest(3,3,20,2,1);now=11.3;frames.onUpdate();assert(countRequest('Apply')==3)
   quest(3,3,20,2,1);quest(2,2,20,1,0);quest(4,1,0,0,0);now=11.4;frames.onUpdate()
   assert(countRequest('Apply')==3)
   for _,p in ipairs(sent) do assert(not p:find('QUEST|',1,true)) end
  ''')
