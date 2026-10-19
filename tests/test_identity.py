"""Host-only metadata validation and narrowly scoped LuaJIT C lookup binding."""
from pathlib import Path
import unittest
from x4lan.universe import UniverseSession, Baseline
from test_host import TOKEN, P1, P2, FOREIGN
from test_game_adapter import LuaRuntime

UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'

class IdentityRelayTests(unittest.TestCase):
 def setUp(self):
  self.s=UniverseSession(TOKEN,'world1-test','ids');self.leases={}
  for slot,addr in (('p1',P1),('p2',P2)):
   raw=f'HELLO|1|{TOKEN}|world1-test|sector_macro|ids|{slot}|{slot[-1]*32}'.encode()
   self.leases[slot]=self.s.receive(raw,addr,0)[0].decode().split('|')[3]
  self.a,self.b=self.leases['p1'],self.leases['p2']
 def packet(self,scope='P',id=1,seq=1,name='Shared ship'):
  return f'IDENT|1|{self.a}|{self.a}|{seq}|{scope}|{id}|ABC-123|{name.encode().hex()}|1|1'.encode()
 def test_host_identity_scope_monotonicity_and_no_unknown_npc(self):
  self.s.receive(self.packet(),FOREIGN,.1);self.assertFalse(self.s.identities.rows)
  self.s.receive(self.packet().replace(self.a.encode(),self.b.encode()),P2,.1);self.assertFalse(self.s.identities.rows)
  self.s.receive(self.packet(scope='N'),P1,.1);self.assertFalse(self.s.identities.rows)
  self.s.receive(self.packet(seq=2),P1,.1)
  self.s.receive(self.packet(seq=1,name='Stale name'),P1,.2)
  result=self.s.identities.snapshots(.3);self.assertEqual(len(result),1)
  self.assertIn(b'|2|P|1|ABC-123|',result[0][1]);self.assertEqual(result[0][0],P2)
  self.assertFalse(self.s.identities.snapshots(3))
 def test_controls_invalid_utf8_and_cross_scope_id_rejected(self):
  for p in (self.packet(id=3),self.packet(scope='fixture'),self.packet(name='bad\nname'),self.packet().replace(b'5368617265642073686970',b'ff')):
   self.s.receive(p,P1,.1)
  self.assertFalse(self.s.identities.rows)
 def test_fixture_scope_is_bounded_and_distinct_from_player_and_world_ids(self):
  for scope in ('P','F'):
   self.s.receive(self.packet(scope=scope),P1,.1)
  self.s.receive(self.packet(scope='F',id=5),P1,.1)
  self.assertEqual(set(self.s.identities.rows),{('P',1),('F',1)})
  self.assertEqual(len(self.s.identities.snapshots(.2)),2)
 def test_removed_universe_identity_is_not_replayed(self):
  self.s.entity_ids={'sector_macro':{5}}
  self.s.receive(self.packet(scope='N',id=5),P1,.1)
  self.assertEqual(len(self.s.identities.snapshots(.2)),1)
  self.s.entity_ids={'sector_macro':set()}
  self.assertEqual(self.s.identities.snapshots(.4),[])

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class DisplayIdentityTests(unittest.TestCase):
 def test_existing_c_namespace_references_observe_binding_and_keep_native_fallback(self):
  l=LuaRuntime(unpack_returned_tuples=True)
  l.execute('''
   local ffi=require('ffi')
   ffi.cdef[[typedef uint64_t UniverseID;
    const char* GetObjectIDCode(UniverseID) asm("strerror");
    size_t strlen(const char*);]]
   debug=nil -- X4 does not expose the debug library.
   existingC=ffi.C;nativeCode=ffi.string(existingC.GetObjectIDCode(11))
   events={};requests={}
   RegisterEvent=function(k,f) events[k]=f end
   AddUITriggeredEvent=function(s,c,a) requests[#requests+1]={control=c,args=a} end
   DebugError=function() end
   X4LAN_CONFIG={world_sync=true,slot='p2'}
   X4LAN_BUBBLE_API={state={},id=function(v) return v end,
    universe={receive=function() end,frame=function() end}}
  ''')
  l.execute((UI/'universe_protocol.lua').read_text());l.execute((UI/'identity_game.lua').read_text())
  l.execute('''
   local I=X4LAN_BUBBLE_API.identity;local ffi=require('ffi')
   assert(I.hooked and I.bind(11,'ABC-123','Shared ship'))
   collectgarbage('collect')
   assert(ffi.string(existingC.GetObjectIDCode(11))=='ABC-123')
   assert(existingC.strlen('unrelated')==9)
   assert(ffi.string(existingC.GetObjectIDCode(12))~='ABC-123')
   assert(not I.bind(11,'bad|packet','Wrong'))
   I.unbind(11);assert(ffi.string(existingC.GetObjectIDCode(11))==nativeCode)
   assert(#requests==1 and requests[1].control=='Describe')
  ''')

 def test_market_state_can_arrive_late_without_stopping_host_or_guest_identities(self):
  for slot in ('p1','p2'):
   with self.subTest(slot=slot):
    l=LuaRuntime(unpack_returned_tuples=True)
    l.execute('''
     sent,requests,events={}, {}, {}
     local api={GetObjectIDCode=function(id) return 'ABC-'..id end,GetComponentName=function(id) return 'Fixture '..id end,
      IsComponentOperational=function(id) return id~=nil and id~=0 end,
      GetObjectPositionInSector=function() return {x=0,y=0,z=0} end,
      GetContextByClass=function() return 9 end,SetObjectForcedRadarVisible=function() end}
     local ffi={C=api,cdef=function() end,string=function(v) return v end,new=function(_,_,v) return v end}
     require=function() return ffi end
     DebugError=function() end;ConvertIDTo64Bit=tonumber
     RegisterEvent=function(k,f) events[k]=f end
     AddUITriggeredEvent=function(s,c,a) requests[#requests+1]={c,a} end
     GetComponentData=function(id,k) if k=='maxradarrange' then return 10000 end;return true,true end
     X4LAN_CONFIG={world_sync=true}
     X4LAN_BUBBLE_API={state={client={lease='epoch'},transport={send=function(_,p) sent[#sent+1]=p end}},
      coop={ready=true,ships={11,12}},id=function(v) return v end,paused=function() return false end,
      universe={ready=true,latest={epoch='epoch'},publish={},replicas={},receive=function() end,frame=function() end}}
    ''')
    l.globals().X4LAN_CONFIG.slot=slot
    l.execute((UI/'universe_protocol.lua').read_text());l.execute((UI/'identity_game.lua').read_text())
    l.execute('''
     A=X4LAN_BUBBLE_API;U=A.universe;I=A.identity
     U.receive({'IDENT','1','lease','epoch','1','F','3','ABC-33','4d61726b6574','1','1'},0)
     U.frame(0);assert(not I.failed)
     A.trade={};U.frame(.2);assert(not I.failed)
     A.trade.state={markets={},ready=false};U.frame(.4);assert(not I.failed)
     A.trade.state.markets[1]='33';A.trade.state.ready=true
     U.frame(.6);assert(not I.failed)
     if X4LAN_CONFIG.slot=='p1' then
      local market,player=false,false
      for _,p in ipairs(sent) do market=market or p:find('|F|3|ABC-33|',1,true);player=player or p:find('|P|1|',1,true) end
      assert(market and player,'delayed fixture cannot stop other identities')
     else assert(I.bound['33'] and I.bound['33'].name=='Market','queued identity binds when the fixture arrives') end
    ''')
