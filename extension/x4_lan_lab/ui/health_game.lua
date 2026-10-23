-- Victim-pilot health authority (HEALTH), dev scope: reports/VICTIM-PILOT-HEALTH-DESIGN.txt.
-- The machine that owns AND pilots a co-op ship reports that ship's own hull/shield; every hit
-- it takes in that pilot's world counts, so no attacker attribution is needed. The host sets
-- the reported values on its proxy and keeps the proxy at a 2% hull floor meanwhile, so
-- host-world damage never reaches the 1.01% life threshold; a reported lethal value restores
-- the 1% floor and the existing life tick declares the death.
-- The relay keeps one generation per (owner, epoch, pilot) of a ship. The pilot first adopts the
-- host's canonical WORLD baseline of that generation, acknowledges it, and only then reports:
--  HBASE|1|lease|entity|gen|hull%|shield%        relay -> pilot (repeated until acknowledged)
--  HACK|1|lease|entity|gen                       pilot -> relay
--  HEALTH|1|lease|entity|gen|seq|hull%|shield%   pilot -> relay -> host (with the source lease)
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not cfg or not cfg.coop or not cfg.pilot_health or not A or not A.coop or not A.state.active then return end
local c=A.coop;local s=A.state
local h={seq=0,gen={0,0},last={0,0},remote={},owned={false,false}};A.health=h
local INTERVAL,FRESH=.1,1.0
local function log(v) DebugError('[X4LAN] '..v) end
local function number(v,max)
 local n=tonumber(v);if not n or n~=n or n<0 or n>max then return nil end;return n
end
local function integer(v,max) local n=number(v,max);if n and n%1==0 then return n end end
local function hex(v) return type(v)=='string' and #v==32 and v:match('^[a-f0-9]+$')~=nil end
local function send(v) if s.client and s.client.lease then s.transport:send(v) end end
function h.mine(e) return c.owners[e]==cfg.slot and c.pilots[e]==cfg.slot and c.epochs[e]~=nil end

-- Pilot: adopt a generation's baseline once; acknowledge every repeat of the adopted one.
function h.base(f,now)
 if cfg.slot=='p1' or #f~=7 or not s.client or f[3]~=s.client.lease then return end
 local e,gen,hu,sh=integer(f[4],2),integer(f[5],2147483647),number(f[6],100),number(f[7],100)
 if not e or e<1 or not gen or not hu or not sh or not h.mine(e) then return end
 local live=h.live
 if live and live.entity==e and live.gen==gen then
  if now>=(h.nextAck or 0) then h.nextAck=now+.25;send(string.format('HACK|1|%s|%d|%d',s.client.lease,e,gen)) end
  return
 end
 if h.basing and h.basing.entity==e and h.basing.gen==gen then return end
 h.live,h.basing=nil,{entity=e,gen=gen}
 AddUITriggeredEvent('X4LAN_Health','Base',{entity=e,gen=gen,hull=hu,shield=sh})
end

-- MD 'entity|gen' once it wrote that baseline on the occupied ship.
function h.based(payload)
 local e,gen=tostring(payload):match('^(%d)|(%d+)$');e,gen=tonumber(e),tonumber(gen)
 local b=h.basing
 if b and b.entity==e and b.gen==gen and h.mine(e) then
  h.live,h.basing,h.reading,h.seq,h.nextAck=b,nil,nil,0,0
  log(string.format('HEALTH_LIVE entity=%d gen=%d',e,gen))
 end
end

-- Pilot side: MD reads back the occupied co-op ship at 10 Hz as 'entity|hull%|shield%'.
function h.own(payload)
 local e,hu,sh=tostring(payload):match('^(%d)|([^|]+)|([^|]+)$')
 e,hu,sh=integer(e,2),number(hu,100),number(sh,100)
 if not e or e<1 or not hu or not sh or not h.live or h.live.entity~=e then return false end
 local old=h.reading;h.reading={entity=e,hull=hu,shield=sh}
 if old and (hu<old.hull or sh<old.shield) then h.urgent=true end
 return true
end

-- Host: HEALTH|1|hostLease|sourceLease|entity|gen|seq|hull%|shield%
function h.receive(f,now)
 if f[1]=='HBASE' then h.base(f,now);return true end
 if f[1]~='HEALTH' then return false end
 if cfg.slot~='p1' or #f~=9 or not hex(f[3]) or not hex(f[4]) then return true end
 local e,gen,seq=integer(f[5],2),integer(f[6],2147483647),integer(f[7],2147483647)
 local hu,sh=number(f[8],100),number(f[9],100)
 if not e or e<1 or not gen or not seq or not hu or not sh then return true end
 local owner=c.owners[e]
 if not owner or owner==cfg.slot or c.pilots[e]~=owner or gen<h.gen[e] then return true end
 if gen==h.gen[e] and seq<=h.last[e] then return true end
 if A.paused and A.paused() then return true end    -- a later frame carries the newer value
 h.gen[e],h.last[e]=gen,seq
 h.remote[e]={gen=gen,owner=owner,epoch=c.epochs[e],at=now}
 AddUITriggeredEvent('X4LAN_Health','Apply',{entity=e,gen=gen,hull=hu,shield=sh})
 return true
end

function h.frame(now)
 if cfg.slot=='p1' then
  -- Authority ended or went stale: restore the 1% floor and the native value.
  for e,r in pairs(h.remote) do
   if c.owners[e]~=r.owner or c.pilots[e]~=r.owner or c.epochs[e]~=r.epoch or now-r.at>FRESH then
    h.remote[e]=nil;AddUITriggeredEvent('X4LAN_Health','Release',{entity=e,gen=r.gen})
   end
  end
  return
 end
 if h.live and not h.mine(h.live.entity) then h.live=nil end
 if h.basing and not h.mine(h.basing.entity) then h.basing=nil end
 -- Which co-op ship's health the WORLD Apply must leave alone.
 for e=1,2 do
  local m=h.live~=nil and h.live.entity==e
  if m~=h.owned[e] then h.owned[e]=m;AddUITriggeredEvent('X4LAN_Health','Owned',{entity=e,own=m}) end
 end
 local r=h.reading
 if not h.live or not r or r.entity~=h.live.entity then return end
 if not h.urgent and now<(h.nextSend or 0) then return end
 h.urgent,h.nextSend,h.seq=false,now+INTERVAL,h.seq+1
 send(string.format('HEALTH|1|%s|%d|%d|%d|%.3f|%.3f',s.client.lease,r.entity,h.live.gen,h.seq,r.hull,r.shield))
end

local receive,frame=c.receive,c.frame
function c.receive(f,now) if h.receive(f,now) then return true end return receive(f,now) end
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
RegisterEvent('X4LAN_Health.Based',function(_,payload) if h.ready then h.based(payload) end end)
