-- Feature visibility (development rule, ENGINE-INSTRUMENTATION.txt rule 4). Loaded right after
-- config.lua and before every adapter. Each adapter reports its feature once at load:
--   F.declare(name, enabled, reason_if_off)   on/off by configuration ('config' by default)
--   F.gate(name, flag, prereq1, label1, ...)   declare from a config flag plus prerequisites; a set
--                                               flag with a missing prerequisite is auto-disabled
--   F.disable(name, reason)                    the feature refused or switched itself off
--   F.resume(name)                             an auto-disabled feature works again
-- disable() logs exactly one "[X4LAN] FEATURE <name> DISABLED reason=..." line per transition into
-- the auto-disabled state (repeated calls keep the first reason and log nothing). One summary line
--   [X4LAN] FEATURES on=a,b off=c(reason) auto_disabled=d(reason)
-- is logged on the first frame after load and again, at most every F.interval seconds, whenever the
-- set changed. Adapters and their unit tests must work without this file: guard with
-- `local F=X4LAN_FEATURES` ... `if F then ... end`.
local F={features={},dirty=true,lastAt=nil,interval=10,summaries=0}
X4LAN_FEATURES=F

function F.log(v) DebugError('[X4LAN] '..v) end

local function clean(reason)
 local r=tostring(reason==nil and 'unspecified' or reason):gsub('%c+',' '):gsub('^%s+',''):gsub('%s+$','')
 if r=='' then r='unspecified' end
 if #r>160 then r=r:sub(1,157)..'...' end
 return r
end
-- Summary tokens stay whitespace- and comma-free so the line remains machine-readable.
local function token(reason)
 local r=reason:gsub('[%s,]+','_'):gsub('[()]','')
 if #r>60 then r=r:sub(1,57)..'...' end
 return r
end
local function entry(name)
 name=tostring(name)
 local f=F.features[name]
 if not f then f={name=name};F.features[name]=f end
 return f
end
local function set(f,state,reason)
 if f.state~=state or f.reason~=reason then f.state,f.reason=state,reason;F.dirty=true end
end

function F.declare(name,enabled,reason)
 local f=entry(name)
 -- A refusal stays visible until resume(); a later declaration does not hide it.
 if f.state=='auto_disabled' then return false end
 if enabled then set(f,'on',nil) else set(f,'off',clean(reason or 'config')) end
 return enabled and true or false
end

function F.disable(name,reason)
 local f=entry(name)
 if f.state=='auto_disabled' then return false end
 reason=clean(reason)
 set(f,'auto_disabled',reason)
 F.log('FEATURE '..f.name..' DISABLED reason='..reason)
 return true
end

function F.resume(name)
 local f=F.features[tostring(name)]
 if not f or f.state~='auto_disabled' then return false end
 set(f,'on',nil)
 F.log('FEATURE '..f.name..' RESUMED')
 return true
end

-- gate(name, flag, ok1, label1, ok2, label2, ...): a false flag is off by configuration; with the
-- flag set, the first false prerequisite auto-disables the feature with
-- "missing prerequisite <label>". Returns true only when the feature is on.
function F.gate(name,flag,...)
 if not flag then F.declare(name,false,'config');return false end
 for i=1,select('#',...),2 do
  local ok,label=select(i,...)
  if not ok then F.disable(name,'missing prerequisite '..tostring(label));return false end
 end
 return F.declare(name,true)
end

function F.state(name)
 local f=F.features[tostring(name)]
 if f then return f.state,f.reason end
end

function F.summary()
 local names={}
 for name in pairs(F.features) do names[#names+1]=name end
 table.sort(names)
 local lists={on={},off={},auto_disabled={}}
 for _,name in ipairs(names) do
  local f=F.features[name];local list=lists[f.state]
  if list then list[#list+1]=f.reason and name..'('..token(f.reason)..')' or name end
 end
 local function join(t) return #t>0 and table.concat(t,',') or '-' end
 return 'FEATURES on='..join(lists.on)..' off='..join(lists.off)..' auto_disabled='..join(lists.auto_disabled)
end

-- Config keys that are settings or state, not feature flags.
F.settings={host=true,port=true,token=true,build=true,bubble=true,slot=true,mode=true,proxy_macro=true,
 ship_macro=true,ship_loadout=true,ship_weapons=true,test_profile=true,seed_id=true,seed_verified=true,
 seed_content=true,world_mode=true,net_probe_interval=true}
-- Before the first summary: a config flag that no loaded adapter declared (typically because its
-- parent adapter returned early) is still listed. A set flag nobody declared is a silent miss and
-- is auto-disabled loudly; an unset one is off by configuration.
function F.sweep()
 local cfg=X4LAN_CONFIG
 if type(cfg)~='table' then return end
 for key,value in pairs(cfg) do
  if type(key)=='string' and not F.settings[key] and not F.features[key]
   and (type(value)=='boolean' or key:match('_diagnostics$')) then
   if value then F.disable(key,'not declared by any loaded adapter') else F.declare(key,false,'config') end
  end
 end
end

-- Logs the summary on the first call and afterwards only when the set changed and at least
-- F.interval seconds passed since the previous summary (changes in between are coalesced).
function F.tick(now)
 if not F.swept then F.swept=true;F.sweep() end
 if not F.dirty then return false end
 now=now or (GetCurRealTime and GetCurRealTime()) or 0
 if F.lastAt and now-F.lastAt<F.interval then return false end
 F.dirty=false;F.lastAt=now;F.summaries=F.summaries+1
 F.log(F.summary())
 return true
end

-- The master switch is reported here; every adapter reports its own feature flag.
F.declare('enabled',X4LAN_CONFIG and X4LAN_CONFIG.enabled,'config')
-- The first frame runs after every file in ui.xml has loaded, so the first summary is complete.
if SetScript then SetScript('onUpdate',function() pcall(F.tick) end) end
