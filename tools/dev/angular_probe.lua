-- Read-only timing/orientation observation around the real cooperative frame.
-- Requires the separate projectile probe; never shipped in test packages.
local A=X4LAN_BUBBLE_API
local cfg=X4LAN_CONFIG
if not A or not A.coop or cfg.test_profile~='45841' or cfg.slot~='p1'
 or not cfg.projectile_probe or not cfg.angular_probe then return end
local C=require('ffi').C
local coopFrame,universeFrame=A.coop.frame,A.universe.frame
local setup,ready=false,false
local serial=0
RegisterEvent('X4LAN_AngularProbe.Ready',function() ready=true end)
function A.universe.frame(now)
 universeFrame(now)
 if not setup and A.coop.ready and A.state.client and A.state.client.lease and not A.paused() then
  setup=true;AddUITriggeredEvent('X4LAN_AngularProbe','Setup')
 end
end
function A.coop.frame(now)
 local r=A.coop.remote[2]
 local samples=r and r.track and r.track.samples
 local sample=samples and samples[#samples]
 local ship=A.coop.ships[2]
 local observe=ready and sample and ship and C.IsComponentOperational(ship) and not A.paused()
 local elapsed=observe and sample.time-500 or -1
 observe=observe and elapsed>=0 and elapsed<48
 local before,timeBefore
 if observe then before=C.GetObjectPositionInSector(ship);timeBefore=GetCurTime() end
 coopFrame(now)
 if not observe then return end
 local after=C.GetObjectPositionInSector(ship);local timeAfter=GetCurTime()
 local phase=elapsed<12 and 'stationary' or elapsed<24 and 'rendered' or elapsed<36 and 'physical' or 'native'
 serial=serial+1
 DebugError(string.format('[X4LAN] ANGULAR_PROBE frame=%d phase=%s elapsed=%.6f beforeTime=%.6f afterTime=%.6f beforeYaw=%.9f beforePitch=%.9f beforeRoll=%.9f afterYaw=%.9f afterPitch=%.9f afterRoll=%.9f',
  serial,phase,elapsed,timeBefore,timeAfter,before.yaw,before.pitch,before.roll,after.yaw,after.pitch,after.roll))
 AddUITriggeredEvent('X4LAN_AngularProbe','Sample',{serial=serial,phase=phase,time=timeAfter})
end
