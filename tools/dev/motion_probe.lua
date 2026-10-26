-- Compare real native transforms with the rendered source-clock track.
local A=X4LAN_BUBBLE_API
if not A or not A.universe or X4LAN_CONFIG.test_profile~='45841' then return end
local C=require('ffi').C;local frame=A.universe.frame;local nextLog=0
local function check(kind,id,object,track,now)
 if not object or not track or not C.IsComponentOperational(object) then return end
 local expected=X4LAN_MOTION.render(track,now);if not expected then return end
 local actual=C.GetObjectPositionInSector(object)
 local error=math.max(math.abs(actual.x-expected[1]),math.abs(actual.y-expected[2]),math.abs(actual.z-expected[3]))
 DebugError(string.format('[X4LAN] MOTION_PROBE kind=%s id=%d time=%.4f actual=%.3f expected=%.3f positionError=%.4f angleError=%.5f age=%.3f',
  kind,id,now,actual.x,expected[1],error,math.abs(X4LAN_MOTION.angle(actual.yaw-expected[4])),now-track.arrival))
end
function A.universe.frame(now)
 frame(now)
 if now<nextLog or A.paused() then return end;nextLog=now+.1
 local r=A.universe.replicas[1]
 if r then check('NPC',1,r.object,r.track,now) end
 local c=A.coop
 if c and c.remote[1] then check('PLAYER',1,c.ships[1],c.remote[1].track,now) end
end
