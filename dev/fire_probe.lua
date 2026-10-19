-- Opt-in native source-capture calibration. Shoots only in a disposable host world.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.coop or cfg.test_profile~='45841' or cfg.slot~='p1' or not cfg.fire_probe then return end
local frame=A.universe.frame;local started,on,nextLog;local step=1
local plan=cfg.fire_probe_dual and {{5,true,false},{10,false,true},{15,true,true}} or {{5,true,false}}
function A.universe.frame(now)
 frame(now)
 if not A.coop.ready or not A.state.client or not A.state.client.lease or A.paused() then return end
 if not started then
  started=now
  if cfg.fire_probe_dual then AddUITriggeredEvent('X4LAN_FireProbe','ConfigureDual') end
 end
 local tap=plan[step]
 if on and now-on>=.05 then
  AddUITriggeredEvent('X4LAN_FireProbe','Tap',{primary=false,secondary=false});on=nil;step=step+1
 elseif not on and tap and now-started>=tap[1] then
  on=now;AddUITriggeredEvent('X4LAN_FireProbe','Tap',{primary=tap[2],secondary=tap[3]})
  DebugError('[X4LAN] FIRE_PROBE tap '..step..' started')
 end
 if now>=(nextLog or 0) and now-started<=25 then
  nextLog=now+.2
  local t=A.coop.triggerShots[1]
  DebugError(string.format('[X4LAN] FIRE_PROBE elapsed=%.3f sourceShots=%d primary=%d secondary=%d',
   now-started,A.coop.shots[1],t[1],t[2]))
 end
end
