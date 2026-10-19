-- Original INTERNAL W01 NPC replication adapter. No third-party framework.
-- Ordinary cargo, NPC combat, station economy and missions remain separate work.
local cfg=X4LAN_CONFIG;local A=X4LAN_BUBBLE_API;local P=X4LAN_UNIVERSE_PROTOCOL
if not cfg or not cfg.world_sync or not A or not A.state.active then return end
local ffi=require('ffi');local C=ffi.C;local s=A.state
local M=assert(X4LAN_MOTION,'motion buffer missing')
assert(cfg.test_profile=='45841' and cfg.build:sub(1,7)=='world1-','world adapter requires isolated W01 build')
assert(cfg.world_mode=='observe' or cfg.world_mode=='replica','select an explicit world test mode')
ffi.cdef[[
uint32_t GetAllFactions(const char**,uint32_t,bool);
uint32_t GetNumAllFactionShips(const char*);
uint32_t GetAllFactionShips(UniverseID*,uint32_t,const char*);
bool IsComponentClass(UniverseID,const char*);
void ActivateObject(UniverseID,bool);
void RemoveComponent(UniverseID);
]]
local u={receiver=P.receiver(),excluded={},identities={},nextId=0,revision=0,interestSeq=0,
 replicas={},replicaKeys={},motionSeq=0,publish={},motionCursor=1}
A.universe=u
local function log(v) DebugError('[X4LAN] UNIVERSE '..v) end
local function key(object) return tostring(object):gsub('ULL$','') end
local function send(p) s.transport:send(p) end
local function protected(object)
 local k=key(object)
 return u.excluded[k] or u.replicaKeys[k] or A.coop.entity(object)>0 or
  object==C.GetPlayerControlledShipID() or object==C.GetPlayerOccupiedShipID() or object==C.GetPlayerContainerID()
end
local function currentSector()
 local object=C.GetPlayerContainerID()
 if object==0 then return nil end
 local sector=C.GetContextByClass(object,'sector',false)
 if sector==0 then return nil end
 local name=GetComponentData(A.id(sector),'macro')
 if not P.atom(name,true) then return nil end
 A.coop.sectors[name]=sector;return name,sector
