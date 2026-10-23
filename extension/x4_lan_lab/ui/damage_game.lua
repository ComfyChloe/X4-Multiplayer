-- Shooter-confirmed hits (HIT2). Host X4 stays the only writer of shared health.
-- The guest's own ship hits host-authoritative objects in the guest's world; FIRE2 replays the
-- same trigger on the host, but one latency later in different geometry, so those hits were
-- intermittent. The guest reports the cumulative shield/hull damage its own ship dealt there,
-- the host applies only the increase, and md/x4lan_damage.xml undoes the replayed ship's hits
-- on the host so nothing counts twice.
-- HIT2|1|lease|entity|epoch|seq|shield1|hull1|shield2|hull2|shield3|hull3|shield4|hull4
-- Victims 1-2 are the co-op ships, 3-4 the LAN targets. Totals are monotonic per entity epoch.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not cfg or not cfg.coop or not cfg.damage_claims or not A or not A.coop or not A.state.active then return end
local c=A.coop;local s=A.state
local d={seq=0,applied={},attack={},pending={},undo={}};A.damage=d
local VICTIMS,LIMIT,RESEND,ATTRIBUTION=4,1e9,.25,1.0
local function log(v) DebugError('[X4LAN] '..v) end
local function number(v)
 local n=tonumber(v);if not n or n~=n or n<0 or n>LIMIT then return nil end;return n
end
local function integer(v,limit)
 local n=number(v);if not n or n%1~=0 or n>limit then return nil end;return n
end
local function hex(v) return type(v)=='string' and #v==32 and v:match('^[a-f0-9]+$')~=nil end

-- Native damage events carry no attacker, attack events no amount. A victim's damage belongs to
-- its last attacker within ATTRIBUTION game seconds; damage reported before the attack event of
-- the same game time waits for it. Only the two co-op ships are attackers here.
function d.attribute(v,sh,hu)
 local a=d.attack[v];local e=a.entity
 if e<1 or e==v then return end                    -- another attacker, or a ship hitting itself
 if cfg.slot=='p1' then
  -- The replayed remote ship: its pilot reports this hit from its own world.
  if c.owners[e] and c.owners[e]~=cfg.slot then local u=d.undo[v] or {0,0};u[1],u[2]=u[1]+sh,u[2]+hu;d.undo[v]=u end
 elseif a.own then d.claim(e,v,sh,hu) end
end

-- 'victim|attackerEntity|own|gameSeconds'; attackerEntity 0 is not a co-op ship.
function d.attacked(payload)
 local v,e,own,t=payload:match('^(%d)|(%d)|([01])|([^|]+)$')
 v,e,t=integer(v,VICTIMS),integer(e,2),tonumber(t)
 if not v or v<1 or not e or not t or t~=t then return false end
 d.attack[v]={entity=e,own=own=='1',at=t}
 local p=d.pending[v]
 if p and p.at==t then d.pending[v]=nil;d.attribute(v,p.shield,p.hull) end
 return true
end

-- 'victim|s or h|damage|gameSeconds'
function d.damaged(payload)
 local v,kind,amount,t=payload:match('^(%d)|([sh])|([^|]+)|([^|]+)$')
 v,amount,t=integer(v,VICTIMS),number(amount),tonumber(t)
 if not v or v<1 or not amount or not t or t~=t then return false end
 local sh,hu=kind=='s' and amount or 0,kind=='h' and amount or 0
 local a=d.attack[v]
 if a and t>=a.at and t-a.at<=ATTRIBUTION then d.attribute(v,sh,hu);return true end
 local p=d.pending[v]
 if not p or p.at~=t then p={at=t,shield=0,hull=0};d.pending[v]=p end
 p.shield,p.hull=p.shield+sh,p.hull+hu
 return true
end

-- Guest: one hit of its own ship on a host-authoritative victim.
function d.claim(e,v,sh,hu)
 if c.owners[e]~=cfg.slot or not c.epochs[e] then return end
 if not d.totals or d.entity~=e or d.epoch~=c.epochs[e] then
  d.entity,d.epoch,d.totals=e,c.epochs[e],{0,0,0,0,0,0,0,0}
 end
 local t=d.totals
 t[v*2-1]=math.min(LIMIT,t[v*2-1]+sh);t[v*2]=math.min(LIMIT,t[v*2]+hu)
 d.dirty=true
 log(string.format('DMG_CLAIM entity=%d epoch=%d victim=%d shield=%.3f hull=%.3f',e,d.epoch,v,sh,hu))
end

function d.frame(now)
 if cfg.slot=='p1' then
  for v,u in pairs(d.undo) do AddUITriggeredEvent('X4LAN_Damage','Undo',{victim=v,shield=u[1],hull=u[2]}) end
  d.undo={}
  return
 end
 if not d.totals or not s.client or not s.client.lease then return end
 if c.owners[d.entity]~=cfg.slot or c.epochs[d.entity]~=d.epoch then d.totals=nil;return end
 if not d.dirty and now<(d.nextSend or 0) then return end
 d.seq=d.seq+1;d.dirty=false;d.nextSend=now+RESEND
 local fields={'HIT2','1',s.client.lease,tostring(d.entity),tostring(d.epoch),tostring(d.seq)}
 for i=1,8 do fields[#fields+1]=string.format('%.3f',d.totals[i]) end
 s.transport:send(table.concat(fields,'|'))
end

-- Host: HIT2|1|hostLease|sourceLease|entity|epoch|seq|8 totals, forwarded by the relay.
function d.receive(f)
 if f[1]~='HIT2' then return false end
 if cfg.slot~='p1' or #f~=15 or not hex(f[3]) or not hex(f[4]) then return true end
 local e,epoch,seq=integer(f[5],2),integer(f[6],2147483647),integer(f[7],2147483647)
 if not e or e<1 or not epoch or not seq or c.owners[e]==cfg.slot or c.epochs[e]~=epoch then return true end
 local totals={}
 for i=1,8 do totals[i]=number(f[i+7]);if not totals[i] then return true end end
 local key=f[4]..'|'..e..'|'..epoch
 local old=d.applied[key]
 if old and seq<=old.seq then return true end
 local delta,any={},false
 for i=1,8 do
  delta[i]=totals[i]-(old and old.totals[i] or 0)
  if delta[i]<0 then return true end
  if delta[i]>0 then any=true end
 end
 if A.paused and A.paused() then return true end  -- applied after the pause from the same totals
 if any then AddUITriggeredEvent('X4LAN_Damage','Apply',{entity=e,values=delta}) end
 for k in pairs(d.applied) do if k~=key and k:match('|'..e..'|%d+$') then d.applied[k]=nil end end
 d.applied[key]={seq=seq,totals=totals}
 return true
end

local receive,frame=c.receive,c.frame
function c.receive(f,now) if d.receive(f) then return true end return receive(f,now) end
function c.frame(now)
 frame(now)
 -- Coop's MD session owns the ships and targets; set up only after it is ready.
 if c.ready and not d.setup then d.setup=true;AddUITriggeredEvent('X4LAN_Damage','Setup') end
 if d.ready and not d.failed then
  local ok,err=pcall(d.frame,now)
  if not ok then d.failed=true;log('DMG STOPPED '..tostring(err)) end
 end
end
RegisterEvent('X4LAN_Damage.Ready',function() d.ready=true;log('DMG_READY slot='..cfg.slot) end)
RegisterEvent('X4LAN_Damage.Attacked',function(_,payload) if d.ready then d.attacked(tostring(payload)) end end)
RegisterEvent('X4LAN_Damage.Damaged',function(_,payload) if d.ready then d.damaged(tostring(payload)) end end)
