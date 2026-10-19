-- Cooperative navigation: stable ship identities, sector-aware poses, one pilot per ship.
-- NAV2 appends source time (seconds) and velocity (m/s, rad/s) to NAV fields.
-- Source clocks need no wall-clock synchronization. Authority/sector transitions reset
-- tracks; motion.lua also resets after pauses or discontinuities and bounds prediction.
-- FIRE2 (entity|epoch|seq|sourceTime|primaryGroup|secondaryGroup|primaryShots|secondaryShots)
-- mirrors the active weapon groups (C API numbering 1-4) and per-trigger shot counters,
-- played back on the NAV2 source clock. The first FIRE2 of an owner epoch is a baseline:
-- it never replays earlier shots and ends the NAV shot-counter path for that epoch.
-- Counter mapping: the NAV fire counter equals primaryShots+secondaryShots for the same
-- epoch, so shots already shown through NAV (even if NAV overtook a delayed baseline) are
-- reconciled and never shown again. A local pause stops FIRE2 fire, clears queued edges and
-- adopts counters received meanwhile without queuing them; only post-resume shots play.
-- Without FIRE2 (older peers) the NAV/NAV2 shot counter remains the fire trigger.
local cfg=X4LAN_CONFIG;local A=X4LAN_BUBBLE_API
if not cfg or not cfg.coop or not A or not A.state.active then return end
local ffi=require('ffi');local C=ffi.C;local s=A.state
pcall(ffi.cdef,[[uint32_t GetDefensibleActiveWeaponGroup(UniverseID,bool);
void SetDefensibleActiveWeaponGroup(UniverseID,bool,uint32_t);]])
-- Without the weapon-group API, FIRE2 is neither sent nor accepted: received FIRE2 frames
-- are ignored, so no epoch is latched and the NAV shot-counter path stays in use.
local function api(name) local ok,f=pcall(function() return C[name] end);return ok and f or nil end
local getGroup,setGroup=api('GetDefensibleActiveWeaponGroup'),api('SetDefensibleActiveWeaponGroup')
local M=assert(X4LAN_MOTION,'motion buffer missing')
local c={ships={},sectors={},resolving={},owners={},epochs={},pilots={},remote={},seq={0,0},shots={0,0},docks={0,0},revision=-1,crewSeq=0,metricSeq=0,metrics={n=0,sum=0,max=0},
 fire2={},fire2Seq={0,0},triggerShots={{0,0},{0,0}}}
A.coop=c
local function log(v) DebugError('[X4LAN] '..v) end
local function integer(v,limit)
 local n=tonumber(v);if not n or n~=n or n<0 or n%1~=0 or n>(limit or 2147483647) then return nil end;return n
end
local function numeric(v,bound)
 local n=tonumber(v);if not n or n~=n or math.abs(n)>bound then return nil end;return n
end
local function send(p) if s.client and s.client.lease then s.transport:send(p) end end
local function stopFire2(i)
 local q=c.fire2[i]
 if q and (q.on[1] or q.on[2]) then AddUITriggeredEvent('X4LAN_Coop','Fire2',{ship=i,primary=false,secondary=false}) end
 if q then q.on={false,false};q.deadline={0,0};q.events={} end
end
function c.stopFire()
 for i,r in pairs(c.remote) do
  if r.firing then AddUITriggeredEvent('X4LAN_Coop','Fire',{ship=i,on=false}) end
  r.firing=false;r.fireDeadline=0
 end
 for i in pairs(c.fire2) do stopFire2(i) end
end
function c.entity(object)
 for i=1,2 do if c.ships[i] and object==c.ships[i] then return i end end
 return 0
end
function c.sectorId(name)
 if c.sectors[name] then return c.sectors[name] end
 if not c.resolving[name] then
  c.resolving[name]=true
  RegisterEvent('X4LAN_Coop.Sector.'..name,function(_,object)
   c.sectors[name]=ConvertIDTo64Bit(object);log('sector mapping ready: '..name)
  end)
  AddUITriggeredEvent('X4LAN_Coop','ResolveSector',name)
 end
 return nil
end
function c.prepare(now)
 if not c.setup then
  c.setup=true;local own=cfg.slot=='p1' and 1 or 2;c.ships[own]=s.ship;c.ships[3-own]=s.proxy
  AddUITriggeredEvent('X4LAN_Coop','Setup')
 end
 local m=c.metrics
 if c.lastFrame then local delta=now-c.lastFrame;if delta>0 then m.n=m.n+1;m.sum=m.sum+delta;m.max=math.max(m.max,delta) end end
 c.lastFrame=now
