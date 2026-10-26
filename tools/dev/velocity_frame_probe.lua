-- Opt-in setter-coordinate experiment; no production movement or wire changes.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.coop or cfg.test_profile~='45841' or cfg.slot~='p1' or not cfg.velocity_frame_probe then return end
local ffi=require('ffi');local C=ffi.C;local frame=A.universe.frame
local rotations={{0,0,0},{90,0,0},{180,0,0},{0,45,0},{0,0,45},{35,20,-15}}
local setup,ready,started,phase,finished;local nextSample=0
RegisterEvent('X4LAN_VelocityFrame.Ready',function() ready=true end)
function A.universe.frame(now)
 frame(now)
 if not A.coop.ready or not A.state.client or not A.state.client.lease or A.paused() then return end
 if not setup then setup=true;AddUITriggeredEvent('X4LAN_VelocityFrame','Setup') end
 if not ready or finished then return end
 local time=GetCurTime();started=started or time
 local index=math.floor((time-started)/8)+1
 if index>6 then
  finished=true;AddUITriggeredEvent('X4LAN_VelocityFrame','Set',{phase=7,x=0,y=0,z=0})
  DebugError('[X4LAN] VELOCITY_FRAME complete=1');return
 end
 local ship=A.coop.ships[2];local p=C.GetObjectPositionInSector(ship)
 local sector=C.GetContextByClass(ship,'sector',false);local angle=rotations[index]
 -- Keep the actual translated position; only orientation is controlled.
 C.SetObjectSectorPos(ship,sector,ffi.new('UIPosRot',{x=p.x,y=p.y,z=p.z,yaw=angle[1],pitch=angle[2],roll=angle[3]}))
 if phase~=index then
  phase=index;AddUITriggeredEvent('X4LAN_VelocityFrame','Set',{phase=index,x=100,y=40,z=-25})
 end
 local elapsed=(time-started)%8
 if elapsed>=2 and elapsed<=7.5 and now>=nextSample then
  nextSample=now+.2;AddUITriggeredEvent('X4LAN_VelocityFrame','Sample',index)
 end
end
