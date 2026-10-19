-- Opt-in physical-velocity/trajectory diagnostic. The synthetic pilot sends
-- four phases on source time 500..548: stationary, rendered motion, then the
-- same rendered motion with an explicit native 100 m/s lateral velocity, and
-- native movement with pose application disabled by this diagnostic only.
-- This measures trajectories, not damage, actual player aiming or a fix.
local A=X4LAN_BUBBLE_API
local cfg=X4LAN_CONFIG
if not A or not A.coop or cfg.test_profile~='45841' or cfg.slot~='p1' or not cfg.projectile_probe then return end
local ffi=require('ffi');local C=ffi.C
local frame=A.universe.frame
local ready,setup,phase,requested
local configuredVx=0
local bullets={};local serial=0;local nextShip=0
local function position(object)
 local p=C.GetObjectPositionInSector(object)
 return {p.x,p.y,p.z}
end
RegisterEvent('X4LAN_ProjectileProbe.Ready',function() ready=true end)
RegisterEvent('X4LAN_ProjectileProbe.Velocity',function(_,v)
 configuredVx=tonumber(v)
 DebugError(string.format('[X4LAN] PROJECTILE_PROBE velocity_ack=%.3f phase=%s',configuredVx,tostring(phase)))
end)
RegisterEvent('X4LAN_ProjectileProbe.Projectile',function(_,value)
 serial=serial+1
 local id=ConvertIDTo64Bit(value)
 local gameTime=GetCurTime()
 local p=position(id)
 bullets[#bullets+1]={id=id,serial=serial,phase=phase or 'unknown',configuredVx=configuredVx,
  gameTime=gameTime,realTime=GetCurRealTime(),position=p}
 DebugError(string.format('[X4LAN] PROJECTILE_PROBE born=%d phase=%s configuredVx=%.3f id=%s x=%.5f y=%.5f z=%.5f gameTime=%.6f',
  serial,tostring(phase),configuredVx,tostring(id),p[1],p[2],p[3],gameTime))
end)
function A.universe.frame(now)
 frame(now)
 if not A.coop.ready or not A.state.client or not A.state.client.lease or A.paused() then return end
 if not setup then setup=true;AddUITriggeredEvent('X4LAN_ProjectileProbe','Setup') end
 local r=A.coop.remote[2]
 local samples=r and r.track and r.track.samples
 local sample=samples and samples[#samples]
 if not ready or not sample then return end
 local elapsed=sample.time-500
 phase=elapsed<12 and 'stationary' or elapsed<24 and 'rendered' or elapsed<36 and 'physical' or elapsed<48 and 'native' or 'finished'
 local desired=(phase=='physical' or phase=='native') and 100 or 0
 if phase=='native' then r.nextApply=math.huge
 elseif r.nextApply==math.huge then r.nextApply=0 end
 if desired~=requested then
  requested=desired;AddUITriggeredEvent('X4LAN_ProjectileProbe','Velocity',desired)
 end
 local gameTime=GetCurTime()
 for i=#bullets,1,-1 do
  local b=bullets[i];local dt=gameTime-b.gameTime
  if dt>=.20 or now-b.realTime>1 then
   local p=position(b.id)
   local vx=dt>0 and (p[1]-b.position[1])/dt or 0
   local vy=dt>0 and (p[2]-b.position[2])/dt or 0
   local vz=dt>0 and (p[3]-b.position[3])/dt or 0
   DebugError(string.format('[X4LAN] PROJECTILE_PROBE sample=%d phase=%s configuredVx=%.3f dt=%.6f dx=%.5f dy=%.5f dz=%.5f vx=%.5f vy=%.5f vz=%.5f',
    b.serial,b.phase,b.configuredVx,dt,p[1]-b.position[1],p[2]-b.position[2],p[3]-b.position[3],vx,vy,vz))
   table.remove(bullets,i)
  end
 end
 if now>=nextShip then
  nextShip=now+.2
  local p=position(A.coop.ships[2])
  local ok,details=pcall(function() return C.GetComponentDetails(A.coop.ships[2],'') end)
  local engineSpeed=ok and tonumber(details.speed) or -1
  DebugError(string.format('[X4LAN] PROJECTILE_PROBE ship phase=%s sourceTime=%.6f gameTime=%.6f x=%.5f y=%.5f z=%.5f wantedVx=%.3f configuredVx=%.3f engineSpeed=%.3f',
   phase,sample.time,gameTime,p[1],p[2],p[3],sample.velocity[1],configuredVx,engineSpeed))
 end
end
