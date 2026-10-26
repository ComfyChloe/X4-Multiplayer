-- INTERNAL npc_combat_intent: replicate NPC combat INTENT, not shots (target architecture sec. 6).
-- Host: for NPCs relevant to a shared ship (within weapons range of one, targeting one, or being
-- hit by the guest), MD (x4lan_npc_intent.xml) reports the current attack target, recent weapon
-- fire per weapon kind and a manoeuvre hint. Lua publishes on change plus a periodic refresh:
--  NINTENT|1|lease|hostEpoch|id|gen|seq|sector|kind|ref|mask|hint
--   kind 0 none | P shared ship slot (ref 1/2) | N tracked NPC network id (ref) | X other (ref 0)
--   mask 1 guns, 2 missile launchers, 4 turrets (fired within the last 0.6 s); hint 0 idle,
--   1 engaging, 2 travel drive. gen is the host's tracking generation of that id.
-- Guest: replicas keep AI disabled and act as puppets: they face a replicated P/N target and fire
-- their own guns locally while 'firing' is latched. A lost packet only delays a state change: the
-- latch holds until a newer record or LATCH seconds without any record (timeout).
-- Hits on a player ship stay with the victim's machine through the existing HEALTH generation
-- (health_game.lua); this module never writes hull or shield. Peers without the feature ignore
-- NINTENT (old clients) or never send it (old hosts/relays); see FEATURE lines below.
local cfg=X4LAN_CONFIG;local A=X4LAN_BUBBLE_API
local function log(v) DebugError('[X4LAN] NPC_INTENT '..v) end
local function feature(reason) DebugError('[X4LAN] FEATURE npc_combat_intent DISABLED reason='..reason) end
if not cfg or not cfg.enabled then return end
if not cfg.npc_combat_intent then feature('flag_off');return end
if not cfg.world_sync or cfg.world_mode~='replica' or not A or not A.state or not A.state.active or not A.universe or not A.coop then
 feature('requires_world_sync_replica');return
end
local ffi=require('ffi');local C=ffi.C
local U,c,s,P=A.universe,A.coop,A.state,X4LAN_UNIVERSE_PROTOCOL
local K={select=.25,range=6000,keep=2,max=24,visit=256,refresh=.5,idle=2,latch=1.5,reassert=2,aimRange=8000,
 silent=60,calibrate=20}
local n={tracked={},nextGen=0,seq=0,last={},intents={},firing={},stats={},pitchSign=1,cal={yaw=0,yawBad=0,up=0,down=0}}
A.npcIntent=n;n.K=K
local function count(k,v) n.stats[k]=(n.stats[k] or 0)+(v or 1) end
local function integer(v,max) local i=P.integer(tostring(v));return i and i<=(max or 2147483647) and i end
local function send(fields) s.transport:send(table.concat(fields,'|')) end
local function call(action,args) AddUITriggeredEvent('X4LAN_NpcIntent',action,args) end
local function key(object) return tostring(object):gsub('ULL$','') end
local function sectorOf(object) return C.GetContextByClass(object,'sector',false) end
local function pos(object) local p=C.GetObjectPositionInSector(object);return p.x,p.y,p.z end
function n.fail(reason)
 if n.failed then return end
 n.failed=true;log('STOPPED '..tostring(reason));feature('adapter_stopped')
 pcall(call,'Stop',{})
end

-- HOST -----------------------------------------------------------------------------------------
local function hitByGuest()
 local ids={};local pending=A.npcCombat and A.npcCombat.pending
 if pending then for _,p in pairs(pending) do ids[p.id]=true end end
 return ids