end
local function beginScan(name,sector)
 local factions=ffi.new('const char*[256]')
 local count=tonumber(C.GetAllFactions(factions,256,true))
 assert(count>0 and count<256,'faction enumeration unavailable or exceeded limit; refusing an incomplete baseline')
 local names={};for i=0,count-1 do names[#names+1]=ffi.string(factions[i]) end
 return {name=name,sector=sector,factions=names,faction=1,index=0,seen={},objects={},total=0}
end
local function scanStep(scan)
 -- Bound per-frame enumeration; metadata/positions are read only for this sector.
 for _=1,512 do
  if not scan.buffer or scan.index>=scan.count then
   local faction=scan.factions[scan.faction]
   if not faction then return true end
   scan.faction=scan.faction+1
   local count=tonumber(C.GetNumAllFactionShips(faction))
   assert(count<=250000,'faction fleet exceeds internal scan limit')
   -- Headroom plus post-read count catches growth/truncation during enumeration.
   local cap=count+64;scan.buffer=ffi.new('UniverseID[?]',cap)
   scan.count=tonumber(C.GetAllFactionShips(scan.buffer,cap,faction));scan.index=0
   if scan.count>=cap or tonumber(C.GetNumAllFactionShips(faction))>scan.count then
    -- A changing fleet invalidates this capture, not the entire world adapter.
    scan.retry=true;return true
   end
   return false
  end
  local object=scan.buffer[scan.index];scan.index=scan.index+1
  local k=key(object)
  if not scan.seen[k] then
   scan.seen[k]=true;scan.total=scan.total+1
   assert(scan.total<=250000,'universe fleet exceeds internal scan limit')
   if C.IsComponentOperational(object) and not protected(object) and C.IsComponentClass(object,'ship') and
      C.GetContextByClass(object,'sector',false)==scan.sector then
    local macro,owner=GetComponentData(A.id(object),'macro','owner')
    assert(P.atom(macro,true) and P.atom(owner),'NPC descriptor unavailable; refusing incomplete baseline')
    if owner~='player' then
     scan.objects[#scan.objects+1]={object=object,key=k,macro=macro,owner=owner}
     assert(#scan.objects<=P.limit,'sector object limit exceeded; refusing a truncated baseline')
    end
   end
  end
 end
 return false
end
local function pose(object)
 local p=C.GetObjectPositionInSector(object);return {p.x,p.y,p.z,p.yaw,p.pitch,p.roll}
end
local function poseFields(p)
 local out={};for _,v in ipairs(p) do out[#out+1]=string.format('%.9g',v) end;return table.concat(out,',')
end
local function captured(scan)
 local rows={};local objects={}
 for _,entry in ipairs(scan.objects) do
  if C.IsComponentOperational(entry.object) and C.GetContextByClass(entry.object,'sector',false)==scan.sector then
   local identity=u.identities[entry.key]
   if not identity or identity.macro~=entry.macro then
    u.nextId=u.nextId+1;assert(u.nextId<=2147483647,'world ID space exhausted')
    identity={id=u.nextId,macro=entry.macro};u.identities[entry.key]=identity
   end
   local row=identity.id..',ship,'..entry.macro..','..entry.owner..','..poseFields(pose(entry.object))..',none,0'
   assert(P.record(row),'invalid captured NPC state')
   objects[#objects+1]={id=identity.id,object=entry.object,sector=scan.name,identity=identity}
   rows[#rows+1]={id=identity.id,row=row}
  end
 end
 table.sort(rows,function(a,b) return a.id<b.id end)
 local ordered={};for i,r in ipairs(rows) do ordered[i]=r.row end
 u.revision=u.revision+1;u.publish=objects;u.motionCursor=1
 u.sender=P.sender(P.baseline(s.client.lease,u.revision,scan.name,ordered))
 log('CAPTURE revision='..u.revision..' sector='..scan.name..' objects='..#ordered)
end
local function removeReplica(r)
 local object=r.object
 if object and C.IsComponentOperational(object) then
  -- Recheck live player IDs; a player may have boarded since creation.
  if A.coop.entity(object)>0 or object==C.GetPlayerControlledShipID() or object==C.GetPlayerOccupiedShipID() or object==C.GetPlayerContainerID() then
   error('player entered an NPC replica; end this internal scene test')
  end
  C.RemoveComponent(object)
 end
 if object then u.replicaKeys[key(object)]=nil end
end
local function accept(b,now)
 local name=currentSector();if b.sector~=name then return end
 u.acceptedAt=now;u.latest=b
 if cfg.world_mode=='observe' then log('OBSERVED revision='..b.revision..' sector='..b.sector..' objects='..#b.rows);return end
 local wanted={};u.renderIds={};u.renderCursor=1
 for _,row in ipairs(b.rows) do
  local entity=assert(P.record(row));assert(entity.kind=='ship','drop engine adapter is not implemented')
  wanted[entity.id]=entity
  u.renderIds[#u.renderIds+1]=entity.id
 end
 for id,r in pairs(u.replicas) do
  local next=wanted[id]
  if not next or next.macro~=r.entity.macro or next.owner~=r.entity.owner or b.sector~=r.sector then
   removeReplica(r);u.replicas[id]=nil
  end
 end
 for id,entity in pairs(wanted) do
  local r=u.replicas[id]
  if not r then r={entity=entity,sector=b.sector,seq=-1};u.replicas[id]=r
  else
   r.entity=entity
   if not r.at or now-r.at>=M.stale then r.track=nil;r.target=nil end
  end
 end
 log('COMMITTED revision='..b.revision..' sector='..b.sector..' objects='..#b.rows)
end
function u.receive(f,now)
 if f[1]=='UINTEREST' then
  if cfg.slot=='p1' and #f==5 and P.integer(f[4]) and P.atom(f[5],true) then u.wanted=f[5] end
  return true
 elseif f[1]=='UACK' then
  local rev=P.integer(f[5]);local index=f[6]=='-1' and -1 or P.integer(f[6])
  if cfg.slot=='p1' and #f==6 and rev and index and u.sender then u.sender:ack(f[4],rev,index) end
  return true
 elseif f[1]=='UPOS' or f[1]=='UMOVE' then
  local modern=f[1]=='UMOVE'
  if cfg.slot~='p2' or #f~=(modern and 20 or 13) or not u.latest or f[4]~=u.latest.epoch then return true end
  local id,seq=P.integer(f[5]),P.integer(f[6]);local r=id and u.replicas[id]
  if not r or not seq or seq<=r.seq or f[7]~=r.sector then return true end
  local start=modern and 9 or 8
  local row=id..',ship,'..r.entity.macro..','..r.entity.owner..','..table.concat(f,',',start,start+5)..',none,0'
  local parsed=P.record(row);if not parsed then return true end
  local velocity={0,0,0,0,0,0};local time=now
  if modern then
   time=tonumber(f[8]);for i=1,6 do velocity[i]=tonumber(f[14+i]) end
  end
  local track=r.track or {}
  if not modern and not r.track then M.push(track,now-.1,r.entity.pose,velocity,now-.1) end
  if not M.push(track,time,parsed.pose,velocity,now) then return true end
  r.track=track;r.target=parsed.pose;r.at=now;r.seq=seq;r.modern=modern
  return true
 elseif f[1]=='UBEGIN' or f[1]=='UOBJECT' or f[1]=='UCOMMIT' then
  if cfg.slot~='p2' or u.failed then return true end
  local ack,b=u.receiver:receive(f,now)
  if b then u.incoming=b end -- Engine changes belong to an unpaused frame.
  if ack then send('UACK|1|'..s.client.lease..'|'..f[4]..'|'..f[5]..'|'..ack) end
  return true
 end
 return false
end
local function render(now,sector)
 local ids=u.renderIds or {};if #ids==0 then return end
 for _=1,math.min(128,#ids) do
  local r=u.replicas[ids[u.renderCursor]];u.renderCursor=u.renderCursor%#ids+1
  if r.sector==u.interest and (not r.nextApply or now>=r.nextApply) then
   r.nextApply=now+.015
   local p=r.entity.pose
   if r.track then p=M.render(r.track,now,r.modern and M.delay or .1) or p end
   local value=ffi.new('UIPosRot',{x=p[1],y=p[2],z=p[3],yaw=p[4]*180/math.pi,pitch=p[5]*180/math.pi,roll=p[6]*180/math.pi})
   if not r.object or not C.IsComponentOperational(r.object) then
    r.object=C.SpawnObjectAtPos2(r.entity.macro,sector,value,r.entity.owner)
    assert(r.object~=0,'NPC replica spawn failed');u.replicaKeys[key(r.object)]=true
    C.ActivateObject(r.object,false)
   end
   assert(r.object~=C.GetPlayerContainerID() and r.object~=C.GetPlayerControlledShipID(),'player in NPC replica')
   local changed=not r.applied
   if r.applied then for i=1,6 do if math.abs(p[i]-r.applied[i])>(i<=3 and .005 or .00001) then changed=true end end end
   if changed then C.SetObjectSectorPos(r.object,sector,value);r.applied=p end
  end
 end
end
local function frame(now)
 if u.failed or not s.client or not s.client.lease or not A.coop.ready then return end
 if not u.setup then u.setup=true;AddUITriggeredEvent('X4LAN_Universe','Setup');return end
 if not u.ready or A.paused() then return end
 local name,sector=currentSector();if not name then return end
 if cfg.slot=='p2' then
  if u.interest~=name then
   u.interest=name;u.scan=nil;u.receiver.pending=nil;u.latest=nil;u.nextScan=0
   for _,r in pairs(u.replicas) do removeReplica(r) end;u.replicas={}
  end
  if now>=(u.nextInterest or 0) then
   u.nextInterest=now+1;u.interestSeq=u.interestSeq+1
   send('UINTEREST|1|'..s.client.lease..'|'..u.interestSeq..'|'..name)
  end
  if u.incoming then local incoming=u.incoming;u.incoming=nil;accept(incoming,now) end
  if cfg.world_mode=='replica' and u.latest and now-u.acceptedAt<15 then
   render(now,sector)
   if not u.scan and now>=(u.nextScan or 0) then u.scan=beginScan(name,sector) end
   if u.scan and scanStep(u.scan) then
    if u.scan.retry then u.scan=nil;u.nextScan=now+1;return end
    local n=0
    for _,entry in ipairs(u.scan.objects) do
     if C.IsComponentOperational(entry.object) and not protected(entry.object) and C.GetContextByClass(entry.object,'sector',false)==sector then
      C.RemoveComponent(entry.object);n=n+1
     end
    end
    log('LOCAL_NPC_SUPPRESSED count='..n..' sector='..name);u.scan=nil;u.nextScan=now+5
   end
  end
  return
 end
 if u.sender and not u.sender.done then
  for _,p in ipairs(u.sender:packets(s.client.lease,now)) do send(p) end
 elseif u.wanted and not u.scan and now>=(u.nextScan or 0) then
  local target=A.coop.sectorId(u.wanted)
  if target then u.scan=beginScan(u.wanted,target) end
 end
 if u.scan and scanStep(u.scan) then
  if not u.scan.retry then captured(u.scan) end
  u.scan=nil;u.nextScan=now+2
 end
 if #u.publish>0 and now>=(u.nextMotion or 0) then
  u.nextMotion=now+.05
  for _=1,math.min(32,#u.publish) do
   local entry=u.publish[u.motionCursor];u.motionCursor=u.motionCursor%#u.publish+1
   local target=A.coop.sectors[entry.sector]
   if C.IsComponentOperational(entry.object) and C.GetContextByClass(entry.object,'sector',false)==target then
    u.motionSeq=u.motionSeq+1
    local p=pose(entry.object);local identity=entry.identity
    local velocity=M.velocity(identity.motion,p,now);identity.motion={time=now,pose=p}
    send('UMOVE|1|'..s.client.lease..'|'..s.client.lease..'|'..entry.id..'|'..u.motionSeq..'|'..entry.sector..'|'..
     string.format('%.6f',now)..'|'..poseFields(p):gsub(',','|')..'|'..poseFields(velocity):gsub(',','|'))
   end
  end
 end
end
function u.frame(now)
 local ok,err=pcall(frame,now)
 if not ok then u.failed=true;log('STOPPED '..tostring(err)) end
end
RegisterEvent('X4LAN_Universe.Exclude',function(_,object) u.excluded[key(ConvertIDTo64Bit(object))]=true end)
RegisterEvent('X4LAN_Universe.Ready',function() u.ready=true;log('READY mode='..cfg.world_mode..' slot='..cfg.slot) end)
log('ARMED W01: NPC scene only; cargo/combat/economy not synchronized')
