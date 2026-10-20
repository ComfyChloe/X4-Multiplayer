-- Unshipped damage-sync probe (D-task step 1). Never packaged; a native test harness copies it
-- into ui/ after the production adapters, together with dev/damage_probe.xml in md/.
-- Each DAMAGE_PROBE line records, on this PC, the local hull/shield of both co-op ships and
-- both LAN targets plus the fire state. Lines are written on every local shot (detected per
-- frame from the shot counters, so several shots in one frame share a line), on every
-- replicated fire edge (NAV Fire or FIRE2), on every guest world Apply and at 5 Hz otherwise.
-- Compare host and guest logs by game time and shot sequence; this probe changes nothing.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.coop or not cfg then return end
local ffi=require('ffi');local C=ffi.C;local c=A.coop
local probe={seq=0,targets={},next=0};A.damageProbe=probe

local function state(object)
 if not object or object==0 then return 'none' end
 local ok,alive=pcall(C.IsComponentOperational,object)
 if not ok or not alive then return 'dead' end
 local hull,shield=GetComponentData(A.id(object),'hullpercent','shieldpercent')
 return string.format('%.2f/%.2f',tonumber(hull) or -1,tonumber(shield) or -1)
end
local function target(component)
 if not component then return 'none' end
 local hull,shield=GetComponentData(component,'hullpercent','shieldpercent')
 if hull==nil then return 'dead' end
 return string.format('%.2f/%.2f',tonumber(hull) or -1,tonumber(shield) or -1)
end
local function fire2(i)
 local q=c.fire2 and c.fire2[i]
 if not q then return '-' end
 return string.format('%s%s:%d,%d',q.on and q.on[1] and 'P' or 'p',q.on and q.on[2] and 'S' or 's',q.shots and q.shots[1] or -1,q.shots and q.shots[2] or -1)
end
local function navFire(i)
 local r=c.remote and c.remote[i]
 return r and (r.firing and 'F' or 'f')..tostring(r.fire or 0) or '-'
end
function probe.log(reason,detail)
 probe.seq=probe.seq+1
 local trigger=c.triggerShots or {{-1,-1},{-1,-1}}
 DebugError(string.format('[X4LAN] DAMAGE_PROBE seq=%d t=%.3f slot=%s reason=%s detail=%s paused=%s '..
  'ship1=%s ship2=%s target1=%s target2=%s shots=%d,%d trigger1=%d,%d trigger2=%d,%d nav=%s,%s fire2=%s,%s mode=%s',
  probe.seq,GetCurRealTime(),cfg.slot,reason,tostring(detail or '-'),tostring(A.paused()),
  state(c.ships[1]),state(c.ships[2]),target(probe.targets[1]),target(probe.targets[2]),
  c.shots[1] or -1,c.shots[2] or -1,trigger[1][1],trigger[1][2],trigger[2][1],trigger[2][2],
  navFire(1),navFire(2),fire2(1),fire2(2),cfg.fire2==true and 'fire2' or 'legacy'))
end

-- Fire edges and guest world applies are observed where they leave Lua, without changing them.
local trigger=AddUITriggeredEvent
AddUITriggeredEvent=function(screen,control,value)
 trigger(screen,control,value)
 if screen=='X4LAN_Coop' and (control=='Fire' or control=='Fire2') and type(value)=='table' then
  local edge=control=='Fire' and tostring(value.on) or (tostring(value.primary)..'/'..tostring(value.secondary))
  probe.log('edge-'..control,tostring(value.ship)..':'..edge)
 elseif screen=='X4LAN_Bubble' and control=='Apply' then
  probe.log('apply')
 end
end
RegisterEvent('X4LAN_DamageProbe.Targets',function(_,payload)
 if type(payload)=='table' then probe.targets={payload[1],payload[2]};probe.log('targets') end
end)
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
 checkShots()
 frame(now)
 if now>=probe.next and c.ready then probe.next=now+.2;probe.log('tick') end
end
DebugError('[X4LAN] DAMAGE_PROBE armed slot='..tostring(cfg.slot)..' (unshipped dev probe)')
