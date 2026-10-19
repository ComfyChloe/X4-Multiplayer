-- Opt-in physical-velocity/trajectory diagnostic. The synthetic pilot sends
-- four phases on source time 500..548: stationary, rendered motion, then the
-- same rendered motion with an explicit native 100 m/s lateral velocity, and
-- native translation with network position application disabled. The diagnostic
-- preserves native position and holds orientation at zero in every control;
-- setting angular velocity once did not prevent the engine controller turning it.
-- This measures trajectories, not damage, actual player aiming or a fix.
local A=X4LAN_BUBBLE_API
local cfg=X4LAN_CONFIG
if not A or not A.coop or cfg.test_profile~='45841' or cfg.slot~='p1' or not cfg.projectile_probe then return end
local ffi=require('ffi');local C=ffi.C
local frame=A.universe.frame
local ready,setup,phase,previousPhase,requested
local configuredVx=0
local nextShip=0
local function position(object)
 local p=C.GetObjectPositionInSector(object)
 return {p.x,p.y,p.z,p.yaw,p.pitch,p.roll}
end
RegisterEvent('X4LAN_ProjectileProbe.Ready',function() ready=true end)
RegisterEvent('X4LAN_ProjectileProbe.Velocity',function(_,v)
 configuredVx=tonumber(v)
 DebugError(string.format('[X4LAN] PROJECTILE_PROBE velocity_ack=%.3f phase=%s',configuredVx,tostring(phase)))
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
 local changed=phase~=previousPhase
 if changed then
  previousPhase=phase;AddUITriggeredEvent('X4LAN_ProjectileProbe','Phase',phase)
 end
 local desired=(phase=='physical' or phase=='native') and 100 or 0
 if phase=='native' then r.nextApply=math.huge
 elseif r.nextApply==math.huge then r.nextApply=0 end
 -- Hold orientation in every comparison without changing actual native position.
 -- The engine turned even the stationary proxy when production skipped an
 -- unchanged pose; that distinct observation is retained in the failed first runs.
 local ship=A.coop.ships[2];local actual=C.GetObjectPositionInSector(ship)
 local sector=C.GetContextByClass(ship,'sector',false)
 C.SetObjectSectorPos(ship,sector,ffi.new('UIPosRot',{x=actual.x,y=actual.y,z=actual.z,yaw=0,pitch=0,roll=0}))
 if desired~=requested or changed then
  requested=desired;AddUITriggeredEvent('X4LAN_ProjectileProbe','Velocity',desired)
 end
 local gameTime=GetCurTime()
 if now>=nextShip then
  nextShip=now+.2
  local p=position(A.coop.ships[2])
  local ok,details=pcall(function() return C.GetComponentDetails(A.coop.ships[2],'') end)
  local engineSpeed=ok and tonumber(details.speed) or -1
  DebugError(string.format('[X4LAN] PROJECTILE_PROBE ship phase=%s sourceTime=%.6f gameTime=%.6f x=%.5f y=%.5f z=%.5f wantedVx=%.3f configuredVx=%.3f engineSpeed=%.3f yaw=%.6f pitch=%.6f roll=%.6f',
   phase,sample.time,gameTime,p[1],p[2],p[3],sample.velocity[1],configuredVx,engineSpeed,p[4],p[5],p[6]))
 end
end