end
local function selectRelevant(now)
 local list=U.publish or {};local total=#list;if total==0 then return end
 local ships={}
 for e=1,2 do
  local ship=c.ships[e]
  if ship and C.IsComponentOperational(ship) then ships[#ships+1]={object=ship,sector=sectorOf(ship),pos={pos(ship)}} end
 end
 local guest=hitByGuest()
 n.cursor=n.cursor or 0
 for _=1,math.min(total,K.visit) do
  n.cursor=n.cursor%total+1
  local entry=list[n.cursor];local object=entry.object
  if C.IsComponentOperational(object) then
   local here=sectorOf(object);local best=math.huge
   for _,ship in ipairs(ships) do
    if ship.sector==here then
     local x,y,z=pos(object)
     best=math.min(best,math.sqrt((x-ship.pos[1])^2+(y-ship.pos[2])^2+(z-ship.pos[3])^2))
    end
   end
   local last=n.last[entry.id]
   local engaged=last and last.kind=='P'
   if best<=K.range or engaged or guest[entry.id] then
    local t=n.tracked[entry.id]
    if t and t.object~=object then t=nil end
    if not t then
     if #n.order<K.max then
      n.nextGen=n.nextGen+1
      t={id=entry.id,object=object,sector=entry.sector,gen=n.nextGen}
      n.tracked[entry.id]=t;n.order[#n.order+1]=entry.id
      call('Track',{id=entry.id,object=A.id(object)});count('tracked_new')
     else count('track_full') end
    end
    if t then t.until_=now+K.keep;t.distance=best end
   end
  end
 end
end
local function prune(now)
 local keep={}
 for _,id in ipairs(n.order) do
  local t=n.tracked[id]
  if t and now<t.until_ and C.IsComponentOperational(t.object) and t.sector==U.wanted then keep[#keep+1]=id
  else
   if t then call('Untrack',{id=id}) end
   n.tracked[id]=nil;n.last[id]=nil
  end
 end
 n.order=keep
end
local function publish(t,state,now)
 n.seq=n.seq+1
 send({'NINTENT','1',s.client.lease,s.client.lease,t.id,t.gen,n.seq,t.sector,state.kind,state.ref,state.mask,state.hint})
 state.sent=now;count('published')
 if state.mask>0 then count('pub_firing') end
end
-- MD 'id:kind:ref:mask:hint;...' every 0.1 s while unpaused.
function n.sample(payload,now)
 if n.failed or cfg.slot~='p1' or not s.client or not s.client.lease then return end
 count('samples')
 for id,kind,ref,mask,hint in tostring(payload):gmatch('(%d+):([0PNX]):(%d+):(%d):(%d);') do
  id,ref,mask,hint=tonumber(id),tonumber(ref),tonumber(mask),tonumber(hint)
  local t=n.tracked[id]
  if t and mask<=7 and hint<=2 then
   local old=n.last[id]
   local state={kind=kind,ref=ref,mask=mask,hint=hint}
   if kind=='P' then count('target_p'..ref) end
   if not old or old.kind~=kind or old.ref~=ref or old.mask~=mask or old.hint~=hint then
    publish(t,state,now);n.last[id]=state
   elseif now-(old.sent or 0)>=((mask>0 or kind~='0') and K.refresh or K.idle) then
    publish(t,old,now)
   end
  end
 end
end
local function hostFrame(now)
 if now>=(n.nextSelect or 0) then
  n.nextSelect=now+K.select
  n.order=n.order or {}
  prune(now);selectRelevant(now)
 end
end

-- GUEST ----------------------------------------------------------------------------------------
function n.receive(f,now)
 if f[1]~='NINTENT' then return false end
 if n.failed or cfg.slot~='p2' or #f~=12 or not s.client or f[3]~=s.client.lease or not U.latest or f[4]~=U.latest.epoch then return true end
 local id,gen,seq,ref,mask,hint=integer(f[5]),integer(f[6]),integer(f[7]),integer(f[10]),integer(f[11],7),integer(f[12],2)
 local r=id and U.replicas[id]
 if not r or not gen or gen==0 or not seq or not ref or not mask or not hint or f[8]~=r.sector or r.entity.kind~='ship' then return true end
 local kind=f[9]
 if not (kind=='0' and ref==0 or kind=='X' and ref==0 or kind=='P' and (ref==1 or ref==2) or kind=='N' and ref>0) then return true end
 local old=n.intents[id]
 if old and (gen<old.gen or gen==old.gen and seq<=old.seq) then return true end
 n.intents[id]={gen=gen,seq=seq,kind=kind,ref=ref,mask=mask,hint=hint,at=now}
 n.lastIntent=now;count('received')
 if n.silentLogged then n.silentLogged=nil;log('FEATURE_RESUMED npc_combat_intent') end
 return true
end
local function latched(i,now) return i and now-i.at<K.latch end
local function targetObject(i)
 if i.kind=='P' then return c.ships[i.ref] end
 if i.kind=='N' then local t=U.replicas[i.ref];return t and t.object end
end
-- Pose override used by universe_game.lua's replica renderer: face a replicated P/N target.
-- Orientation in radians; yaw=atan2(dx,dz); pitch sign is calibrated from host motion samples.
function n.aim(r,p,now)
 if n.failed or not r.object then return p end
 local i=n.intents[r.entity.id]
 if not latched(i,now) or i.hint==2 or (i.kind~='P' and i.kind~='N') then return p end
 local target=targetObject(i)
 if not target or target==r.object or not C.IsComponentOperational(target) or sectorOf(target)~=sectorOf(r.object) then return p end
 local x,y,z=pos(target);local dx,dy,dz=x-p[1],y-p[2],z-p[3]
 local flat=math.sqrt(dx*dx+dz*dz);local d=math.sqrt(flat*flat+dy*dy)
 if d<1 or d>K.aimRange then return p end
 count('aimed')
 return {p[1],p[2],p[3],math.atan2(dx,dz),n.pitchSign*math.atan2(dy,flat),p[6]}
end
-- Calibration of the pitch convention from host UMOVE: ships mostly fly nose first.
local function calibrate(f)
 local vx,vy,vz=tonumber(f[15]),tonumber(f[16]),tonumber(f[17]);local yaw,pitch=tonumber(f[12]),tonumber(f[13])
 if not vx or not vy or not vz or not yaw or not pitch then return end
 local speed=math.sqrt(vx*vx+vy*vy+vz*vz);if speed<40 then return end
 local cal=n.cal
 local dyaw=math.abs((math.atan2(vx,vz)-yaw+math.pi)%(2*math.pi)-math.pi)
 if dyaw<math.rad(30) then cal.yaw=cal.yaw+1 else cal.yawBad=cal.yawBad+1 end
 if math.abs(vy)/speed>.25 and math.abs(pitch)>math.rad(5) then
  if (vy>0)==(pitch>0) then cal.up=cal.up+1 else cal.down=cal.down+1 end
  local sign=n.pitchSign
  if cal.down>=K.calibrate and cal.down>4*cal.up then sign=-1 elseif cal.up>=K.calibrate and cal.up>4*cal.down then sign=1 end
  if sign~=n.pitchSign then n.pitchSign=sign;log('PITCH_SIGN '..sign..' agree='..cal.up..' disagree='..cal.down) end
 end
end
local function fire(r,i,now)
 local want=latched(i,now) and i.kind=='P' and i.hint~=2 and i.mask or 0
 local guns,turrets=want%2==1,want%8>=4
 local state=n.firing[r.entity.id]
 if state and state.object~=r.object then n.firing[r.entity.id]=nil;state=nil end
 if not r.object or not C.IsComponentOperational(r.object) then n.firing[r.entity.id]=nil;return end
 local on=guns or turrets
 if not state and not on then return end
 if state and state.guns==guns and state.turrets==turrets and (not on or now<state.next) then return end
 call('Fire',{id=r.entity.id,object=A.id(r.object),guns=guns,turrets=turrets})
 if on then
  if not state or not (state.guns or state.turrets) then count('fire_on') end
  n.firing[r.entity.id]={object=r.object,guns=guns,turrets=turrets,next=now+K.reassert}
 else
  count('fire_off');n.firing[r.entity.id]=nil
  if i and i.mask>0 and not latched(i,now) then count('timeouts') end
 end
end
local function guestFrame(now)
 if A.paused() then
  for id,state in pairs(n.firing) do
   if C.IsComponentOperational(state.object) then call('Fire',{id=id,object=A.id(state.object),guns=false,turrets=false}) end
  end
  n.firing={};return
 end
 for id in pairs(n.intents) do if not U.replicas[id] then n.intents[id]=nil end end
 for id,state in pairs(n.firing) do
  local r=U.replicas[id]
  if not r or r.object~=state.object then
   if C.IsComponentOperational(state.object) then call('Fire',{id=id,object=A.id(state.object),guns=false,turrets=false}) end
   n.firing[id]=nil
  end
 end
 local latch=0
 for id,i in pairs(n.intents) do
  local r=U.replicas[id]
  if r then fire(r,i,now);if latched(i,now) and i.mask>0 and i.kind=='P' then latch=latch+1 end end
 end
 n.latched=latch
 if U.latest and not n.lastIntent and not n.silentLogged and now-(n.firstBaseline or now)>=K.silent then
  n.silentLogged=true;feature('no_host_intents host_or_relay_without_feature')
 end
 n.firstBaseline=n.firstBaseline or (U.latest and now)
end

-- COMMON ---------------------------------------------------------------------------------------
function n.hit(payload)
 local e,id,auth,hull,shield=tostring(payload):match('^(%d)|(%d+)|(%a+)|([^|]*)|([^|]*)$')
 if not e then return end
 count('hits_'..auth)
 if id~='0' then count('hits_tracked') end
end
local function diag(now)
 if now<(n.nextDiag or 0) then return end
 local any=false;for _ in pairs(n.stats) do any=true;break end
 if not any and now<(n.nextQuiet or 0) then n.nextDiag=now+1;return end
 n.nextDiag=now+1;n.nextQuiet=now+30
 local out={'DIAG role='..(cfg.slot=='p1' and 'host' or 'guest')}
 if cfg.slot=='p1' then out[#out+1]='tracked='..#(n.order or {})
 else
  local count=0;for _ in pairs(n.intents) do count=count+1 end
  out[#out+1]='intents='..count;out[#out+1]='latched_firing='..(n.latched or 0)
  out[#out+1]='pitch_sign='..n.pitchSign..' cal='..n.cal.up..'/'..n.cal.down..' yaw_ok='..n.cal.yaw..'/'..(n.cal.yaw+n.cal.yawBad)
 end
 local names={};for k in pairs(n.stats) do names[#names+1]=k end;table.sort(names)
 for _,k in ipairs(names) do out[#out+1]=k..'='..n.stats[k] end
 log(table.concat(out,' '));n.stats={}
end
function n.frame(now)
 if n.failed then return end
 if U.failed then n.fail('universe adapter stopped');return end
 if not s.client or not s.client.lease or not c.ready then return end
 if not n.setup then n.setup=true;call('Setup',{});return end
 if not n.ready then return end
 if cfg.slot=='p1' then hostFrame(now) else guestFrame(now) end
 diag(now)
end
local receive,frame=U.receive,U.frame
function U.receive(f,now)
 if f[1]=='UMOVE' and #f==20 and cfg.slot=='p2' and not n.failed then pcall(calibrate,f) end
 local ok,handled=pcall(n.receive,f,now)
 if not ok then n.fail(handled);handled=f[1]=='NINTENT' end
 if handled then return true end
 return receive(f,now)
end
function U.frame(now)
 frame(now)
 local ok,err=pcall(n.frame,now);if not ok then n.fail(err) end
end
RegisterEvent('X4LAN_NpcIntent.Ready',function()
 n.ready=true
 log('READY slot='..cfg.slot..' hits='..(cfg.pilot_health and 'victim_pilot' or 'host_world'))
end)
RegisterEvent('X4LAN_NpcIntent.Sample',function(_,p) local ok,err=pcall(n.sample,p,GetCurRealTime());if not ok then n.fail(err) end end)
RegisterEvent('X4LAN_NpcIntent.Hit',function(_,p) pcall(n.hit,p) end)
