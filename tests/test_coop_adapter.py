"""Engine doubles for sector movement and pilot arbitration; native tests remain required."""
import unittest
from pathlib import Path
from test_game_adapter import MOCK,LuaRuntime
from test_bubble_adapter import EXTRA
UI=Path(__file__).resolve().parents[1]/'src/extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class CoopAdapterTests(unittest.TestCase):
 def make(self,slot='p1'):
  l=LuaRuntime(unpack_returned_tuples=True);l.execute(MOCK+EXTRA)
  l.globals().X4LAN_CONFIG.slot=slot;l.globals().X4LAN_CONFIG.coop=True
  l.execute('''
   sectors={[11]=22,[42]=22};ConvertIDTo64Bit=function(v) return tonumber(v) end
   api.GetContextByClass=function(object) return sectors[object] or 22 end
   GetComponentData=function(object) return tonumber(object)==23 and 'cluster_01_sector001_macro' or 'cluster_14_sector001_macro' end
   api.SetObjectSectorPos=function(object,sector,p)
    writes[#writes+1]={object=object,sector=sector,x=p.x,y=p.y,z=p.z}
    positions[object]={x=p.x,y=p.y,z=p.z,yaw=p.yaw*math.pi/180,pitch=p.pitch*math.pi/180,roll=p.roll*math.pi/180}
   end
  ''')
  for file in ('bubble_game.lua','motion.lua','coop_game.lua','life_game.lua'):l.execute((UI/file).read_text())
  l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
   function auth(rev,o1,e1,p1,o2,e2,p2)
    client:receiveExtension({'AUTH','1',client.lease,tostring(rev),o1,tostring(e1),p1,o2,tostring(e2),p2},now)
   end
   function nav(entity,epoch,seq,sector,x,dock,fire)
    client:receiveExtension({'NAV','1',client.lease,string.rep('b',32),tostring(entity),tostring(epoch),tostring(seq),sector,tostring(x),'10000','0','0','0','0',tostring(dock or 0),tostring(fire or 0)},now)
   end
   auth(1,'p1',1,'p1','p2',1,'p2')
  ''')
  return l
 def test_absolute_far_pose_and_sector_are_sent_without_old_bubble_limit(self):
  l=self.make();l.execute('''
   positions[11]={x=900000,y=20000,z=0,yaw=0,pitch=0,roll=0};sectors[11]=23
   now=11;frames.onUpdate();assert(lastPose==nil)
   local found=false
   for _,p in ipairs(sent) do if p:find('|cluster_01_sector001_macro|900000|20000|0|',1,true) then found=true end end
   assert(found and not X4LAN_BUBBLE_API.state.failed)
  ''')
 def test_remote_sector_resolves_and_warps_without_recreating_ship(self):
  l=self.make();l.execute('''
   nav(2,1,1,'cluster_01_sector001_macro',900000);frames.onUpdate();assert(countRequest('ResolveSector')==1)
   events['X4LAN_Coop.Sector.cluster_01_sector001_macro'](nil,23)
   now=10.2;frames.onUpdate();assert(countRequest('SectorMove')==1)
   sectors[42]=23;now=10.4;frames.onUpdate()
   assert(writes[#writes].object==42 and writes[#writes].sector==23 and writes[#writes].x==900000)
   n=#writes;now=10.5;frames.onUpdate();assert(#writes==n) -- stationary pose does not reset engine physics every frame
   now=12;frames.onUpdate();assert(#writes==n)
  ''')
 def test_transfer_moves_former_local_ship_and_never_uses_old_epoch(self):
  l=self.make();l.execute('''
   controlled=0;container=11;auth(2,'p2',2,'p2','p2',1,'0')
   nav(1,1,1,'cluster_14_sector001_macro',55);assert(X4LAN_BUBBLE_API.coop.remote[1]==nil)
   X4LAN_BUBBLE_API.coop.sectors.cluster_14_sector001_macro=22
   nav(1,2,2,'cluster_14_sector001_macro',66);now=10.3;frames.onUpdate()
   assert(writes[#writes].object==11 and writes[#writes].x==66)
  ''')
 def test_docked_replica_waits_for_undock_before_position_apply(self):
  l=self.make();l.execute('''
   X4LAN_BUBBLE_API.coop.sectors.cluster_14_sector001_macro=22
   events['X4LAN_Coop.Docks'](nil,'0|1');nav(2,1,1,'cluster_14_sector001_macro',50)
   now=10.2;frames.onUpdate();assert(countRequest('Dock')==1 and #writes==0)
   events['X4LAN_Coop.Docks'](nil,'0|0');now=10.4;frames.onUpdate();assert(#writes==1)
  ''')
 def test_new_trade_execution_keeps_actual_ship_market_and_basket(self):
  l=self.make();l.execute('''
   X4LAN_BUBBLE_API.trade={receive=function(_,f) delivered=f;return true end}
   client:receiveExtension({'COOP_EXEC','1',client.lease,string.rep('c',32),'1','m2','energycells,10,1200'},now)
   assert(delivered[1]=='ORDER_EXEC' and delivered[5]=='p1' and delivered[6]=='m2' and delivered[7]=='energycells,10,1200')
  ''')
 def test_ordinary_native_dock_blocks_pose_even_with_zero_market_index(self):
  l=self.make();l.execute('''
   local original=GetComponentData
   local docked=false
   GetComponentData=function(object,key)
    if key=='isdocked' then return tonumber(object)==42 and docked end
    return original(object,key)
   end
   local c=X4LAN_BUBBLE_API.coop;c.sectors.cluster_14_sector001_macro=22
   nav(2,1,1,'cluster_14_sector001_macro',50);now=10.2;frames.onUpdate()
   assert(#writes==1 and c.docks[2]==0)
   docked=true;positions[42].x=700;nav(2,1,2,'cluster_14_sector001_macro',100)
   now=10.3;frames.onUpdate();assert(#writes==1 and countRequest('Dock')==0)
   docked=false;now=10.4;frames.onUpdate();assert(#writes==2)
  ''')
 def test_local_pilot_blocks_remote_pose_while_authority_update_is_pending(self):
  l=self.make();l.execute('''
   local c=X4LAN_BUBBLE_API.coop;c.sectors.cluster_14_sector001_macro=22
   controlled=42;nav(2,1,1,'cluster_14_sector001_macro',50)
   now=10.2;frames.onUpdate();assert(#writes==0 and countRequest('SectorMove')==0)
   controlled=11;now=10.3;frames.onUpdate();assert(#writes==1)
  ''')
 def test_queued_sector_move_has_native_attachment_and_pilot_guards(self):
  # Static boundary check: Lua doubles do not execute native MD queues.
  import xml.etree.ElementTree as ET
  root=ET.parse(UI.parent/'md/x4lan_coop.xml')
  guard=root.find(".//cue[@name='SectorMove']/actions/do_if").get('value')
  for condition in ('not $ship.dock','not $ship.isinternallystored','player.occupiedship != $ship'):
   self.assertIn(condition,guard)
 def test_suit_remote_sector_and_absolute_coordinates_survive_original_ship_death(self):
  l=self.make();l.execute('''
   nextObject=90
   api.SpawnObjectAtPos2=function(macro,sector,p,owner) local n=nextObject;nextObject=n+1;positions[n]=p;sectors[n]=sector;return n end
   X4LAN_BUBBLE_API.coop.sectors.cluster_01_sector001_macro=23
   client:receiveExtension({'BODY2','1',client.lease,string.rep('b',32),'1','suit','1','1','cluster_01_sector001_macro','800000','12345','0','0','0','0'},now)
   now=10.3;frames.onUpdate()
   assert(positions[90].x==800000 and positions[90].y==12345 and sectors[90]==23)
   positions[11]=nil;positions[77]={x=900000,y=50,z=20,yaw=0,pitch=0,roll=0};sectors[77]=23;controlled=77
   events['X4LAN_Life.State'](nil,'suit|1|1');now=10.5;frames.onUpdate()
   local found=false;for _,p in ipairs(sent) do if p:find('|suit|1|1|cluster_01_sector001_macro|900000|50|20|',1,true) then found=true end end;assert(found)
  ''')
 def test_shots_stop_on_timeout_or_authority_transfer_without_replaying_baseline(self):
  l=self.make();l.execute('''
   X4LAN_BUBBLE_API.coop.sectors.cluster_14_sector001_macro=22
   nav(2,1,1,'cluster_14_sector001_macro',50,0,5);now=10.1;frames.onUpdate();assert(countRequest('Fire')==0)
   nav(2,1,2,'cluster_14_sector001_macro',50,0,6);now=10.2;frames.onUpdate();assert(countRequest('Fire')==1)
   now=10.7;frames.onUpdate();assert(countRequest('Fire')==2)
   nav(2,1,3,'cluster_14_sector001_macro',50,0,7);now=10.8;frames.onUpdate();assert(countRequest('Fire')==3)
   auth(2,'p1',1,'p1','p1',2,'0');assert(countRequest('Fire')==4)
   nav(2,1,4,'cluster_14_sector001_macro',50,0,99);assert(X4LAN_BUBBLE_API.coop.remote[2]==nil)
  ''')
 def test_delayed_pilot_denial_does_not_eject_after_new_grant(self):
  l=self.make();l.execute('''
   controlled=42;container=42;now=11;frames.onUpdate()
   local c=X4LAN_BUBBLE_API.coop
   auth(2,'p1',1,'0','p1',2,'p1')
   client:receiveExtension({'PILOT_DENIED','1',client.lease,tostring(c.crewSeq),'2'},now)
   assert(countRequest('LeaveControls')==0)
   auth(3,'p1',1,'0','p2',3,'p2');now=12;frames.onUpdate()
   client:receiveExtension({'PILOT_DENIED','1',client.lease,tostring(c.crewSeq),'2'},now)
   assert(countRequest('LeaveControls')==1)
  ''')
 def test_remote_dock_goes_through_engine_without_writing_inside_the_other_pad(self):
  l=self.make();l.execute('''
   X4LAN_BUBBLE_API.coop.sectors.cluster_14_sector001_macro=22
   nav(2,1,1,'cluster_14_sector001_macro',8000,2);now=10.2;frames.onUpdate()
   assert(countRequest('Dock')==1 and #writes==0)
   events['X4LAN_Coop.Docks'](nil,'0|2');local n=#writes
   nav(2,1,2,'cluster_14_sector001_macro',8050,2);now=10.3;frames.onUpdate();assert(#writes==n)
  ''')
 def test_transport_failure_stops_remote_weapons_before_reconnecting(self):
  l=self.make();l.execute('''
   X4LAN_BUBBLE_API.coop.sectors.cluster_14_sector001_macro=22
   nav(2,1,1,'cluster_14_sector001_macro',50,0,0);nav(2,1,2,'cluster_14_sector001_macro',50,0,1)
   now=10.1;frames.onUpdate();assert(countRequest('Fire')==1)
   client.update=function() error('transport unavailable') end
   now=10.2;frames.onUpdate();assert(countRequest('Fire')==2)
   assert(X4LAN_BUBBLE_API.coop.remote[2]==nil)
  ''')
 def test_health_reconciles_periodically_even_when_host_snapshot_is_unchanged(self):
  l=self.make('p2');l.execute('''
   local function world(rev,time)
    local f={'WORLD','1',client.lease,string.rep('a',32),tostring(rev)}
    for i=1,17 do f[#f+1]='0' end
    client:receiveExtension(f,time)
   end
   world(1,10);assert(countRequest('Apply')==1)
   world(2,10.5);assert(countRequest('Apply')==1)
   world(3,11.1);assert(countRequest('Apply')==2)
  ''')

 def test_modern_navigation_predicts_during_loss_and_freezes_without_snapback(self):
  l=self.make();l.execute("""
   local c=X4LAN_BUBBLE_API.coop;c.sectors.cluster_14_sector001_macro=22
   local f={'NAV2','1',client.lease,string.rep('b',32),'2','1','1','cluster_14_sector001_macro',
    '1000','10000','0','0','0','0','0','0','50','100','0','0','0','0','0'}
   c.receive(f,now);now=10.4;frames.onUpdate();assert(math.abs(positions[42].x-1025)<.01)
   now=11;frames.onUpdate();assert(math.abs(positions[42].x-1035)<.01)
   local n=#writes;now=12;frames.onUpdate();assert(#writes==n and positions[42].x>1000)
   f[7]='2';f[17]='49';c.receive(f,now);assert(c.remote[2].seq==1)
   auth(2,'p1',1,'p1','p1',2,'0');assert(c.remote[2]==nil)
  """)

 def stationary_remote(self):
  l=self.make();l.execute("""
   local c=X4LAN_BUBBLE_API.coop;c.sectors.cluster_14_sector001_macro=22
   c.receive({'NAV2','1',client.lease,string.rep('b',32),'2','1','1','cluster_14_sector001_macro',
    '1000','10000','0','0','0','0','0','0','50','0','0','0','0','0','0'},now)
   now=10.2;frames.onUpdate()
  """);return l

 def test_unchanged_remote_pose_repairs_actual_engine_translation_and_rotation(self):
  l=self.stationary_remote();l.execute("""
   local n=#writes
   positions[42].x=1020;positions[42].yaw=.5;positions[42].pitch=-.2
   now=10.4;frames.onUpdate()
   assert(#writes==n+1 and positions[42].x==1000 and positions[42].yaw==0 and positions[42].pitch==0)
   now=10.6;frames.onUpdate();assert(#writes==n+1) -- no correction if the actual pose agrees
  """)

 def test_drift_repair_respects_pause_docking_and_local_authority(self):
  l=self.stationary_remote();l.execute("""
   local n=#writes;positions[42].yaw=.5
   paused=true;now=10.4;frames.onUpdate();assert(#writes==n and positions[42].yaw==.5)
   paused=false;now=10.6;frames.onUpdate();assert(#writes==n+1 and positions[42].yaw==0)
   events['X4LAN_Coop.Docks'](nil,'0|1');positions[42].yaw=.6
   now=10.8;frames.onUpdate();assert(#writes==n+1 and positions[42].yaw==.6)
   events['X4LAN_Coop.Docks'](nil,'0|0');auth(2,'p1',1,'p1','p1',2,'0')
   now=11;frames.onUpdate();assert(#writes==n+1 and positions[42].yaw==.6)
  """)

 def test_equivalent_wrapped_engine_angle_does_not_cause_pose_correction(self):
  l=self.stationary_remote();l.execute("""
   local c=X4LAN_BUBBLE_API.coop
   c.receive({'NAV2','1',client.lease,string.rep('b',32),'2','1','2','cluster_14_sector001_macro',
    '1000','10000','0',tostring(math.pi),'0','0','0','0','50.2','0','0','0','0','0','0'},now)
   now=10.6;frames.onUpdate();local n=#writes
   positions[42].yaw=-math.pi;now=10.8;frames.onUpdate();assert(#writes==n)
   positions[42].yaw=-math.pi+.2;now=11;frames.onUpdate();assert(#writes==n+1)
  """)

 def test_remote_drift_is_corrected_on_the_next_render_frame_above_60_fps(self):
  l=self.stationary_remote();l.execute("""
   local n=#writes;positions[42].yaw=.005
   now=10.208;frames.onUpdate()
   assert(#writes==n+1 and positions[42].yaw==0)
  """)

 def moving_remote(self):
  l=self.make();l.execute("""
   local c=X4LAN_BUBBLE_API.coop;c.sectors.cluster_14_sector001_macro=22
   function lastRequest(name)
    for i=#requests,1,-1 do if requests[i][2]==name then return requests[i][3] end end
   end
   function movingNav(seq,time,x)
    c.receive({'NAV2','1',client.lease,string.rep('b',32),'2','1',tostring(seq),'cluster_14_sector001_macro',
     tostring(x),'10000','0','0','0','0','0','0',tostring(time),'100','0','0','0','0','0'},now)
   end
   movingNav(1,50,1000);now=10.4;frames.onUpdate()
   assert(lastRequest('Velocity').x==100 and lastRequest('VelocityFence').enabled)
  """);return l

 def test_freeze_zero_bypasses_native_velocity_renewal_timer(self):
  l=self.moving_remote();l.execute("""
   now=10.4999;frames.onUpdate();local n=countRequest('Velocity')
   now=10.5001;frames.onUpdate()
   assert(countRequest('Velocity')==n+1 and lastRequest('Velocity').x==0)
   assert(lastRequest('Velocity').expires==now+.3)
  """)

 def test_pause_revokes_once_before_return_and_resume_uses_new_generation(self):
  l=self.moving_remote();l.execute("""
   local old=lastRequest('Velocity');paused=true;now=10.41;frames.onUpdate()
   local revoke=lastRequest('VelocityFence');assert(not revoke.enabled and revoke.generation>old.generation)
   local n=countRequest('VelocityFence');now=10.42;frames.onUpdate();assert(countRequest('VelocityFence')==n)
   paused=false;now=11;frames.onUpdate()
   assert(lastRequest('Velocity').generation>revoke.generation and lastRequest('Velocity').x==0)
  """)

 def test_authority_and_actual_local_control_revoke_without_zero_lock(self):
  for boundary in ("auth(2,'p1',1,'p1','p1',2,'p1')",'controlled=42;frames.onUpdate()'):
   l=self.moving_remote();l.execute(boundary+"""
    assert(not lastRequest('VelocityFence').enabled and X4LAN_BUBBLE_API.coop.velocity[2]==nil)
    local n=countRequest('Velocity');now=10.45;frames.onUpdate();assert(countRequest('Velocity')==n)
   """)

 def test_dock_death_missing_pose_and_missing_lease_release(self):
  for boundary in ("events['X4LAN_Coop.Docks'](nil,'0|1')",'positions[42]=nil;frames.onUpdate()',
                   'X4LAN_BUBBLE_API.coop.remote[2]=nil;frames.onUpdate()',
                   'X4LAN_BUBBLE_API.state.client.lease=nil;X4LAN_BUBBLE_API.coop.frame(now)'):
   l=self.moving_remote();l.execute(boundary+"""
    assert(not lastRequest('VelocityFence').enabled and X4LAN_BUBBLE_API.coop.velocity[2]==nil)
   """)

 def test_connection_error_discards_old_track_and_fences_reconnect(self):
  l=self.moving_remote();l.execute("""
   local old=lastRequest('Velocity');client.update=function() error('transport unavailable') end
   now=10.41;frames.onUpdate();local revoke=lastRequest('VelocityFence')
   assert(not revoke.enabled and revoke.generation>old.generation and X4LAN_BUBBLE_API.coop.remote[2]==nil)
   now=12;frames.onUpdate();local n=countRequest('Velocity');frames.onUpdate();assert(countRequest('Velocity')==n)
   movingNav(2,52,1200);now=12.2;frames.onUpdate()
   assert(lastRequest('Velocity').generation>revoke.generation)
  """)

 def test_parent_and_sector_changes_revoke_previous_generation(self):
  l=self.moving_remote();l.execute("""
   local old=lastRequest('Velocity');local getContext=api.GetContextByClass
   api.GetContextByClass=function(object,class) if class=='zone' then return 99 end;return getContext(object,class) end
   now=10.41;frames.onUpdate();local fresh=lastRequest('Velocity')
   assert(tostring(fresh.parent)=='99' and fresh.generation>old.generation)
   nav(2,1,2,'cluster_01_sector001_macro',9000)
   assert(not lastRequest('VelocityFence').enabled)
  """)
