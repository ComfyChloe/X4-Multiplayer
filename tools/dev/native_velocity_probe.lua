-- Opt-in lifecycle/negative-command experiment in a disposable native world.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.coop or cfg.test_profile~='45841' or cfg.slot~='p1' or not cfg.native_velocity_probe then return end
local ffi=require('ffi');local C=ffi.C;local c=A.coop;local M=X4LAN_MOTION
local frame,receive,universe,paused,trigger=c.frame,c.receive,A.universe.frame,A.paused,AddUITriggeredEvent
local phases={'motion','freeze','resume','adapter_pause','watchdog','revoked','old_sequence','wrong_session','wrong_epoch','expired','wrong_parent','takeover'}
local setup,ready,started,phase,index,elapsed,finished,revoked,takeover,lastCommand;local nextSample,nextAttack=0,0
local function copy(t) local r={};for k,v in pairs(t) do r[k]=v end;return r end
AddUITriggeredEvent=function(screen,control,value)
 if screen=='X4LAN_Coop' and control=='Velocity' and value.ship==2 then lastCommand=copy(value) end
 return trigger(screen,control,value)
end
RegisterEvent('X4LAN_NativeVelocity.Ready',function() ready=true end)
A.paused=function() return phase=='adapter_pause' or paused() end
c.receive=function(f,now)
 if phase=='freeze' and f[1]=='NAV2' and f[5]=='2' then return true end
 return receive(f,now)
end
local function snapshot(tag,expectedManaged,expectedSequence)
 local ship=c.ships[2];local actual=C.GetObjectPositionInSector(ship)
 local p,v
 if index<=3 then p,v=M.render(c.remote[2].track,GetCurRealTime()) end
 p=p or {actual.x,actual.y,actual.z};v=v or {0,0,0}
 local state=c.velocity[2]
 trigger('X4LAN_NativeVelocity','Sample',{phase=phase,elapsed=elapsed,tag=tag,
  x=p[1],y=p[2],z=p[3],vx=v[1],vy=v[2],vz=v[3],expectedManaged=expectedManaged,
  generation=state and state.generation or c.velocityGeneration,sequence=expectedSequence or (state and state.sequence or 0)})
end
c.frame=function(now)
 if not ready then
  frame(now)
  if c.ready and not setup then setup=true;trigger('X4LAN_NativeVelocity','Setup') end
  return
 end
 if not started then
  if not c.remote[2] or not c.remote[2].track then frame(now);return end
  started=GetCurTime()
 end
 local age=GetCurTime()-started;index=math.floor(age/6)+1;elapsed=age%6
 if index>#phases then
  if not finished then
   finished=true;c.stopVelocity(false);trigger('X4LAN_NativeVelocity','Finish')
   DebugError('[X4LAN] NATIVE_VELOCITY complete=1')
  end
  return
 end
 if phase~=phases[index] then phase=phases[index];DebugError('[X4LAN] NATIVE_VELOCITY phase='..phase) end
 if index<=4 or elapsed<.8 or index==12 then frame(now) end
 if phase=='revoked' and elapsed>=.8 then
  if not revoked then
   revoked=copy(lastCommand);c.stopVelocity(false)
  end
  if now>=nextAttack then
   nextAttack=now+.2
   local bad=copy(revoked);bad.expires=GetCurTime()+.3;bad.x=777
   trigger('X4LAN_Coop','VelocityFence',{ship=2,generation=bad.generation,session=bad.session,epoch=bad.epoch,enabled=true})
   trigger('X4LAN_Coop','Velocity',bad);snapshot('old_generation',false)
  end
 elseif index>=7 and index<=11 and elapsed>=.8 and now>=nextAttack then
  nextAttack=now+.2
  local state=c.velocity[2];local good=copy(lastCommand)
  state.sequence=state.sequence+1;good.sequence=state.sequence;good.x=0;good.y=0;good.z=0;good.expires=GetCurTime()+.3
  trigger('X4LAN_Coop','Velocity',good)
  local bad=copy(good);bad.x=777;bad.sequence=good.sequence+1
  local managed=true
  if phase=='old_sequence' then bad.sequence=good.sequence-1
  elseif phase=='wrong_session' then bad.session='diagnostic-invalid-session'
  elseif phase=='wrong_epoch' then bad.epoch=bad.epoch+1
  elseif phase=='expired' then bad.expires=GetCurTime()-.1;managed=false
  elseif phase=='wrong_parent' then bad.parent=A.id(c.ships[1]);managed=false end
  state.sequence=math.max(state.sequence,bad.sequence)
  trigger('X4LAN_Coop','Velocity',bad);snapshot(phase,managed,managed and good.sequence or bad.sequence)
 elseif phase=='takeover' and elapsed>=.8 and not takeover then
  takeover=true;trigger('X4LAN_NativeVelocity','Takeover')
 end
end
function A.universe.frame(now)
 universe(now)
 if phase=='watchdog' and elapsed>=.7 and elapsed<2 and now>=nextSample then
  nextSample=now+.05;snapshot('watchdog_window',false);return
 end
 if not phase or finished or now<nextSample or elapsed<2 or elapsed>5.5 then return end
 nextSample=now+.2
 snapshot('settled',index<=3)
end
