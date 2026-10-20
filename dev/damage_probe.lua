-- Unshipped damage-sync probe (D-task step 1). Never packaged; a native test harness copies it
-- into ui/ after the production adapters, together with dev/damage_probe.xml in md/.
-- Each DAMAGE_PROBE line records, on this PC, the local hull/shield of both co-op ships and
-- both LAN targets plus fire and authority state. Lines are written on every local shot
-- (detected per frame from the shot counters, so several shots in one frame share a line), on
-- every replicated fire edge (NAV Fire or FIRE2), around every guest world Apply and at 5 Hz.
--
-- Correlation fields (no session credentials are logged):
--  run      random marker chosen when this probe loads; a reload or reconnect shows a new run
--  rt / gt  GetCurRealTime() and GetCurTime(): real and game clock of THIS PC (not shared)
--  frame    co-op frame counter of this probe: requests sharing a frame were queued in one update
--  auth     AUTH revision, owners and epochs of both co-op ships
--  wrev     WORLD revision: published by the host (p1), last received by the guest (p2)
--  fire2    per ship: epoch:seq@sourceTime groups=primary/secondary shots=p,s and trigger state
-- Apply is asynchronous. 'apply_requested' is only the dispatch to MD and shows the requested
-- values beside the still unchanged local values. 'apply_readback' is MD's later native readback
-- (dev/damage_probe.xml), tagged with its request number and echoing the requested values,
-- plus 'md=seen/read': MD's game clock in microseconds when it handled the Apply and when it read
-- back (one value per MD update while unpaused; opaque here, compared only for equality).
-- An echo that differs from that request is 'apply_readback_echo_mismatch'. A request without a readback within
-- probe.completion seconds is reported once as 'apply_missing'. A readback only shows the state
-- at that moment; it is not proof that nothing else changed health in between.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.coop or not cfg then return end
local ffi=require('ffi');local C=ffi.C;local c=A.coop;local s=A.state
local probe={seq=0,frame=0,targets={},next=0,applies=0,pending={},completion=1.5,
 run=string.format('%04x%04x',math.random(0,65535),math.random(0,65535))}
A.damageProbe=probe

local function gameTime() return type(GetCurTime)=='function' and GetCurTime() or -1 end
local function pair(hull,shield) return string.format('%.2f/%.2f',tonumber(hull) or -1,tonumber(shield) or -1) end
local function state(object)
 if not object or object==0 then return 'none' end
 local ok,alive=pcall(C.IsComponentOperational,object)
 if not ok or not alive then return 'dead' end
 return pair(GetComponentData(A.id(object),'hullpercent','shieldpercent'))
end
local function target(component)
 if not component then return 'none' end
 local hull,shield=GetComponentData(component,'hullpercent','shieldpercent')
 if hull==nil then return 'dead' end
 return pair(hull,shield)
end
local function fire2(i)
 local q=c.fire2 and c.fire2[i]
 if not q then return '-' end
 local g=q.groups or {};local n=q.shots or {}
 return string.format('%s:%s@%.6f groups=%s/%s shots=%s,%s on=%s%s',tostring(q.epoch),tostring(q.seq),tonumber(q.time) or -1,
  tostring(g[1]),tostring(g[2]),tostring(n[1]),tostring(n[2]),q.on and q.on[1] and 'P' or 'p',q.on and q.on[2] and 'S' or 's')
end
local function navFire(i)
 local r=c.remote and c.remote[i]
 return r and (r.firing and 'F' or 'f')..tostring(r.fire or 0) or '-'
end
local function worldRevision() return tostring(cfg.slot=='p1' and s.worldRevision or s.remoteRevision) end
function probe.log(reason,detail)
 probe.seq=probe.seq+1
 local trigger=c.triggerShots or {{-1,-1},{-1,-1}}
 local owners=c.owners or {};local epochs=c.epochs or {}
 DebugError(string.format('[X4LAN] DAMAGE_PROBE run=%s seq=%d frame=%d rt=%.3f gt=%.3f slot=%s reason=%s detail=%s paused=%s '..
  'auth=%s:%s/%s,%s/%s wrev=%s ship1=%s ship2=%s target1=%s target2=%s shots=%d,%d trigger1=%d,%d trigger2=%d,%d '..
  'nav=%s,%s fire2_1=[%s] fire2_2=[%s] mode=%s',
  probe.run,probe.seq,probe.frame,GetCurRealTime(),gameTime(),cfg.slot,reason,tostring(detail or '-'),tostring(A.paused()),
  tostring(c.revision),tostring(owners[1]),tostring(epochs[1]),tostring(owners[2]),tostring(epochs[2]),worldRevision(),
  state(c.ships[1]),state(c.ships[2]),target(probe.targets[1]),target(probe.targets[2]),
  c.shots[1] or -1,c.shots[2] or -1,trigger[1][1],trigger[1][2],trigger[2][1],trigger[2][2],
  navFire(1),navFire(2),fire2(1),fire2(2),cfg.fire2==true and 'fire2' or 'legacy'))
