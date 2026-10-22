-- Explicit local diagnostic mutation: zero native velocity on admitted FREE
-- saved replicas. Not a production ownership/AI suppression implementation.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.replica_hold_probe then return end
assert(cfg.slot=='p2' and cfg.test_profile=='45841' and cfg.attachment_guest_probe)
local A=X4LAN_BUBBLE_API;local U=A.universe;local C=require('ffi').C
local expected=assert(X4LAN_NATIVE_REPLAY);local pending,held,attempts={},{},{}
X4LAN_REPLICA_HOLD_PENDING=pending
local function log(value) DebugError('[X4LAN] REPLICA_HOLD '..value) end
local function fail(why)
 U.failed=true;log('STOPPED '..tostring(why))
 if U.attachments then U.attachments:stop(why) end
 if not C.IsGamePaused() then Pause() end
end
RegisterEvent('X4LAN_ReplicaHoldProbe.Result',function(_,value)
 local seed,epoch,ship,outcome=tostring(value):match('^(%x+)|(%x+)|(%d+)|([a-z]+)$');ship=tonumber(ship)
 if seed~=cfg.seed_id or not U.latest or epoch~=U.latest.epoch or not ship or not pending[ship]
  or (outcome~='held' and outcome~='busy') then fail('unmatched/invalid native hold result');return end
 pending[ship]=nil;if outcome=='held' then held[ship]=true end
 log('ack id='..ship..' outcome='..outcome)
end)
local frame=U.frame
U.frame=function(now)
 frame(now)
 if U.failed or C.IsGamePaused() or not U.latest or not U.attachments then return end
 for ship,at in pairs(pending) do if now-at>5 then fail('native hold acknowledgment missing');return end end
 for ship,want in pairs(expected) do
  local r=U.replicas[ship]
  if want.state=='FREE' and not held[ship] and not pending[ship] and r and r.seedOriginal and r.object
   and U.attachments:allow_pose(ship,r.object,now) then
   attempts[ship]=(attempts[ship] or 0)+1
   if attempts[ship]>3 then fail('native hold repeatedly busy');return end
   pending[ship]=now
   log('request id='..ship..' epoch='..U.latest.epoch..' code='..want.code..' macro='..want.macro..' owner='..want.owner)
   AddUITriggeredEvent('X4LAN_ReplicaHoldProbe','Hold',{seed=cfg.seed_id,epoch=U.latest.epoch,
    ship=ship,code=want.code,macro=want.macro,owner=want.owner})
  end
 end
end
