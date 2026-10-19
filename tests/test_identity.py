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
