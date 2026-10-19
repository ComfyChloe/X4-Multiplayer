"""Native boundary doubles plus exact LuaJIT 64-bit conversion; no engine claims."""
from pathlib import Path
import unittest
from test_game_adapter import LuaRuntime

UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class CargoAdapterTests(unittest.TestCase):
    def make(self,slot='p2'):
        l=LuaRuntime(unpack_returned_tuples=True)
        l.execute('''
         events={};calls={};sent={};now=10
         RegisterEvent=function(k,f) events[k]=f end
         AddUITriggeredEvent=function(s,c,a) calls[#calls+1]={control=c,args=a} end
         DebugError=function() end;GetCurRealTime=function() return now end
         X4LAN_CONFIG={world_sync=true,slot='p2'}
         X4LAN_BUBBLE_API={state={client={lease=string.rep('b',32)},transport={send=function(self,p) sent[#sent+1]=p end}},
          paused=function() return false end,id=function(v) return tostring(v) end,
          coop={ready=true,owners={'p1','p2'},epochs={1,1}},
          universe={revision=4,replicas={},replicaKeys={},nextId=0,latest={epoch=string.rep('a',32),revision=4},receive=function() return false end,frame=function() end}}
        ''')
        l.globals().X4LAN_CONFIG.slot=slot
        l.execute((UI/'universe_protocol.lua').read_text());l.execute((UI/'cargo_game.lua').read_text())
        l.execute("A=X4LAN_BUBBLE_API;G=A.cargo;U=A.universe;U.frame(now);events['X4LAN_Cargo.Ready']()")
        return l

    def test_hex_object_preserves_all_64_bits(self):
        l=self.make();l.execute('''
         assert(tostring(G.object('0xffffffffffffffff'))=='18446744073709551615ULL')
         assert(tostring(G.object('0x20000000000001'))=='9007199254740993ULL')
         assert(G.object('0x10000000000000000')==nil and G.object('1e20')==nil)
        ''')

    def test_guest_prediction_requests_retry_same_id_and_hold_drop_until_commit(self):
        l=self.make();l.execute('''
         events['X4LAN_Cargo.GuestPickup'](nil,'77|2|energycells|20|20')
         assert(#G.queue==1 and G.held[77]);U.frame(now);local first=sent[#sent]
         now=now+.3;U.frame(now);assert(sent[#sent]==first)
         local request=G.queue[1].request
         U.receive({'CARGOREPLY','1',A.state.client.lease,request,'PENDING','0'},now);assert(#G.queue==1)
         U.receive({'CARGOREPLY','1',A.state.client.lease,request,'OK','7'},now)
         assert(#G.queue==0 and G.held[77].fence==7)
         local r={entity={id=77,macro='crate_macro',ware='energycells',amount=20,pose={0,0,0,0,0,0}}}
         local before=#calls;G.ensure(r,22);assert(#calls==before)
         U.latest.revision=7;G.ensure(r,22);assert(calls[#calls].control=='Create' and not G.held[77])
        ''')

    def test_host_duplicate_execution_and_result_wait_for_new_baseline(self):
        l=self.make('p1');l.execute('''
         local packet={'CARGOEXEC','1',A.state.client.lease,string.rep('d',32),'2','1','DROP','0','energycells','20','p2'}
         U.receive(packet,now);local before=#calls;U.receive(packet,now);assert(#calls==before)
         events['X4LAN_Cargo.Result'](nil,string.rep('d',32)..'|OK')
         now=now+.3;U.frame(now);assert(#sent==0 and U.cargoDirty)
         U.revision=5;U.frame(now);assert(sent[#sent]:find('|OK|5',1,true))
         U.receive(packet,now);assert(#calls==before and #sent==2)
        ''')

    def test_inventory_requires_current_host_and_monotonic_sequence(self):
        l=self.make();l.execute('''
         local f={'CINV','1',A.state.client.lease,string.rep('a',32),'7','energycells','10','20'}
         U.receive(f,now);local before=#calls;assert(calls[before].control=='Inventory')
         U.receive(f,now);f[5]='6';U.receive(f,now);assert(#calls==before)
         f[4]=string.rep('c',32);f[5]='8';U.receive(f,now);assert(#calls==before)
        ''')
