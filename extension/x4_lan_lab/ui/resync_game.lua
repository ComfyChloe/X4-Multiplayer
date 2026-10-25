-- STATEHASH transport and sector capture requests; repairs use atomic W01 baselines.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.universe or not cfg.world_sync then return end
local U=A.universe;local P=X4LAN_UNIVERSE_PROTOCOL;local s=A.state
local M={sentFrame=0,nextSend=0};X4LAN_STATEHASH=M
local function log(v) DebugError('[X4LAN] RESYNC '..v) end
local function send(v) if s.client and s.client.lease then s.transport:send(v) end end
function M.capture(sample)
 if type(sample)~='table' or sample.complete~=true or type(sample.frame)~='number' or sample.frame<=M.sentFrame then return end
 M.latest=sample
end
local function capture(now)
 local sample=M.latest
 if not sample or sample.frame<=M.sentFrame or now<M.nextSend or not s.client or not s.client.lease then return end
 local epoch,sector,hashes
 if cfg.slot=='p1' then
  epoch=s.client.lease;sector=U.wanted
 else
  epoch=U.latest and U.latest.epoch;sector=U.latest and U.latest.sector
 end
 hashes=epoch and sector and sample.sectors[sector]
 if not hashes then return end
 M.latest=nil;M.sentFrame=sample.frame;M.nextSend=now+.25
 send(table.concat({'UHASH','1',s.client.lease,epoch,sample.frame,sector,hashes.identity,hashes.pose,hashes.health,'-','-'},'|'))
 send(table.concat({'UHASH','1',s.client.lease,epoch,sample.frame,'global','-','-','-',sample.rng,sample.time},'|'))
end
local function validBuckets(value)
 local left={identity=true,pose=true,health=true,rng=true,time=true};local count=0
 for name in (value..','):gmatch('(.-),') do if not left[name] then return false end;left[name]=nil;count=count+1 end
 return count>0
end
local receive=U.receive
function U.receive(f,now)
 if f[1]=='URESYNC' then
  if cfg.slot~='p1' or #f~=8 or f[4]~=s.client.lease then return true end
  local request=P.integer(f[5]);local after=P.integer(f[7])
  if not request or request==0 or not after or not P.atom(f[6],true) or not validBuckets(f[8]) then return true end
  if M.lastRequest and request<=M.lastRequest then return true end
  M.lastRequest=request
  if U.wanted==f[6] then
   U.resync={request=request,sector=f[6],after=after,buckets=f[8]};U.nextScan=0
   log('REQUEST request='..request..' sector='..f[6]..' after='..after..' buckets='..f[8])
  end
  return true
 end
 return receive(f,now)
end
local frame=U.frame
function U.frame(now)
 frame(now)
 if U.failed then return end
 if U.resync and (U.wanted~=U.resync.sector or
    (U.sender and U.sender.baseline.sector==U.resync.sector and U.sender.baseline.revision>U.resync.after)) then U.resync=nil end
 capture(now)
end
