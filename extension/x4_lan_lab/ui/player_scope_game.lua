-- INTERNAL host-only player scope (docs/PLAYER-SCOPE.txt). Tells MD which shared ship the
-- remote player pilots so vanilla NPC interactions keyed to player.occupiedship can include
-- it. Halt notices are queued for a later guest relay; nothing is sent over the network yet.
local cfg,A=X4LAN_CONFIG,X4LAN_BUBBLE_API
if not cfg or not cfg.player_scope or cfg.slot~='p1' or not A or not A.coop then return end
local s,c=A.state,A.coop
local p={notices={},limit=32};A.playerScope=p
local function log(v) DebugError('[X4LAN] PLAYER_SCOPE '..v) end
-- AUTH pilots[i] is '0' or the movement owner; only a remote-owned, remote-piloted ship counts.
function p.guestIndex()
 for i=1,2 do if c.owners[i]=='p2' and c.pilots[i]=='p2' then return i end end
 return 0
end
function p.frame(now)
 if not c.ready or not s.client or not s.client.lease or A.paused() then return end
 if not p.setup then p.setup=true;AddUITriggeredEvent('X4LAN_PlayerScope','Setup');return end
 if not p.ready then return end
 local i=p.guestIndex()
 -- Periodic resend lets MD drop a mark after the ship is destroyed or reassigned.
 if i~=p.sent or now>=(p.nextSync or 0) then
  p.sent=i;p.nextSync=now+1;AddUITriggeredEvent('X4LAN_PlayerScope','Guest',i)
 end
end
RegisterEvent('X4LAN_PlayerScope.Ready',function() p.ready=true;log('READY host-side guest scope') end)
RegisterEvent('X4LAN_PlayerScope.Halt',function(_,v)
 local mode,response,code,owner,grace=tostring(v):match('^(%a+)|(%a+)|([%w%-]*)|([%w_]*)|([%d.]+)$')
 if (mode~='policehalt' and mode~='piratehalt') or (response~='comply' and response~='wait') then return end
 p.notices[#p.notices+1]={mode=mode,response=response,inspector=code,owner=owner,grace=tonumber(grace)}
 if #p.notices>p.limit then table.remove(p.notices,1) end
 log('HALT mode='..mode..' response='..response..' inspector='..code..' owner='..owner)
end)
log('ARMED host-side guest scope; guest halt notices are not relayed yet')
