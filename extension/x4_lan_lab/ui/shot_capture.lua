-- SHOT capture and undo state (md/x4lan_shot_capture.xml), dev scope, INERT: shot_capture=false,
-- and it needs shot_claims (shot_game.lua) and pilot_health. MD does the per-callback work; this
-- module only tells MD, under the HEALTH readback window, which co-op ship I pilot, which ones the
-- other player flies here (their shots are replays), and the admitted bullet lifetime for the
-- in-flight quarantine: range / bulletspeed from the weapon's native library entry, never
-- assumed. MD's native cumulative counts feed shot_game.lua as totals, never as increments.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not cfg or not cfg.coop or not cfg.shot_capture or not cfg.shot_claims or not cfg.pilot_health or not A or not A.shot or not A.health or not A.coop then return end
local c,s,h,d=A.coop,A.state,A.health,A.shot
local k={revision=0};A.shotCapture=k
local INTERVAL,READING,MAXLIFE=.25,.5,30
local function log(v) DebugError('[X4LAN] '..v) end
local function paused() return A.paused and A.paused() end
local function writable() return h.mdStamp and (h.now or 0)-h.mdAt<=READING and not paused() end

-- Seconds a bullet of the admitted weapon of entity e can fly; nil refuses capture.
function k.lifetime(e)
 local sup=d.support[e]
 if not sup then return nil end
 local ok,entry=pcall(function() return GetLibraryEntry(GetMacroData(sup.macro,'infolibrary'),sup.macro) end)
 local range=ok and type(entry)=='table' and tonumber(entry.range)
 local speed=ok and type(entry)=='table' and tonumber(entry.bulletspeed)
 if not range or not speed or range<=0 or speed<=0 or range/speed>MAXLIFE then return nil end
 return range/speed
end

local function piloted()
 for e=1,2 do if c.owners[e]==cfg.slot and c.pilots[e]==cfg.slot then return e end end
 return 0
end
-- A co-op ship the OTHER player owns and flies: its shots are FIRE2 replays in my world.
local function remote(e) return c.owners[e]~=nil and c.owners[e]~=cfg.slot and c.pilots[e]==c.owners[e] end

function k.frame(now)
 if d.failed then k.fail('shot module stopped');return end
 if now<(k.next or 0) or not writable() then return end
 k.next=now+INTERVAL
 local me=piloted();local life=me>0 and k.lifetime(me)
 if not life then me,life=0,0 end
 k.revision=k.revision+1
 AddUITriggeredEvent('X4LAN_ShotCapture','State',{revision=k.revision,me=me,r1=remote(1),r2=remote(2),lifetime=life,
  stamp=h.mdStamp,from=h.mdAge,deadline=h.mdAge+1})
end

-- MD 'victim|shooterGen|victimGen|count': the native cumulative count of my own ship's hits.
function k.count(payload)
 local v,sg,vg,n=tostring(payload):match('^(%d)|(%d+)|(%d+)|(%d+)$')
 if k.failed or not v then return end
 d.cumulative(tonumber(v),tonumber(sg),tonumber(vg),tonumber(n))
end

-- A failure stops capture and undo natively for the session.
function k.fail(err)
 if k.failed then return end
 k.failed=true;log('SHOT_CAPTURE STOPPED '..tostring(err))
 pcall(AddUITriggeredEvent,'X4LAN_ShotCapture','Stop',{})
end

local frame=c.frame
function c.frame(now)
 frame(now)
 if d.ready and not k.setup then k.setup=true;AddUITriggeredEvent('X4LAN_ShotCapture','Setup') end
 if k.ready and not k.failed then
  local ok,err=pcall(k.frame,now)
  if not ok then k.fail(err) end
 end
end
RegisterEvent('X4LAN_ShotCapture.Ready',function() k.ready=true;log('SHOT_CAPTURE_READY slot='..cfg.slot) end)
RegisterEvent('X4LAN_ShotCapture.Count',function(_,payload) if k.ready then k.count(payload) end end)
