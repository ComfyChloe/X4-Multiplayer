-- Lane-only recorder/shadow of X4's turret joint step (rust/x4mp-combat, x4mp_combat.dll).
-- Enabled only by a lane overlay (dev/combat_scenario/capture_overlay.py), which appends
--   X4MP_COMBATCAP = {mode='both'|'capture'|'shadow', capacity=200000}
-- to ui/config.lua. Arms when the combat scenario's measurement window begins (its MD raises
-- the Lua event 'X4MP.CombatScn' with 'begin'), logs a COMBATCAP line every 10 s, and when the
-- window ends writes <profile folder>/x4mp-combatcap.txt (collected by x4lane) and restores
-- the engine code. The original function's results are always the ones the game uses.
if not X4MP_COMBATCAP or X4MP_COMBATCAP_LOADED then return end
X4MP_COMBATCAP_LOADED=true
local cfg=X4MP_COMBATCAP
local ffi=require('ffi')
pcall(ffi.cdef,'const char* GetSaveFolderPath(void);')
local C=ffi.C
local function log(text) DebugError('[X4LAN] COMBATCAP '..text) end
local init,message=package.loadlib(cfg.dll or './extensions/x4_lan_lab/native/x4mp_combat_64.dll','luaopen_x4mp_combat')
if not init then
 DebugError('[X4LAN] FEATURE combatcap DISABLED reason=loadlib '..tostring(message))
 return
end
local native=init()
local record=cfg.mode~='shadow'
local shadow=cfg.mode~='capture'
local capacity=cfg.capacity or 200000
local handle,state,nextLog,wanted=nil,'waiting',0,false
local function statsLine(s)
 return string.format('calls=%d recorded=%d dropped=%d foreign=%d shadowed=%d mismatched=%d skippedMode=%d uninitTls=%d mxcsr=0x%x mxcsrChanges=%d mode=%d%s',
  s.calls,s.recorded,s.dropped,s.foreign,s.shadowed,s.mismatched,s.skipped_mode,s.uninitialised_tls,
  s.mxcsr,s.mxcsr_changes,s.mode,s.failed and (' failed='..s.failed) or '')
end
local function finish(reason)
 if not handle then return end
 local s=handle:stats()
 if s then
  log('final '..statsLine(s))
  if s.first_mismatch then log('first_mismatch '..s.first_mismatch) end
 end
 if record then
  local path=cfg.path
  if not path then
   local folder=ffi.string(C.GetSaveFolderPath()):gsub('\\','/'):gsub('/+$',''):gsub('/save$','')
   path=folder..'/x4mp-combatcap.txt'
  end
  local n,why=handle:write(path)
  if n then log(string.format('written records=%d path=%s',n,path)) else log('write failed reason='..tostring(why)) end
 end
 local ok,why=handle:stop()
 if ok then log('stopped reason='..reason) else log('stop failed reason='..tostring(why)) end
 handle=nil
 state='done'
end
RegisterEvent('X4MP.CombatScn',function(_,stage)
 if stage=='begin' and state=='waiting' then wanted=true end
 if stage=='end' and state=='armed' then finish('window end') end
end)
SetScript('onUpdate',function()
 local now=GetCurRealTime()
 if state=='waiting' and wanted then
  local h,why=native.start(capacity,record,shadow)
  if not h then
   DebugError('[X4LAN] FEATURE combatcap DISABLED reason='..tostring(why))
   state='off'
   return
  end
  handle=h
  state='armed'
  nextLog=now+10
  DebugError(string.format('[X4LAN] FEATURE combatcap ENABLED record=%s shadow=%s capacity=%d',
   tostring(record),tostring(shadow),capacity))
 elseif state=='armed' and now>=nextLog then
  nextLog=now+10
  local s=handle:stats()
  if s then
   log('stats '..statsLine(s))
   if s.failed then finish('failed') end
  end
 end
end)
