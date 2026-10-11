-- Cooperative navigation: stable ship identities, sector-aware poses, one pilot per ship.
local cfg=X4LAN_CONFIG;local A=X4LAN_BUBBLE_API
if not cfg or not cfg.coop or not A or not A.state.active then return end
local ffi=require('ffi');local C=ffi.C;local s=A.state
local c={ships={},sectors={},resolving={},owners={},epochs={},pilots={},remote={},seq={0,0},shots={0,0},docks={0,0},revision=-1,crewSeq=0,metricSeq=0,metrics={n=0,sum=0,max=0}}
A.coop=c
local function log(v) DebugError('[X4LAN] '..v) end
local function integer(v,limit)
 local n=tonumber(v);if not n or n~=n or n<0 or n%1~=0 or n>(limit or 2147483647) then return nil end;return n
end
local function numeric(v,bound)
 local n=tonumber(v);if not n or n~=n or math.abs(n)>bound then return nil end;return n
end
local function send(p) if s.client and s.client.lease then s.transport:send(p) end end
function c.stopFire()
 for i,r in pairs(c.remote) do
  if r.firing then AddUITriggeredEvent('X4LAN_Coop','Fire',{ship=i,on=false}) end
  r.firing=false;r.fireDeadline=0
 end
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
    c.remote[i]=nil;c.seq[i]=0;c.shots[i]=0
    log('ship '..i..' movement authority='..owners[i]..'; epoch='..epochs[i])
   end
  end
  c.owners,c.epochs,c.pilots,c.revision=owners,epochs,pilots,revision
  return true
 elseif f[1]=='NAV' then
  if #f~=16 then return true end
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
  r.seq,r.at,r.dock=seq,now,dock
  if fire>r.fire then r.fireDeadline=now+.35 end;r.fire=fire
  r.samples[#r.samples+1]={time=now,pose=p};while #r.samples>5 do table.remove(r.samples,1) end
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
 if not r or now-r.at>.75 or #r.samples==0 then return nil end
 local target=now-.1;local a,b=r.samples[1],r.samples[#r.samples]
 for i=2,#r.samples do if r.samples[i].time>=target then b=r.samples[i];break end;a=r.samples[i] end
 local t=b.time>a.time and math.max(0,math.min(1,(target-a.time)/(b.time-a.time))) or 0;local p={}
 for i=1,6 do
  if i<=3 then p[i]=a.pose[i]+(b.pose[i]-a.pose[i])*t
  else p[i]=a.pose[i]+((b.pose[i]-a.pose[i]+math.pi)%(2*math.pi)-math.pi)*t end
 end
 return p
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
 if A.paused() then return end
 if now>=(c.nextNav or 0) then
  c.nextNav=now+.05
  for i=1,2 do
   local ship=c.ships[i]
   if c.owners[i]==cfg.slot and C.IsComponentOperational(ship) then
    local sector=C.GetContextByClass(ship,'sector',false)
    if sector~=0 then
     local name=GetComponentData(A.id(sector),'macro');c.sectors[name]=sector
     local p=C.GetObjectPositionInSector(ship);c.seq[i]=c.seq[i]+1
     local fields={'NAV','1',s.client.lease,tostring(i),tostring(c.epochs[i]),tostring(c.seq[i]),name}
     for _,value in ipairs({p.x,p.y,p.z,p.yaw,p.pitch,p.roll}) do fields[#fields+1]=string.format('%.9g',value) end
     fields[#fields+1]=tostring(c.docks[i]);fields[#fields+1]=tostring(c.shots[i]);send(table.concat(fields,'|'))
    end
   end
  end
 end
 for i=1,2 do
  local r=c.remote[i];local ship=c.ships[i];local p=render(r,now)
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
     r.nextApply=now+.05
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
RegisterEvent('X4LAN_Coop.Shot',function(_,entity)
 local i=tonumber(entity);if i and c.owners[i]==cfg.slot then c.shots[i]=c.shots[i]+1 end
end)
