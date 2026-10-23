-- Victim-pilot health authority (HEALTH), dev scope: reports/VICTIM-PILOT-HEALTH-DESIGN.txt.
-- The machine that owns AND pilots a co-op ship reports that ship's own hull/shield; every hit
-- it takes in that pilot's world counts, so no attacker attribution is needed. The host sets
-- the reported values on its proxy and keeps the proxy at a 2% hull floor meanwhile, so
-- host-world damage never reaches the 1.01% life threshold; a reported lethal value restores
-- the 1% floor and the existing life tick declares the death.
-- HEALTH|1|lease|entity|epoch|seq|hull%|shield%; forwarded to the host with the source lease.
-- Requires the guest WORLD Apply to skip the ship it pilots (Arena.$healthOwn, not yet wired).
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not cfg or not cfg.coop or not cfg.pilot_health or not A or not A.coop or not A.state.active then return end
local c=A.coop;local s=A.state
local h={seq=0,applied={},remote={},owned={false,false}};A.health=h
local INTERVAL=.1
local function log(v) DebugError('[X4LAN] '..v) end
local function number(v,max)
 local n=tonumber(v);if not n or n~=n or n<0 or n>max then return nil end;return n
end
local function integer(v,max) local n=number(v,max);if n and n%1==0 then return n end end
local function hex(v) return type(v)=='string' and #v==32 and v:match('^[a-f0-9]+$')~=nil end
function h.mine(e) return c.owners[e]==cfg.slot and c.pilots[e]==cfg.slot and c.epochs[e]~=nil end

-- Pilot side: MD reads back the occupied co-op ship at 10 Hz as 'entity|hull%|shield%'.
function h.own(payload)
 local e,hu,sh=tostring(payload):match('^(%d)|([^|]+)|([^|]+)$')
 e,hu,sh=integer(e,2),number(hu,100),number(sh,100)
 if not e or e<1 or not hu or not sh then return false end
 local old=h.reading;h.reading={entity=e,hull=hu,shield=sh}
 if old and old.entity==e and (hu<old.hull or sh<old.shield) then h.urgent=true end
 return true
end

-- Host: HEALTH|1|hostLease|sourceLease|entity|epoch|seq|hull%|shield%
function h.receive(f)
 if f[1]~='HEALTH' then return false end
 if cfg.slot~='p1' or #f~=9 or not hex(f[3]) or not hex(f[4]) then return true end
 local e,epoch,seq=integer(f[5],2),integer(f[6],2147483647),integer(f[7],2147483647)
 local hu,sh=number(f[8],100),number(f[9],100)
 if not e or e<1 or not epoch or not seq or not hu or not sh then return true end
 local owner=c.owners[e]
 if not owner or owner==cfg.slot or c.pilots[e]~=owner or c.epochs[e]~=epoch then return true end
 local key=f[4]..'|'..e..'|'..epoch
 if seq<=(h.applied[key] or 0) then return true end
 if A.paused and A.paused() then return true end    -- a later frame carries the newer value
 for k in pairs(h.applied) do if k~=key and k:sub(34,34)==tostring(e) then h.applied[k]=nil end end
 h.applied[key]=seq;h.remote[e]={epoch=epoch,owner=owner}
 AddUITriggeredEvent('X4LAN_Health','Apply',{entity=e,hull=hu,shield=sh})
 return true
end

function h.frame(now)
 -- Guest: which co-op ship's health the WORLD Apply must leave alone.
 for e=1,2 do
  local m=cfg.slot~='p1' and h.mine(e)
  if m~=h.owned[e] then h.owned[e]=m;AddUITriggeredEvent('X4LAN_Health','Owned',{entity=e,own=m}) end
 end
 if cfg.slot=='p1' then
  -- Authority ended (transfer, new epoch, no pilot): restore the 1% floor, keep the native value.
  for e,r in pairs(h.remote) do
   if c.owners[e]~=r.owner or c.pilots[e]~=r.owner or c.epochs[e]~=r.epoch then
    h.remote[e]=nil;AddUITriggeredEvent('X4LAN_Health','Release',{entity=e})
   end
  end
  return
 end
 local r=h.reading
 if not r or not h.mine(r.entity) or not s.client or not s.client.lease then return end
 if not h.urgent and now<(h.nextSend or 0) then return end
 h.urgent,h.nextSend,h.seq=false,now+INTERVAL,h.seq+1
 s.transport:send(string.format('HEALTH|1|%s|%d|%d|%d|%.3f|%.3f',s.client.lease,r.entity,c.epochs[r.entity],h.seq,r.hull,r.shield))
end

local receive,frame=c.receive,c.frame
function c.receive(f,now) if h.receive(f) then return true end return receive(f,now) end
function c.frame(now)
 frame(now)
 if c.ready and not h.setup then h.setup=true;AddUITriggeredEvent('X4LAN_Health','Setup') end
 if h.ready and not h.failed then
  local ok,err=pcall(h.frame,now)
  if not ok then h.failed=true;log('HEALTH STOPPED '..tostring(err)) end
 end
end
RegisterEvent('X4LAN_Health.Ready',function() h.ready=true;log('HEALTH_READY slot='..cfg.slot) end)
RegisterEvent('X4LAN_Health.Own',function(_,payload) if h.ready then h.own(payload) end end)
