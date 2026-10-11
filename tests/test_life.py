import unittest
from pathlib import Path
from x4lan.economy import EconomySession
from test_host import TOKEN,P1,P2,FOREIGN,hello
from test_game_adapter import MOCK,LuaRuntime
from test_bubble_adapter import EXTRA

UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'

class BodyProtocolTests(unittest.TestCase):
    def setUp(self):
        self.s=EconomySession(TOKEN,'test','bubble')
        self.a=self.s.receive(hello(),P1,0)[0].decode().split('|')[3]
        self.b=self.s.receive(hello('p2','2'*32),P2,0)[0].decode().split('|')[3]
    def body(self,seq=1,mode='suit',dead=1,generation=1,pose='1|2|3|0|0|0',address=P2):
        return self.s.receive(f'BODY|1|{self.b}|{seq}|{mode}|{dead}|{generation}|{pose}'.encode(),address,.1)
    def test_authenticated_bounded_monotonic_body_and_death(self):
        self.body(address=FOREIGN);self.assertFalse(self.s.bodies)
        for mode,dead,generation,pose in [('invalid',1,1,'0|0|0|0|0|0'),('suit',2,1,'0|0|0|0|0|0'),('suit',1,0,'0|0|0|0|0|0'),('suit',1,1,'nan|0|0|0|0|0'),('suit',1,1,'50001|0|0|0|0|0')]:
            self.body(mode=mode,dead=dead,generation=generation,pose=pose)
            self.assertFalse(self.s.bodies)
        self.body();old=self.s.bodies[self.b]
        self.body(seq=2,dead=0);self.assertEqual(self.s.bodies[self.b],old)
        self.body(seq=1,mode='foot');self.assertEqual(self.s.bodies[self.b],old)
        packets=self.s.snapshots(.2)
        self.assertTrue(any(address==P1 and packet.startswith(f'BODY|1|{self.a}|{self.b}|1|suit|1|1|'.encode()) for address,packet in packets))
    def test_suit_kill_retries_only_for_matching_generation_from_host(self):
        self.body()
        self.s.receive(f'SUITKILL|1|{self.b}|1'.encode(),P2,.2);self.assertFalse(self.s.suit_kills)
        self.s.receive(f'SUITKILL|1|{self.a}|2'.encode(),P1,.3);self.assertFalse(self.s.suit_kills)
        self.s.receive(f'SUITKILL|1|{self.a}|1'.encode(),P1,.4)
        for now in (.5,.6):self.assertTrue(any(p.startswith(b'SUITKILL') for _,p in self.s.snapshots(now)))
        self.body(seq=2,mode='foot')
        self.assertFalse(any(p.startswith(b'SUITKILL') for _,p in self.s.snapshots(.7)))
        self.body(seq=3,generation=2)
        self.assertFalse(any(p.startswith(b'SUITKILL') for _,p in self.s.snapshots(.8)))

    def test_walking_coordinates_are_authenticated_bounded_monotonic_and_expire(self):
        def walk(seq=1,context=2,pose='1|2|3|90|0|0',address=P2):
            self.s.receive(f'WALK|1|{self.b}|{seq}|{context}|{pose}'.encode(),address,.1)
        walk(address=FOREIGN)
        for context,pose in [(5,'0|0|0|0|0|0'),(1,'nan|0|0|0|0|0'),(1,'10001|0|0|0|0|0'),(1,'0|0|0|361|0|0')]:
            walk(context=context,pose=pose)
        self.assertFalse(self.s.walkers)
        walk();old=self.s.walkers[self.b];walk(context=0)
        self.assertEqual(old,self.s.walkers[self.b])
        self.assertTrue(any(a==P1 and p.startswith(f'WALK|1|{self.a}|{self.b}|1|2|'.encode()) for a,p in self.s.snapshots(.2)))
        self.assertFalse(any(p.startswith(b'WALK') for _,p in self.s.snapshots(1.2)))

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class LifeAdapterTests(unittest.TestCase):
    def make(self,slot='p1'):
        lua=LuaRuntime(unpack_returned_tuples=True);lua.execute(MOCK+EXTRA)
        lua.globals().X4LAN_CONFIG.slot=slot
        lua.execute('''
          nextObject=42
          api.SpawnObjectAtPos2=function(macro,sector,p,owner)
            local n=nextObject;nextObject=nextObject+1;positions[n]=p;return n
          end
          api.SetObjectSectorPos=function(object,sector,p)
            assert(object~=11 and object~=77);positions[object]=p;writes[#writes+1]=p
          end
        ''')
        lua.execute((UI/'bubble_game.lua').read_text());lua.execute((UI/'life_game.lua').read_text())
        lua.execute("events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate()")
        lua.execute('''function body(seq,mode,generation)
         client:receiveExtension({'BODY','1',client.lease,string.rep('b',32),tostring(seq),mode,'1',tostring(generation),'5','6','7','0','0','0'},now)
        end''')
        return lua
    def test_remote_suit_not_duplicated_replaced_on_generation_and_removed_on_foot(self):
        l=self.make();l.execute('''
          body(1,'suit',1);now=10.1;frames.onUpdate()
          local suit=X4LAN_BUBBLE_API.life.remoteSuit;assert(suit==43)
          body(2,'suit',1);now=10.2;frames.onUpdate();assert(X4LAN_BUBBLE_API.life.remoteSuit==suit)
          body(1,'foot',1);frames.onUpdate();assert(X4LAN_BUBBLE_API.life.remoteSuit==suit)
          body(3,'foot',1);frames.onUpdate();assert(X4LAN_BUBBLE_API.life.remoteSuit==nil and positions[suit]==nil)
          body(4,'suit',2);frames.onUpdate();assert(X4LAN_BUBBLE_API.life.remoteSuit==44)
          assert(countRequest('RemoteSuit')==2 and not X4LAN_BUBBLE_API.state.failed)
        ''')
    def test_delayed_kill_cannot_affect_next_suit_or_a_player_on_station(self):
        l=self.make('p2');l.execute('''
          controlled=77;positions[77]=shipPos
          events['X4LAN_Life.State'](nil,'suit|1|2')
          client:receiveExtension({'SUITKILL','1',client.lease,string.rep('b',32),'1'},now)
          assert(countRequest('SuitDeath')==0)
          client:receiveExtension({'SUITKILL','1',client.lease,string.rep('b',32),'2'},now)
          assert(countRequest('SuitDeath')==1)
          events['X4LAN_Life.State'](nil,'foot|1|2')
          client:receiveExtension({'SUITKILL','1',client.lease,string.rep('b',32),'2'},now)
          assert(countRequest('SuitDeath')==1)
        ''')
    def test_local_suit_pose_is_separate_from_destroyed_original_ship(self):
        l=self.make();l.execute('''
          positions[11]=nil;controlled=77;positions[77]=shipPos
          events['X4LAN_Life.State'](nil,'suit|1|1');now=10.2;frames.onUpdate()
          assert(lastPose==nil and closeCount==0 and not X4LAN_BUBBLE_API.state.failed)
          local found=false;for _,p in ipairs(sent) do if p:find('|suit|1|1|100|-9800|300|',1,true) then found=true end end
          assert(found)
        ''')

    def test_suit_control_can_finish_after_the_md_state_event(self):
        l=self.make();l.execute('''
          controlled=0;events['X4LAN_Life.State'](nil,'suit|1|1')
          now=10.2;frames.onUpdate();assert(X4LAN_BUBBLE_API.life.suit==nil)
          controlled=77;positions[77]=shipPos;now=10.4;frames.onUpdate()
          assert(X4LAN_BUBBLE_API.life.suit==77)
          local found=false;for _,p in ipairs(sent) do if p:find('|suit|1|1|100|-9800|300|',1,true) then found=true end end
          assert(found and not X4LAN_BUBBLE_API.state.failed)
        ''')

    def test_walking_avatar_moves_hides_on_stale_data_and_survives_reordered_packets(self):
        l=self.make();l.globals().X4LAN_CONFIG.trading=True
        for disabled in (None,False):
            l.globals().X4LAN_CONFIG.walking_avatars=disabled
            l.execute((UI/'walk_game.lua').read_text())
            l.execute("assert(X4LAN_BUBBLE_API.walk==nil and events['X4LAN_Walk.Pose']==nil)")
        l.globals().X4LAN_CONFIG.walking_avatars=True
        l.execute((UI/'walk_game.lua').read_text());l.execute('''
          X4LAN_BUBBLE_API.trade={state={ready=true},frame=function() end,receive=function() return false end}
          now=10.1;frames.onUpdate();assert(countRequest('Setup')>=1)
          events['X4LAN_Walk.Pose'](nil,'2|1|2|3|90|0|0');now=10.4;frames.onUpdate()
          local found=false;for _,p in ipairs(sent) do if p:find('WALK|1|',1,true) then found=true end end;assert(found)
          local f={'WALK','1',client.lease,string.rep('b',32),'1','2','1','2','3','90','0','0'}
          client:receiveExtension(f,now);frames.onUpdate();assert(countRequest('Move')==1)
          client:receiveExtension(f,now+.1);assert(countRequest('Move')==1)
          f[5]='2';f[6]='5';client:receiveExtension(f,now+.2);assert(countRequest('Move')==1)
          now=12;frames.onUpdate();assert(countRequest('Hide')==1)
          f[6]='0';client:receiveExtension(f,now);frames.onUpdate();assert(countRequest('Move')==2)
          now=13.2;frames.onUpdate();assert(countRequest('Hide')==1)
        ''')
