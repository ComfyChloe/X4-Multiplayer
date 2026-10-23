-- Optional saved-bay observations. A matching NAV/CDOCK pair is required before
-- free-flight writes; neither a missing packet nor an unknown dock means FREE.
-- Attachment execution is separate and is not enabled by this observer alone.
local cfg,A=X4LAN_CONFIG,X4LAN_BUBBLE_API
if not cfg or not cfg.coop_docks or not A or not A.coop then return end
local c,s=A.coop,A.state
local C=require('ffi').C
local d={localRows={},remote={},closed=false};A.coopDocks=d
local modes={FREE=true,UNKNOWN=true,APPROACH=true,EXTERNAL=true,STORED=true}
local function integer(v,max)
 local n=tonumber(v)
 if n and n==n and n>=0 and n%1==0 and n<=max then return n end
end
local function valid(mode,dock)
 return modes[mode] and dock and ((mode=='FREE' or mode=='UNKNOWN') and dock==0 or
  (mode=='APPROACH' or mode=='EXTERNAL' or mode=='STORED') and dock>0)
end
local function log(v) DebugError('[X4LAN] COOP_DOCK '..v) end
local function send(v) if s.client and s.client.lease then s.transport:send(v) end end
local function stop(reason)
 d.closed=true;d.ready=false;d.localRows={};d.remote={}
 c.stopVelocity(false);log('STOPPED '..tostring(reason))
end
local function localRow(e,now)
 local r=d.localRows[e]
 if not d.ready or not r or now-r.at>.3 or r.ship~=c.ships[e] then return nil end
 -- A native attachment can change between the MD read and this render frame.
 local docked=GetComponentData(A.id(c.ships[e]),'isdocked')==true
 if (r.mode=='FREE' or r.mode=='APPROACH') and docked then return nil end
 if (r.mode=='EXTERNAL' or r.mode=='STORED') and not docked then return nil end
 return r
end
function c.publishDock(e,seq,now)
 if not s.client or not s.client.lease then return end
 local r=localRow(e,now)
 send(string.format('CDOCK|1|%s|%d|%d|%d|%s|%d',s.client.lease,e,c.epochs[e],seq,
  r and r.mode or 'UNKNOWN',r and r.dock or 0))
end
function c.dockPoseAllowed(e,r,now)
 if d.closed or not r then return false end
 local q=d.remote[e];local own=localRow(e,now)
 return q and own and q.epoch==c.epochs[e] and q.lease==r.sourceLease and
  q.seq==r.seq and now-q.at<=.75 and q.mode=='FREE' and own.mode=='FREE'
end
local receive,frame=c.receive,c.frame
function c.receive(f,now)
 if f[1]~='CDOCK' and f[1]~='CDOCK_DENIED' then
  local result=receive(f,now)
  if f[1]=='AUTH' then
   for e,q in pairs(d.remote) do if c.owners[e]==cfg.slot or q.epoch~=c.epochs[e] then d.remote[e]=nil end end
  end
  return result
 end
 if not s.client or f[3]~=s.client.lease or d.closed then return true end
 if f[1]=='CDOCK_DENIED' then
  if #f~=7 then return true end
  local e,epoch,seq,dock=integer(f[4],2),integer(f[5],2147483647),integer(f[6],2147483647),integer(f[7],250000)
  if e and e>0 and epoch==c.epochs[e] and c.owners[e]==cfg.slot and seq and dock and dock>0 then
   local old=d.denied
   d.denied={entity=e,epoch=epoch,seq=seq,dock=dock,at=now}
   if not old or old.entity~=e or old.epoch~=epoch or old.dock~=dock then
    log(string.format('RESERVATION_DENIED entity=%d dock=%d',e,dock))
   end
  end
  return true
 end
 if #f~=9 or type(f[4])~='string' or #f[4]~=32 or not f[4]:match('^[a-f0-9]+$') then return true end
 local e,epoch,seq,dock=integer(f[5],2),integer(f[6],2147483647),integer(f[7],2147483647),integer(f[9],250000)
 if not e or e<1 or not epoch or not seq or not valid(f[8],dock) or c.owners[e]==cfg.slot or c.epochs[e]~=epoch then return true end
 local old=d.remote[e]
 if old and old.epoch==epoch and old.lease==f[4] and seq<=old.seq then return true end
 d.remote[e]={epoch=epoch,seq=seq,lease=f[4],mode=f[8],dock=dock,at=now}
 return true
end
function c.frame(now)
 if c.ready and not d.setup and not d.closed then
  d.setup=true
  if type(cfg.seed_id)~='string' or #cfg.seed_id~=32 or not cfg.seed_id:match('^[a-f0-9]+$') then
   stop('saved world identity is required')
  else AddUITriggeredEvent('X4LAN_CoopDocks','Setup',cfg.seed_id) end
 end
 if d.ready and not A.paused() and now>=(d.nextRead or 0) then
  d.nextRead=now+.05;AddUITriggeredEvent('X4LAN_CoopDocks','Read')
 end
 frame(now)
end
RegisterEvent('X4LAN_CoopDocks.Ready',function(_,id)
 if not d.closed and id==cfg.seed_id then d.ready=true;log('READY') end
end)
RegisterEvent('X4LAN_CoopDocks.Error',function(_,reason) stop(reason) end)
RegisterEvent('X4LAN_CoopDocks.Row',function(_,payload)
 if not d.ready or d.closed then return end
 local age,stamp,e,ship,mode,dock=tostring(payload):match('^([^|]+)|([%d%-T:]+)|(%d)|([^|]+)|(%u+)|(%d+)$')
 age,e,dock=tonumber(age),integer(e,2),integer(dock,250000)
 if not age or age~=age or not stamp or #stamp~=19 or not e or e<1 or not valid(mode,dock) then return end
 local ok,id=pcall(ConvertIDTo64Bit,ship)
 if not ok or id~=c.ships[e] then return end
 d.localRows[e]={ship=id,age=age,stamp=stamp,mode=mode,dock=dock,at=GetCurRealTime()}
end)
