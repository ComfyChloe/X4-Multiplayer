"""Native boundary doubles plus exact LuaJIT 64-bit conversion; no engine claims."""
from pathlib import Path
import unittest
from test_game_adapter import LuaRuntime

UI=Path(__file__).resolve().parents[1]/'src/extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class CargoAdapterTests(unittest.TestCase):
    def make(self,slot='p2'):
        l=LuaRuntime(unpack_returned_tuples=True)
        l.execute('''
         events={};calls={};sent={};now=10;objects={};removed={}
         local realRequire=require;local realffi=require('ffi')
         local function key(v) return tostring(v):gsub('ULL$','') end
         local api={IsComponentOperational=function(v) return objects[key(v)]~=nil end,
          RemoveComponent=function(v) removed[#removed+1]=key(v);objects[key(v)]=nil end}
         require=function(name)
          if name=='ffi' then return setmetatable({C=api},{__index=realffi}) end
          return realRequire(name)
         end
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
         U.replicas[77]={creation=1,object=G.object('0x10010')}
         events['X4LAN_Cargo.GuestPickup'](nil,'77|1|0x10010|2|energycells|20|20')
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

    def test_partial_pickup_ignores_repeated_and_retired_native_callbacks(self):
        l=self.make();l.execute('''
         local event=events['X4LAN_Cargo.GuestPickup']
         U.replicas[77]={creation=1,object=G.object('0x10010')}
         local old='77|1|0x10010|2|energycells|10|10'
         event(nil,old);event(nil,old);assert(#G.queue==1 and G.counter==1)
         local request=G.queue[1].request
         U.latest.revision=7 -- The partial-quantity baseline overtakes its completion.
         U.receive({'CARGOREPLY','1',A.state.client.lease,request,'OK','7'},now)
         assert(G.held[77].fence==7)
         local r={entity={id=77,kind='drop',macro='crate_macro',ware='energycells',amount=10,pose={0,0,0,0,0,0}}}
         U.replicas[77]=r;G.creationSeq=1;G.ensure(r,22)
         objects['65568']=true;events['X4LAN_Cargo.Created'](nil,'77|2|0x10020')
         assert(not G.held[77] and #G.queue==0)
         event(nil,old);assert(#G.queue==0 and G.counter==1)
         event(nil,'77|2|0x10020|2|energycells|10|10')
         assert(#G.queue==1 and G.counter==2)
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

    def test_stale_authority_uses_existing_revision_without_requesting_a_baseline(self):
        host=self.make('p1');host.execute('''
         A.coop.epochs[2]=2;local before=#calls
         U.receive({'CARGOEXEC','1',A.state.client.lease,string.rep('d',32),'2','1','PICKUP','77','energycells','10','p2'},now)
         assert(#calls==before and not U.cargoDirty)
         assert(sent[#sent]:find('|STALE|4',1,true))
        ''')
        guest=self.make();guest.execute('''
         local r={creation=1,object=G.object('0x10010'),entity={id=77,kind='drop',macro='crate_macro',ware='energycells',amount=10,pose={0,0,0,0,0,0}}}
         U.replicas[77]=r
         events['X4LAN_Cargo.GuestPickup'](nil,'77|1|0x10010|2|energycells|10|10')
         local f={'CARGOREPLY','1',A.state.client.lease,G.queue[1].request,'STALE','4'}
         U.receive(f,now);assert(#G.queue==0 and G.held[77].fence==4 and calls[#calls].control=='Notice')
         local before=#calls;U.receive(f,now);assert(#calls==before)
         G.ensure(r,22);assert(calls[#calls].control=='Create' and not G.held[77])
         assert(U.latest.revision==4) -- No unrelated revision bump is needed.
        ''')

    def test_uncertain_outcome_notifies_once_without_replaying_a_new_intention(self):
        l=self.make();l.execute('''
         events['X4LAN_Cargo.GuestDrop'](nil,'2|energycells|10|10')
         local f={'CARGOREPLY','1',A.state.client.lease,G.queue[1].request,'UNCERTAIN','0'}
         U.receive(f,now);local before=#calls
         assert(calls[before].control=='Notice' and calls[before].args:find('do not repeat',1,true))
         U.receive(f,now);now=now+10;U.frame(now)
         assert(#calls==before and #G.queue==0 and G.counter==1 and #sent==0)
        ''')

    def test_replaced_creation_discards_stale_handle_in_either_completion_order(self):
        for reverse in (False, True):
            l=self.make();l.globals().reverse=reverse
            l.execute("""
             local function record(amount) return {entity={id=77,kind='drop',macro='crate_macro',ware='energycells',amount=amount,pose={0,0,0,0,0,0}}} end
             local first=record(20);U.replicas[77]=first;G.ensure(first,22);assert(first.creating==1)
             local second=record(10);U.replicas[77]=second;G.ensure(second,22);assert(second.creating==2)
             objects['65568']=true;objects['65552']=true
             local callback=events['X4LAN_Cargo.Created']
             if reverse then callback(nil,'77|2|0x10010');callback(nil,'77|1|0x10020')
             else callback(nil,'77|1|0x10020');callback(nil,'77|2|0x10010') end
             assert(objects['65552'] and not objects['65568'] and #removed==1)
             assert(tostring(second.object)=='65552ULL' and U.replicaKeys['65552'] and not U.replicaKeys['65568'])
             callback(nil,'77|2|0x10010');assert(#removed==1 and objects['65552'])
            """)

    def test_removed_record_discards_late_creation_and_stale_failure(self):
        l=self.make();l.execute("""
         local r={entity={id=77,kind='drop',macro='crate_macro',ware='energycells',amount=20,pose={0,0,0,0,0,0}}}
         U.replicas[77]=r;G.ensure(r,22);U.replicas[77]=nil;objects['65568']=true
         events['X4LAN_Cargo.Created'](nil,'77|1|0x10020')
         events['X4LAN_Cargo.CreateFailed'](nil,'77|1|energycells|20')
         assert(not objects['65568'] and #removed==1 and not G.failed)
         local fresh={entity=r.entity};U.replicas[77]=fresh;G.ensure(fresh,22)
         events['X4LAN_Cargo.CreateFailed'](nil,'77|1|energycells|20');assert(not G.failed)
         events['X4LAN_Cargo.CreateFailed'](nil,'77|2|energycells|20');assert(G.failed)
        """)
