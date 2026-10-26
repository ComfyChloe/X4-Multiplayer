"""W01 engine doubles: scope, atomicity and protected player objects."""
from pathlib import Path
import unittest
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class UniverseAdapterTests(unittest.TestCase):
    def make(self,slot='p1',mode='replica'):
        l=LuaRuntime(unpack_returned_tuples=True);l.execute(MOCK+EXTRA)
        l.globals().X4LAN_CONFIG.slot=slot;l.globals().X4LAN_CONFIG.world_mode=mode
        l.execute('''
          X4LAN_CONFIG.coop=true;X4LAN_CONFIG.world_sync=true
          X4LAN_CONFIG.test_profile='45841';X4LAN_CONFIG.build='world1-test'
          ConvertIDTo64Bit=function(v) return tonumber(v) end
          local old=require
          require=function(name)
           local module=old(name)
           if name=='ffi' then
            local alloc=module.new
            module.new=function(name,v) if name:find('%[') then return {} else return alloc(name,v) end end
           end
           return module
          end
          sectors={[11]=22,[42]=22,[90]=22,[101]=22,[102]=22,[103]=23,[104]=22}
          for _,id in ipairs({90,101,102,103,104}) do positions[id]={x=id,y=200,z=300,yaw=0,pitch=0,roll=0} end
          api.GetContextByClass=function(id) return sectors[id] or 22 end
          api.GetAllFactions=function(out,cap,hidden) out[0]='argon';out[1]='player';return 2 end
          api.GetNumAllFactionShips=function(f) return f=='argon' and 5 or 2 end
          api.GetAllFactionShips=function(out,cap,f)
           local values=f=='argon' and {90,101,102,103,104} or {11,42}
           for i,id in ipairs(values) do out[i-1]=id end;return #values
          end
          api.IsComponentClass=function() return true end
          GetComponentData=function(id,field)
           id=tonumber(id)
           if id==22 then return 'cluster_14_sector001_macro' end
           if id==23 then return 'cluster_01_sector001_macro' end
           return 'ship_arg_s_scout_01_a_macro',id==104 and 'player' or 'argon'
          end
          local nextReplica=200;spawns={};activated={}
          api.SpawnObjectAtPos2=function(macro,sector,p,owner)
           local id
           if X4LAN_BUBBLE_API and X4LAN_BUBBLE_API.state.proxy then id=nextReplica;nextReplica=nextReplica+1
           else id=42 end
           positions[id]=p;sectors[id]=sector;spawns[#spawns+1]={id=id,macro=macro,owner=owner};return id
          end
          api.SetObjectSectorPos=function(id,sector,p) positions[id]=p;writes[#writes+1]={id=id,sector=sector,pose=p} end
          api.ActivateObject=function(id,on) activated[#activated+1]={id=id,on=on} end
        ''')
        for name in ('bubble_game.lua','motion.lua','coop_game.lua','universe_protocol.lua','universe_game.lua'):
            l.execute((UI/name).read_text())
        l.execute('''
          events['X4LAN.Loaded']();now=10;frames.onUpdate()
          events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
          frames.onUpdate();events['X4LAN_Universe.Exclude'](nil,90);events['X4LAN_Universe.Ready']()
          U=X4LAN_BUBBLE_API.universe;P=X4LAN_UNIVERSE_PROTOCOL
          function tick(n) for i=1,n do now=now+.05;frames.onUpdate() end end
          function baseline(revision,rows,complete)
           local b=P.baseline(string.rep('a',32),revision,'cluster_14_sector001_macro',rows)
           for i=0,#rows do U.receive(P.split(P.packet(b,client.lease,i),'|'),now) end
           if complete then U.receive(P.split(P.packet(b,client.lease,#rows+1),'|'),now) end
           return b
          end
          testRow='1,ship,ship_arg_s_scout_01_a_macro,argon,1500,200,300,0,0,0,none,0'
        ''')
        return l

    def test_host_enumerates_npcs_excludes_player_fixture_and_other_sector(self):
        l=self.make();l.execute('''
          U.receive({'UINTEREST','1',client.lease,'1','cluster_14_sector001_macro'},now)
          tick(20);assert(U.sender and #U.sender.baseline.rows==2)
          assert(U.sender.baseline.rows[1]:find(',101,200,300,',1,true))
          assert(U.sender.baseline.rows[2]:find(',102,200,300,',1,true))
          assert(not U.failed and #removed==0)
        ''')

    def test_host_captures_each_active_guest_sector(self):
        l=self.make();l.execute('''
          X4LAN_BUBBLE_API.coop.sectors.cluster_01_sector001_macro=23
          U.receive({'UINTEREST','1',client.lease,'1','cluster_14_sector001_macro',string.rep('b',32)},now)
          U.receive({'UINTEREST','1',client.lease,'1','cluster_01_sector001_macro',string.rep('c',32)},now)
          tick(20);U.sender.done=true;tick(100)
          assert(U.lastScans.cluster_14_sector001_macro and U.lastScans.cluster_01_sector001_macro)
          assert(U.publishes.cluster_14_sector001_macro and U.publishes.cluster_01_sector001_macro)
          assert(U.revision>=2 and not U.failed)
          assert(#U.publish==3) -- two Argon Prime ships plus one Grand Exchange ship
        ''')

    def test_partial_baseline_never_spawns_or_suppresses_npcs(self):
        l=self.make('p2');l.execute('''
          tick(1);baseline(1,{testRow},false);tick(20)
          assert(#spawns==1 and #removed==0 and U.latest==nil and not U.failed)
        ''')

    def test_changing_fleet_retries_capture_without_committing_empty_state(self):
        l=self.make();l.execute('''
          local real=api.GetAllFactionShips;local once=true
          api.GetAllFactionShips=function(out,cap,f)
           local count=real(out,cap,f)
           if once then once=false;return count-1 end
           return count
          end
          U.receive({'UINTEREST','1',client.lease,'1','cluster_14_sector001_macro'},now)
          tick(5);assert(not U.sender and not U.failed)
          tick(70);assert(U.sender and #U.sender.baseline.rows==2 and not U.failed)
        ''')

    def test_new_baseline_replaces_stale_motion_after_packet_loss(self):
        l=self.make('p2');l.execute('''
          tick(1);baseline(1,{testRow},true);tick(1)
          U.receive({'UPOS','1',client.lease,string.rep('a',32),'1','2','cluster_14_sector001_macro','1600','200','300','0','0','0'},now)
          tick(20);assert(positions[200].x==1600)
          baseline(2,{(testRow:gsub('1500','1700'))},true);tick(2)
          assert(positions[200].x==1700 and not U.failed)
        ''')

    def test_complete_baseline_replicates_and_only_suppresses_unprotected_local_npcs(self):
        l=self.make('p2');l.execute('''
          tick(1);baseline(1,{testRow},true);tick(20)
          assert(not U.failed and #spawns==2 and spawns[2].owner=='argon')
          assert(positions[200].x==1500 and #activated>0)
          assert(#removed==2 and positions[101]==nil and positions[102]==nil)
          for _,id in ipairs({11,42,90,103,104,200}) do assert(positions[id]) end
          baseline(2,{},true);tick(1);assert(positions[200]==nil)
        ''')

    def test_observe_mode_never_changes_engine_world(self):
        l=self.make('p2','observe');l.execute('''
          tick(1);baseline(1,{testRow},true);tick(20)
          assert(U.latest and #spawns==1 and #removed==0 and #activated==0)
        ''')

    def test_pause_defers_engine_application_and_duplicate_commit_does_not_respawn(self):
        l=self.make('p2');l.execute('''
          tick(1);paused=true;local b=baseline(1,{testRow},true);tick(4)
          assert(#spawns==1 and #removed==0)
          paused=false;tick(1);assert(#spawns==2)
          U.receive(P.split(P.packet(b,client.lease,2),'|'),now);tick(2);assert(#spawns==2)
        ''')

    def test_player_boarding_a_replica_stops_before_removal(self):
        l=self.make('p2');l.execute('''
          tick(1);baseline(1,{testRow},true);tick(1);container=200
          baseline(2,{},true);tick(1)
          assert(U.failed and positions[200] and not X4LAN_BUBBLE_API.state.failed)
        ''')

    def test_motion_is_epoch_checked_and_unknown_objects_do_not_spawn(self):
        l=self.make('p2');l.execute('''
          tick(1);baseline(1,{testRow},true);tick(1)
          local f={'UPOS','1',client.lease,string.rep('a',32),'1','2','cluster_14_sector001_macro','1600','200','300','0','0','0'}
          U.receive(f,now);tick(3);assert(positions[200].x==1600)
          f[6]='1';f[8]='9000';U.receive(f,now);tick(3);assert(positions[200].x==1600)
          f[6]='3';f[4]=string.rep('f',32);U.receive(f,now);tick(3);assert(positions[200].x==1600)
          f[4]=string.rep('a',32);f[5]='999';U.receive(f,now);tick(3);assert(#spawns==2)
        ''')