end

-- The requested health of one Apply, in WORLD field order: ship hull/shield and target hull/shield.
local function requested(v)
 return string.format('ship1=%s ship2=%s target1=%s target2=%s',pair(v[1],v[2]),pair(v[4],v[5]),pair(v[7],v[8]),pair(v[9],v[10]))
end
-- Same rounding as the MD readback echo, so a readback finds its request.
local function echoKey(a,b,c2,d) return string.format('%.2f|%.2f|%.2f|%.2f',tonumber(a) or -1,tonumber(b) or -1,tonumber(c2) or -1,tonumber(d) or -1) end

-- Fire edges and guest world applies are observed where they leave Lua, without changing them.
local trigger=AddUITriggeredEvent
AddUITriggeredEvent=function(screen,control,value)
 trigger(screen,control,value)
 if screen=='X4LAN_Coop' and (control=='Fire' or control=='Fire2') and type(value)=='table' then
  local edge=control=='Fire' and tostring(value.on) or (tostring(value.primary)..'/'..tostring(value.secondary))
  probe.log('edge-'..control,tostring(value.ship)..':'..edge)
 end
end
-- Apply: a probe-only marker event carrying the request number is queued immediately BEFORE
-- the unchanged production Apply, so MD tags its readback with that number. Identical
-- repeated requests (the guest re-sends unchanged health every second) stay distinguishable.
local observeFire=AddUITriggeredEvent
AddUITriggeredEvent=function(screen,control,value)
 if screen=='X4LAN_Bubble' and control=='Apply' and type(value)=='table' and type(value.values)=='table' then
  local v=value.values
  probe.applies=probe.applies+1
  trigger('X4LAN_DamageProbe','Mark',{id=probe.applies})
  observeFire(screen,control,value)
  probe.pending[#probe.pending+1]={id=probe.applies,rt=GetCurRealTime(),wrev=worldRevision(),key=echoKey(v[1],v[2],v[4],v[5])}
  probe.log('apply_requested',string.format('apply=%d wrev=%s requested[%s]',probe.applies,worldRevision(),requested(v)))
  return
 end
 observeFire(screen,control,value)
end
RegisterEvent('X4LAN_DamageProbe.Targets',function(_,payload)
 if type(payload)=='table' then probe.targets={payload[1],payload[2]};probe.log('targets') end
end)
-- MD readback after the production Apply ran: request number, echoed request values, observed
-- values, MD batch markers. Pairing is by number; the echo must also equal that request.
RegisterEvent('X4LAN_DamageProbe.Readback',function(_,payload)
 local f={};for v in tostring(payload):gmatch('[^|]+') do f[#f+1]=v end
 local id=tonumber(f[1])
 if #f~=11 or not id or not f[10]:match('^%d+$') or not f[11]:match('^%d+$') then
  probe.log('apply_readback_invalid',tostring(payload));return
 end
 local key=echoKey(f[2],f[3],f[4],f[5])
 local values=string.format('requested[ship1=%s ship2=%s] observed[ship1=%s ship2=%s] md=%s/%s',
  pair(f[2],f[3]),pair(f[4],f[5]),pair(f[6],f[7]),pair(f[8],f[9]),f[10],f[11])
 for n,p in ipairs(probe.pending) do
  if p.id==id then
   table.remove(probe.pending,n)
   if p.key~=key then probe.log('apply_readback_echo_mismatch',string.format('apply=%d wrev=%s %s',p.id,p.wrev,values));return end
   probe.log('apply_readback',string.format('apply=%d wrev=%s delay=%.3f %s',p.id,p.wrev,GetCurRealTime()-p.rt,values))
   return
  end
 end
 probe.log('apply_readback_unmatched',string.format('apply=%d %s',id,values))
end)
local function checkPending(now)
 local keep={}
 for _,p in ipairs(probe.pending) do
  if now-p.rt>probe.completion then probe.log('apply_missing',string.format('apply=%d wrev=%s age=%.3f',p.id,p.wrev,now-p.rt))
  else keep[#keep+1]=p end
 end
 probe.pending=keep
end
-- Local shots are detected from the production counters (never by re-registering the
-- production X4LAN_Coop.Shot handler, which a second registration could replace).
local function shotKey() return tostring(c.shots[1])..','..tostring(c.shots[2]) end
local lastShots=shotKey()
local function checkShots()
 local current=shotKey()
 if current~=lastShots then lastShots=current;probe.log('shot',current) end
end
local frame=c.frame
function c.frame(now)
 probe.frame=probe.frame+1
 checkShots()
 frame(now)
 checkPending(GetCurRealTime())
 if now>=probe.next and c.ready then probe.next=now+.2;probe.log('tick') end
end
DebugError('[X4LAN] DAMAGE_PROBE armed run='..probe.run..' slot='..tostring(cfg.slot)..' (unshipped dev probe)')
