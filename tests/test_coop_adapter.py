"""Engine doubles for sector movement and pilot arbitration; native tests remain required."""
import unittest
from pathlib import Path
from test_game_adapter import MOCK,LuaRuntime
from test_bubble_adapter import EXTRA
UI=Path(__file__).resolve().parents[1]/'src/extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class CoopAdapterTests(unittest.TestCase):
 def make(self,slot='p1',dedicated=False,setup=''):
  l=LuaRuntime(unpack_returned_tuples=True);l.execute(MOCK+EXTRA)
  l.globals().X4LAN_CONFIG.slot=slot;l.globals().X4LAN_CONFIG.coop=True
  l.globals().X4LAN_CONFIG.dedicated_session=dedicated
  if slot=='p3':l.globals().X4LAN_CONFIG.local_ship=1
  if setup:l.execute(setup)
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
 def test_dedicated_p3_uses_ship_one_and_accepts_its_authority(self):
  l=self.make('p3',True);l.execute('''
   local c=X4LAN_BUBBLE_API.coop
   assert(c.ships[1]==X4LAN_BUBBLE_API.state.ship and c.ships[2]==X4LAN_BUBBLE_API.state.proxy)
   auth(2,'p3',2,'p3','p2',1,'p2')
   assert(c.owners[1]=='p3' and c.pilots[1]=='p3' and c.revision==2)
  ''')
  l=self.make('p2',False);l.execute('''
   local c=X4LAN_BUBBLE_API.coop;auth(2,'p3',2,'p3','p2',1,'p2')
   assert(c.owners[1]=='p1' and c.revision==1)
  ''')
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
   -- A zone change inside the sector rebinds the running vector at once, without a fence.
   local fences=countRequest('VelocityFence')
   now=10.41;frames.onUpdate();local fresh=lastRequest('Velocity')
   assert(tostring(fresh.parent)=='99' and fresh.generation==old.generation and fresh.sequence>old.sequence)
   assert(countRequest('VelocityFence')==fences)
   nav(2,1,2,'cluster_01_sector001_macro',9000)
   assert(not lastRequest('VelocityFence').enabled)
  """)

 def test_sender_declares_motion_mode_with_each_nav2_sample(self):
  l=self.make();l.execute("""
   local hw=0;local base=api.GetContextByClass
   api.GetContextByClass=function(object,class) if class=='highway' then return hw end;return base(object,class) end
   local function last(kind) for i=#sent,1,-1 do if sent[i]:sub(1,#kind+1)==kind..'|' then return sent[i] end end end
   local function step(expected)
    now=now+.1;frames.onUpdate()
    local nav,mode=last('NAV2'),last('NMODE')
    local navSeq=nav:match('^NAV2|1|%x+|1|1|(%d+)|');local modeSeq,time,value=mode:match('^NMODE|1|%x+|1|1|(%d+)|([%d.]+)|(%a+)$')
    assert(navSeq and navSeq==modeSeq and value==expected, mode)
    assert(nav:find('|'..time..'|',1,true)) -- the same source time as its NAV2
   end
   now=11;step('normal')
   hw=77;step('highway')
   hw=0;GetPlayerSpeed=function() return 1,2,3,false,true end;step('travel')
   GetPlayerSpeed=function() return 1,2,3,true,false end;step('boost')
   controlled=0;container=11;step('normal')   -- boost/travel state belongs to the piloted ship only
   hw=22;step('normal')                       -- a context equal to the sector is not a highway
  """)

 def high_speed_remote(self,setup=''):
  l=self.make(setup=setup);l.execute("""
   local c=X4LAN_BUBBLE_API.coop;c.sectors.cluster_14_sector001_macro=22
   function lastRequest(name)
    for i=#requests,1,-1 do if requests[i][2]==name then return requests[i][3] end end
   end
   function fenceCount(enabled) local n=0;for _,r in ipairs(requests) do if r[2]=='VelocityFence' and r[3].enabled==enabled then n=n+1 end end;return n end
   function sample(k,v,x,mode)
    now=10+k*.05;local time=50+k*.05
    if mode then c.receive({'NMODE','1',client.lease,string.rep('b',32),'2','1',tostring(k+1),tostring(time),mode},now) end
    c.receive({'NAV2','1',client.lease,string.rep('b',32),'2','1',tostring(k+1),'cluster_14_sector001_macro',
     tostring(x),'10000','0','0','0','0','0','0',tostring(time),tostring(v),'0','0','0','0','0'},now)
    frames.onUpdate()
   end
   zone=5;local base=api.GetContextByClass
   api.GetContextByClass=function(object,class) if class=='zone' then return zone end;if class=='highway' then return 0 end;return base(object,class) end
  """);return l

 def test_highway_mode_reports_native_speed_without_fence_churn(self):
  # Native 7.6: MOTION_DIAG mode=highway velocity=off drift=0.00 at 3-15 km/s, and the HUD
  # (GetComponentDetails().speed) showed 0 m/s for a remote ship in a highway.
  l=self.high_speed_remote();l.execute("""
   local x=1000
   for k=0,4 do sample(k,100,x,'normal');x=x+5 end
   assert(math.abs(lastRequest('Velocity').x-100)<1e-6 and fenceCount(true)==1, lastRequest('Velocity').x)
   local disabled,previous=fenceCount(false),positions[42].x
   for k=5,60 do
    x=x+500;zone=zone+1   -- 10 km/s; the free replica changes temp zone every frame
    local n,velocities=#writes,countRequest('Velocity');sample(k,10000,x,'highway')
    assert(#writes==n+1 and positions[42].x>=previous, k);previous=positions[42].x
    local v=lastRequest('Velocity')
    assert(countRequest('Velocity')==velocities+1 and tostring(v.parent)==tostring(zone), k) -- rebound each zone
    if k>=20 then assert(math.abs(v.x-10000)<1, k..' '..v.x) end
   end
   assert(fenceCount(false)==disabled and fenceCount(true)==1)
   local c=X4LAN_BUBBLE_API.coop
   assert(c.velocity[2] and c.remote[2].mode=='highway')
   -- Leaving the highway keeps the same vector generation and slows the vector down.
   for k=61,100 do x=x+5;sample(k,100,x,'normal') end
   assert(math.abs(lastRequest('Velocity').x-100)<1 and fenceCount(true)==1 and fenceCount(false)==disabled)
  """)

 def test_engine_highway_attached_replica_gets_no_pose_warp_or_vector(self):
  # Dedicated server hang 18:19 during a highway chain: guard the case that the engine
  # itself attaches a replica to a highway. The highway then moves it; the mod stays out.
  l=self.high_speed_remote();l.execute("""
   local hw,sectorOf=0,{};local base=api.GetContextByClass
   api.GetContextByClass=function(object,class)
    if class=='highway' then return object==42 and hw or 0 end
    if class=='sector' and sectorOf[object] then return sectorOf[object] end
    return base(object,class)
   end
   local x=1000
   for k=0,9 do x=x+500;zone=zone+1;sample(k,10000,x,'highway') end
   assert(X4LAN_BUBBLE_API.coop.velocity[2] and fenceCount(false)==0)
   hw=77;local n,moves,velocities=#writes,countRequest('SectorMove'),countRequest('Velocity')
   for k=10,40 do
    x=x+500;zone=zone+1;if k==20 then sectorOf[42]=23 end  -- even a sector mismatch
    sample(k,10000,x,'highway')
   end
   assert(#writes==n and countRequest('SectorMove')==moves and countRequest('Velocity')==velocities)
   assert(fenceCount(false)==1 and X4LAN_BUBBLE_API.coop.velocity[2]==nil and X4LAN_BUBBLE_API.coop.remote[2].applied==nil)
   local attached,released=0,0
   for _,v in ipairs(logs) do
    if v:find('REPLICA_HIGHWAY ship=2 state=attached',1,true) then attached=attached+1 end
    if v:find('REPLICA_HIGHWAY ship=2 state=released',1,true) then released=released+1 end
   end
   assert(attached==1 and released==0)
   -- Released by the engine: pose and vector resume, without a slip from the old pose.
   hw=0;sectorOf[42]=nil
   for k=41,45 do x=x+500;zone=zone+1;sample(k,10000,x,'highway') end
   assert(#writes>n and countRequest('Velocity')>velocities and X4LAN_BUBBLE_API.coop.velocity[2])
   for _,v in ipairs(logs) do
    if v:find('REPLICA_HIGHWAY ship=2 state=released',1,true) then released=released+1 end
    assert(not v:find('COOP_FRAME_SLIP',1,true))
   end
   assert(released==1)
  """)

 def test_boost_travel_and_legacy_fast_streams_keep_native_velocity(self):
  for mode,speed in (('boost',800),('travel',4000),(None,5000)):
   l=self.high_speed_remote();l.execute("""
    local x=1000;local mode=%s
    for k=0,30 do x=x+%d*.05;zone=zone+(k%%3==0 and 1 or 0);sample(k,%d,x,mode) end
    local r=X4LAN_BUBBLE_API.coop.remote[2]
    assert(r.mode==(mode or 'fast') and math.abs(lastRequest('Velocity').x-%d)<1, r.mode..' '..lastRequest('Velocity').x)
    assert(fenceCount(false)==0 and fenceCount(true)==1 and positions[42].x>1000)
   """%(repr(mode) if mode else 'nil',speed,speed,speed))

 def test_motion_diagnostics_are_inert_by_default_and_rate_limited_when_enabled(self):
  def rows(l): return [v for v in l.eval('logs').values() if 'MOTION_DIAG' in v]
  l=self.high_speed_remote()
  l.execute("local x=1000;for k=0,40 do x=x+500;sample(k,10000,x,'highway') end")
  self.assertEqual(rows(l),[])
  l=self.high_speed_remote('X4LAN_CONFIG.motion_diagnostics=.5')
  l.execute("local x=1000;for k=0,40 do x=x+500;sample(k,10000,x,'highway') end")
  found=rows(l);self.assertTrue(2<=len(found)<=5,found)
  self.assertIn('mode=highway declared=highway',found[-1]);self.assertIn('fences=1',found[-1]);self.assertIn('velocity=native',found[-1])

 # Engine doubles of the SetObjectSectorPos zone-frame slip seen natively (constant drift of
 # 51-84 km at 650 m/s). Zone A (origin 20 km) holds 20..60 km, zone B (origin 0) the rest.
 # A write converts the sector pose into the ship's current zone and re-parents the ship to
 # the zone at the target. model 'order' (matches the 7.6 evidence): the move is immediate,
 # but the zone-local pose is also queued and the first queued pose of a frame is applied
 # in the new zone at the end of the frame, so a same-frame rewrite cannot repair it.
 # model 'local': the re-parent keeps the zone-local pose immediately. After each frame the
 # engine files the ship into the zone at its actual position (world pose kept).
 ZONES="""
  zoneOrigin={A=20000,B=0};function zoneAt(x) return (x>=20000 and x<60000) and 'A' or 'B' end
  parent={[42]='B'};slipEngine=true;model='order';pending=nil;writesThisFrame=0
  api.SetObjectSectorPos=function(object,sector,p)
   writes[#writes+1]={object=object,sector=sector,x=p.x,y=p.y,z=p.z}
   local x=p.x
   if slipEngine and object==42 then
    writesThisFrame=writesThisFrame+1
    local localX=p.x-zoneOrigin[parent[42]];local target=zoneAt(p.x);parent[42]=target
    if model=='local' then x=zoneOrigin[target]+localX elseif not pending then pending=localX end
   end
   positions[object]={x=x,y=p.y,z=p.z,yaw=p.yaw*math.pi/180,pitch=p.pitch*math.pi/180,roll=p.roll*math.pi/180}
  end
  function endFrame()
   if pending then positions[42].x=zoneOrigin[parent[42]]+pending;pending=nil end
   parent[42]=zoneAt(positions[42].x)
  end
  local base=api.GetContextByClass
  api.GetContextByClass=function(object,class)
   if class=='zone' then return object==42 and (parent[42]=='A' and 5 or 6) or 7 end
   if class=='highway' then return 0 end;return base(object,class)
  end
 """
 def slipping_remote(self,model='order'):
  l=self.high_speed_remote();l.execute(self.ZONES+"""
   model='%s'
   -- Already in the loop: in zone B, displaced by the zone offset.
   positions[42]={x=61000,y=10000,z=0,yaw=0,pitch=0,roll=0};parent[42]='B'
   slipped,maxWrites=0,0
   function run(from,to)
    for k=from,to do
     local r=X4LAN_BUBBLE_API.coop.remote[2]
     if r and r.applied and math.abs(positions[42].x-r.applied[1])>1000 then slipped=slipped+1 end
     writesThisFrame=0;sample(k,650,41000+k*32.5,'normal');endFrame()
     if k>2 then maxWrites=math.max(maxWrites,writesThisFrame) end
    end
   end
   function slipLogs() local n=0;for _,v in ipairs(logs) do if v:find('COOP_FRAME_SLIP',1,true) then n=n+1 end end;return n end
  """%model);return l

 def test_zone_offset_is_learned_once_and_later_first_writes_land(self):
  for model in ('order','local'):
   l=self.slipping_remote(model);l.execute("""
    run(0,60)
    local r=X4LAN_BUBBLE_API.coop.remote[2]
    assert(slipped<=1, model..' slipped frames '..slipped)       -- not every frame
    assert(maxWrites==1, model..' writes per frame '..maxWrites)  -- no same-frame repair
    assert(math.abs(positions[42].x-r.applied[1])<1 and parent[42]=='A', model)
    assert(fenceCount(true)<=3 and countRequest('SectorMove')==0, model)
    local found=false
    for _,v in ipairs(logs) do if v:find('COOP_FRAME_SLIP ship=2 slip=20000 action=zone',1,true) then found=true end end
    assert(found, model)
   """)

 def test_without_compensation_the_order_double_slips_every_frame(self):
  # Guards the double: a plain write plus a same-frame rewrite (557fad4) still slips on
  # every frame, as in the native 7.6 COOP_FRAME_SLIP count=128 per 5 s rows.
  l=self.slipping_remote();l.execute("""
   for k=0,20 do
    writesThisFrame=0;local x=41000+k*32.5
    api.SetObjectSectorPos(42,22,{x=x,y=10000,z=0,yaw=0,pitch=0,roll=0})
    api.SetObjectSectorPos(42,22,{x=x,y=10000,z=0,yaw=0,pitch=0,roll=0})
    endFrame();assert(math.abs(positions[42].x-x-20000)<1 and parent[42]=='B', k)
   end
  """)

 def test_a_missed_target_zone_drops_the_offset_and_writes_plainly(self):
  l=self.slipping_remote();l.execute("""
   run(0,3)
   local r=X4LAN_BUBBLE_API.coop.remote[2];assert(r.zoneFix and r.zoneFix['6'])
   -- Zone A disappears: the target is now in zone B, so the B->A offset must not apply.
   zoneAt=function() return 'B' end;parent[42]='B'
   run(4,10)
   assert(r.zoneFix['6']==nil and math.abs(positions[42].x-r.applied[1])<1)
  """)

 def test_persistent_slip_falls_back_to_one_warp_per_second(self):
  l=self.slipping_remote('local');l.execute("""
   -- An engine that drops every write into zone B: no compensation can land.
   local set=api.SetObjectSectorPos
   api.SetObjectSectorPos=function(object,sector,p) set(object,sector,p);parent[42]='B' end
   endFrame=function() end
   run(0,45)
   local n=countRequest('SectorMove');assert(n>=1 and n<=2, n)
   local move=lastRequest('SectorMove');assert(move.ship==2 and tostring(move.sector)=='22')
   assert(move.x>=41000 and move.x<=41000+45*32.5) -- the received pose, not a compensated one
  """)

 def test_ordinary_native_velocity_drift_is_not_a_slip(self):
  l=self.slipping_remote();l.execute("""
   slipEngine=false;positions[42].x=41000;local n
   for k=0,40 do
    sample(k,650,41000+k*32.5,'normal')
    if k==5 then n=#writes end
    positions[42].x=positions[42].x+32.5   -- native velocity between writes
   end
   assert(#writes-n==35 and countRequest('SectorMove')==0 and slipLogs()==0)
  """)
