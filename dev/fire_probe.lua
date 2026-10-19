-- Opt-in native source-capture calibration. Shoots only in a disposable host world.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.coop or cfg.test_profile~='45841' or cfg.slot~='p1' or not cfg.fire_probe then return end
local frame=A.universe.frame;local started,on,off,nextLog
function A.universe.frame(now)
 frame(now)
 if not A.coop.ready or not A.state.client or not A.state.client.lease or A.paused() then return end
 started=started or now
 if not on and now-started>=5 then
  on=now;AddUITriggeredEvent('X4LAN_Coop','Fire2',{ship=1,primary=true,secondary=false})
  DebugError('[X4LAN] FIRE_PROBE primary tap started')
 elseif on and not off and now-on>=.05 then
  off=now;AddUITriggeredEvent('X4LAN_Coop','Fire2',{ship=1,primary=false,secondary=false})
  DebugError('[X4LAN] FIRE_PROBE primary tap stopped')
 end
 if now>=(nextLog or 0) and now-started<=25 then
  nextLog=now+.2
  local t=A.coop.triggerShots[1]
  DebugError(string.format('[X4LAN] FIRE_PROBE elapsed=%.3f sourceShots=%d primary=%d secondary=%d',
   now-started,A.coop.shots[1],t[1],t[2]))
 end
end