end
function c.receive(f,now)
 if f[1]=='AUTH' then
  if #f~=10 then return true end
  local revision=integer(f[4]);if not revision or revision<c.revision then return true end
  local owners,epochs,pilots={},{},{}
  for i=1,2 do
   local index=2+i*3;owners[i],epochs[i],pilots[i]=f[index],integer(f[index+1]),f[index+2]
   if (owners[i]~='p1' and owners[i]~='p2') or not epochs[i] or epochs[i]<1 or
      (pilots[i]~='0' and pilots[i]~=owners[i]) then return true end
  end
  for i=1,2 do
   if c.epochs[i]~=epochs[i] or c.owners[i]~=owners[i] then
    if c.remote[i] and c.remote[i].firing then AddUITriggeredEvent('X4LAN_Coop','Fire',{ship=i,on=false}) end
    stopFire2(i);c.fire2[i]=nil;c.fire2Seq[i]=0;c.triggerShots[i]={0,0}
    c.remote[i]=nil;c.seq[i]=0;c.shots[i]=0;c.motion=c.motion or {};c.motion[i]=nil
    log('ship '..i..' movement authority='..owners[i]..'; epoch='..epochs[i])
   end
  end
  c.owners,c.epochs,c.pilots,c.revision=owners,epochs,pilots,revision
  return true
 elseif f[1]=='NAV' or f[1]=='NAV2' then
  local modern=f[1]=='NAV2'
  if #f~=(modern and 23 or 16) then return true end
  local entity,epoch,seq=integer(f[5],2),integer(f[6]),integer(f[7]);local name=f[8]
  if not entity or entity<1 or not epoch or not seq or not name:match('^[%w_.-]+_macro$') or #name>96 then return true end
  if c.owners[entity]==cfg.slot or c.epochs[entity]~=epoch then return true end
  local p={};for i=1,6 do p[i]=numeric(f[i+8],i<=3 and 50000000 or 2*math.pi);if not p[i] then return true end end
  local dock,fire=integer(f[15],2),integer(f[16]);if not dock or not fire then return true end
  local r=c.remote[entity]
  if r and seq<=r.seq then return true end
  if not r or r.sector~=name then
   r={samples={},fire=fire,sector=name};c.remote[entity]=r
  end
  if modern then
   local velocity={};for i=1,6 do velocity[i]=tonumber(f[17+i]) end
   local track=r.track or {}
   if not M.push(track,tonumber(f[17]),p,velocity,now) then return true end
   r.track=track
  else r.track=nil end
  r.seq,r.at,r.dock=seq,now,dock
  -- Once FIRE2 is selected for this owner epoch, the NAV counter never triggers fire.
  local q=c.fire2[entity]
  if fire>r.fire and not (q and q.epoch==epoch) then r.fireDeadline=now+.35 end;r.fire=fire
  r.samples[#r.samples+1]={time=now,pose=p};while #r.samples>5 do table.remove(r.samples,1) end
  return true
 elseif f[1]=='FIRE2' then
  if #f~=12 then return true end
  local entity,epoch,seq=integer(f[5],2),integer(f[6]),integer(f[7])
  local time=numeric(f[8],1e12);local groups={integer(f[9],4),integer(f[10],4)};local shots={integer(f[11]),integer(f[12])}
  if not entity or entity<1 or not epoch or not seq or not time or time<0 then return true end
  if not groups[1] or groups[1]<1 or not groups[2] or groups[2]<1 or not shots[1] or not shots[2] then return true end
  if not (getGroup and setGroup) then return true end
  if c.owners[entity]==cfg.slot or c.epochs[entity]~=epoch then return true end
  local q=c.fire2[entity]
  if not q or q.epoch~=epoch then
   -- Baseline: adopt the counters without replaying them, and end NAV-driven fire now.
   -- 'shown' is the total already displayed: the NAV counter may be ahead of a delayed baseline.
   local r=c.remote[entity]
   if r and r.firing then AddUITriggeredEvent('X4LAN_Coop','Fire',{ship=entity,on=false}) end
   if r then r.firing=false;r.fireDeadline=0 end
   local shown=math.max(shots[1]+shots[2],r and r.fire or 0)
   c.fire2[entity]={epoch=epoch,seq=seq,time=time,shots=shots,groups=groups,at=now,shown=shown,events={},on={false,false},deadline={0,0}}
   return true
  end
  if seq<=q.seq or time<=q.time or shots[1]<q.shots[1] or shots[2]<q.shots[2] then return true end
  local total=shots[1]+shots[2]
  -- Queue only shots beyond the shown total, and nothing while the local game is paused.
  if total>q.shown and not A.paused() then
   for k=1,2 do
    if shots[k]>q.shots[k] then q.events[#q.events+1]={time=time,trigger=k,group=groups[k]} end
   end
  end
  q.shown=math.max(q.shown,total)
  while #q.events>32 do table.remove(q.events,1) end
  q.seq,q.time,q.shots,q.groups,q.at=seq,time,shots,groups,now
  return true
 elseif f[1]=='PILOT_DENIED' then
  local seq=#f==5 and integer(f[4]) or nil
  if seq and seq<=(c.crewSeq or 0) and seq>(c.deniedSeq or -1) then
   c.deniedSeq=seq
   local entity=integer(f[5],2)
   if entity and entity>0 and c.entity(C.GetPlayerControlledShipID())==entity and c.pilots[entity]~=cfg.slot then
    AddUITriggeredEvent('X4LAN_Coop','LeaveControls',entity)
    log('pilot request denied: the ship is occupied, trading or destroyed')
   end
  end
  return true
 elseif f[1]=='COOP_EXEC' then
  if #f~=7 or not integer(f[5],2) or tonumber(f[5])<1 then return true end
  f[1]='ORDER_EXEC';f[5]='p'..f[5]
  if A.trade then return A.trade.receive(s.client,f,now) end
  return true
 end
 return false
end
local function render(r,now)
 if not r or #r.samples==0 then return nil end
 if r.track then return M.render(r.track,now) end
 if now-r.at>.75 then return nil end
 local target=now-.1;local a,b=r.samples[1],r.samples[#r.samples]
 for i=2,#r.samples do if r.samples[i].time>=target then b=r.samples[i];break end;a=r.samples[i] end
 local t=b.time>a.time and math.max(0,math.min(1,(target-a.time)/(b.time-a.time))) or 0;local p={}
 for i=1,6 do
  if i<=3 then p[i]=a.pose[i]+(b.pose[i]-a.pose[i])*t
  else p[i]=a.pose[i]+((b.pose[i]-a.pose[i]+math.pi)%(2*math.pi)-math.pi)*t end
 end
 return p
end
-- Play FIRE2 trigger evidence back at the same source time as the rendered NAV2 pose.
local function fire2Frame(i,r,ship,now)
 local q=c.fire2[i]
 if not q then return end
 if now-q.at>.35 or c.owners[i]==cfg.slot or not C.IsComponentOperational(ship) then
  -- Freshness: stop even if motion keeps rendering a frozen prediction.
  stopFire2(i);return
 end
 local target=(r and r.track and r.track.offset) and now-r.track.offset-M.delay or math.huge
 local keep={}
 for _,e in ipairs(q.events) do
  if e.time<=target then
   -- A queued edge older than the motion window is dropped, never fired late.
   if target-e.time<=M.stale then q.deadline[e.trigger]=now+.35;q.group=q.group or {};q.group[e.trigger]=e.group end
  else keep[#keep+1]=e end
 end
 q.events=keep
 local on={q.deadline[1]>now,q.deadline[2]>now}
 local group=q.group or {}
 for k=1,2 do
  if on[k] and group[k] and getGroup and setGroup and getGroup(ship,k==1)~=group[k] then
   setGroup(ship,k==1,group[k])
  end
 end
 if on[1]~=q.on[1] or on[2]~=q.on[2] then
  q.on=on;AddUITriggeredEvent('X4LAN_Coop','Fire2',{ship=i,primary=on[1],secondary=on[2]})
 end
end
local function publishMetrics(now)
 if now>=(c.nextMetric or 0) then
  c.nextMetric=now+1;c.metricSeq=c.metricSeq+1;local m=c.metrics
  local object=C.GetPlayerContainerID();local sector=object~=0 and C.GetContextByClass(object,'sector',false) or 0
  local name=sector~=0 and GetComponentData(A.id(sector),'macro') or 'unknown'
  local mode=A.paused() and 'paused' or (A.life and A.life.mode or 'waiting')
  send(string.format('PERF|1|%s|%d|%.2f|%.2f|%.2f|%s|%s',s.client.lease,c.metricSeq,m.sum>0 and m.n/m.sum or 0,m.n>0 and 1000*m.sum/m.n or 0,m.max*1000,mode,name))
  c.metrics={n=0,sum=0,max=0}
 end
end
function c.frame(now)
 if not c.ready or not s.client or not s.client.lease then return end
 local controlled=c.entity(C.GetPlayerControlledShipID());local aboard=c.entity(C.GetPlayerContainerID());c.controlled=controlled
 if now>=(c.nextCrew or 0) then
  c.nextCrew=now+.1;c.crewSeq=c.crewSeq+1
  send('CREW|1|'..s.client.lease..'|'..c.crewSeq..'|'..controlled..'|'..aboard)
 end
 publishMetrics(now)
 if A.paused() then
  -- FIRE2 never resumes from pre-pause history: stop active fire and drop queued edges.
  for i in pairs(c.fire2) do stopFire2(i) end
  return
 end
 if now>=(c.nextNav or 0) then
  c.nextNav=now+.05
  for i=1,2 do
   local ship=c.ships[i]
   if c.owners[i]==cfg.slot and C.IsComponentOperational(ship) then
    local sector=C.GetContextByClass(ship,'sector',false)
    if sector~=0 then
     local name=GetComponentData(A.id(sector),'macro');c.sectors[name]=sector
     local p=C.GetObjectPositionInSector(ship);c.seq[i]=c.seq[i]+1
     local fields={'NAV2','1',s.client.lease,tostring(i),tostring(c.epochs[i]),tostring(c.seq[i]),name}
     for _,value in ipairs({p.x,p.y,p.z,p.yaw,p.pitch,p.roll}) do fields[#fields+1]=string.format('%.9g',value) end
     fields[#fields+1]=tostring(c.docks[i]);fields[#fields+1]=tostring(c.shots[i])
     local values={p.x,p.y,p.z,p.yaw,p.pitch,p.roll};c.motion=c.motion or {}
     local velocity=M.velocity(c.motion[i],values,now);c.motion[i]={time=now,pose=values}
     fields[#fields+1]=string.format('%.6f',now)
     for _,v in ipairs(velocity) do fields[#fields+1]=string.format('%.9g',v) end
     send(table.concat(fields,'|'))
     -- FIRE2 shares NAV2's source time; a group outside 1-4 is never encoded.
     local primary,secondary=getGroup and getGroup(ship,true) or 0,getGroup and getGroup(ship,false) or 0
     if primary>=1 and primary<=4 and secondary>=1 and secondary<=4 then
      c.fire2Seq[i]=c.fire2Seq[i]+1;local shots=c.triggerShots[i]
      send(string.format('FIRE2|1|%s|%d|%d|%d|%.6f|%d|%d|%d|%d',s.client.lease,i,c.epochs[i],c.fire2Seq[i],now,primary,secondary,shots[1],shots[2]))
     end
    end
   end
  end
 end
 for i=1,2 do
  local r=c.remote[i];local ship=c.ships[i];local p=render(r,now)
  fire2Frame(i,r,ship,now)
  if r and r.firing and (not p or now>(r.fireDeadline or 0)) then r.firing=false;AddUITriggeredEvent('X4LAN_Coop','Fire',{ship=i,on=false}) end
  if p and c.owners[i]~=cfg.slot and C.IsComponentOperational(ship) then
   if (r.fireDeadline or 0)>now and not r.firing then r.firing=true;AddUITriggeredEvent('X4LAN_Coop','Fire',{ship=i,on=true}) end
   local sector=c.sectorId(r.sector)
   if c.docks[i]~=r.dock and sector and now>=(r.nextDock or 0) then
    r.nextDock=now+1
    AddUITriggeredEvent('X4LAN_Coop','Dock',{ship=i,market=r.dock,sector=A.id(sector),x=p[1],y=p[2],z=p[3],yaw=p[4]*180/math.pi,pitch=p[5]*180/math.pi,roll=p[6]*180/math.pi})
   end
   if sector and r.dock==0 and c.docks[i]==0 then
    local value={x=p[1],y=p[2],z=p[3],yaw=p[4]*180/math.pi,pitch=p[5]*180/math.pi,roll=p[6]*180/math.pi}
    if C.GetContextByClass(ship,'sector',false)~=sector then
     if now>=(r.nextWarp or 0) then
      r.nextWarp=now+1;value.ship=i;value.sector=A.id(sector);AddUITriggeredEvent('X4LAN_Coop','SectorMove',value)
     end
    elseif now>=(r.nextApply or 0) then
     r.nextApply=now+.015
     local changed=not r.applied
     if r.applied then for n=1,6 do if math.abs(p[n]-r.applied[n])>(n<=3 and .01 or .0001) then changed=true end end end
     if changed then C.SetObjectSectorPos(ship,sector,ffi.new('UIPosRot',value));r.applied=p end
    end
   end
  end
 end
end
RegisterEvent('X4LAN_Coop.Ready',function() c.ready=true;log('cooperative navigation ready') end)
RegisterEvent('X4LAN_Coop.Docks',function(_,payload)
 local a,b=payload:match('^(%d)|(%d)$');if a and tonumber(a)<=2 and tonumber(b)<=2 then c.docks={tonumber(a),tonumber(b)} end
end)
RegisterEvent('X4LAN_Coop.Shot',function(_,payload)
 -- Payload 'n' (legacy) or 'n|p' / 'n|s': the shot came from the active primary or secondary group.
 local entity,trigger=tostring(payload):match('^([12])|?([ps]?)$');local i=tonumber(entity)
 if i and c.owners[i]==cfg.slot then
  c.shots[i]=c.shots[i]+1
  local k=trigger=='s' and 2 or 1;c.triggerShots[i][k]=c.triggerShots[i][k]+1
 end
end)
