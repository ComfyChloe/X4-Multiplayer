-- Lane-only combat instrumentation (rust/x4mp-combat, x4mp_combat.dll), enabled only by a lane
-- overlay (dev/combat_scenario/capture_overlay.py), which appends
--   X4MP_COMBATCAP = {mode='both'|'capture'|'shadow'|'switch'|'census', capacity=200000, verify=1}
-- to ui/config.lua.
--   both/capture/shadow/switch: recorder/shadow of X4's turret joint step ('switch': the Rust step
--     is authoritative, every verify-th call checked against the original); output
--     <profile folder>/x4mp-combatcap.txt (capture modes).
--   census: per-frame census of weapon and bullet updates (data-only vtable hooks; capacity =
--     frames kept); output <profile folder>/x4mp-combatcensus.txt.
-- Arms when the combat scenario's measurement window begins (its MD raises the Lua event
-- 'X4MP.CombatScn' with 'begin'), logs a line every 10 s, and when the window ends writes its
-- file (collected by x4lane) and restores the engine. Outside switch mode the original code's
-- results are always the ones the game uses.
if not X4MP_COMBATCAP or X4MP_COMBATCAP_LOADED then return end
X4MP_COMBATCAP_LOADED=true
local cfg=X4MP_COMBATCAP
local ffi=require('ffi')
pcall(ffi.cdef,'const char* GetSaveFolderPath(void);')
local C=ffi.C
local census=cfg.mode=='census'
local tag=census and 'COMBATCENSUS ' or 'COMBATCAP '
local feature=census and 'combatcensus' or 'combatcap'
local function log(text) DebugError('[X4LAN] '..tag..text) end
local init,message=package.loadlib(cfg.dll or './extensions/x4_lan_lab/native/x4mp_combat_64.dll','luaopen_x4mp_combat')
if not init then
 DebugError('[X4LAN] FEATURE '..feature..' DISABLED reason=loadlib '..tostring(message))
 return
end
local native=init()
local switch=cfg.mode=='switch'
local record=not switch and not census and cfg.mode~='shadow'
local shadow=not switch and not census and cfg.mode~='capture'
local verify=cfg.verify or 1
local capacity=cfg.capacity or (census and 20000 or 200000)
local handle,state,nextLog,wanted=nil,'waiting',0,false
local function statsLine(s)
 if census then
  return string.format('frames=%d weaponCalls=%d bulletCalls=%d shots=%d missileShots=%d checks=%d hits=%d fresh=%d sameFrame=%d rngWeapon=%d rngBullet=%d rngFrame=%d overCap=%d unmatched=%d foreign=%d uninitTls=%d dropped=%d%s',
   s.frames,s.weapon_calls,s.bullet_calls,s.shots,s.missile_shots,s.bullet_checks,s.bullet_hits,s.bullet_fresh or 0,s.fresh_same_frame or 0,s.rng_weapon,
   s.rng_bullet,s.rng_frame,s.rng_frame_over_cap,s.rng_unmatched,s.foreign,s.uninitialised_tls,s.dropped_frames,
   s.failed and (' failed='..s.failed) or '')
 end
 return string.format('calls=%d recorded=%d dropped=%d foreign=%d shadowed=%d mismatched=%d skippedMode=%d uninitTls=%d mxcsr=0x%x mxcsrChanges=%d mode=%d%s',
  s.calls,s.recorded,s.dropped,s.foreign,s.shadowed,s.mismatched,s.skipped_mode,s.uninitialised_tls,
  s.mxcsr,s.mxcsr_changes,s.mode,s.failed and (' failed='..s.failed) or '')..
  (switch and string.format(' served=%d verified=%d switchMismatched=%d foreignClass=%d%s',s.served or 0,s.verified or 0,
   s.switch_mismatched or 0,s.foreign_class or 0,s.switch_off and (' switchOff='..s.switch_off) or '') or '')
end
local function outputPath(name)
 if cfg.path then return cfg.path end
 local folder=ffi.string(C.GetSaveFolderPath()):gsub('\\','/'):gsub('/+$',''):gsub('/save$','')
 return folder..'/'..name
end
local function finish(reason)
 if not handle then return end
 local s=handle:stats()
 if s then
  log('final '..statsLine(s))
  if s.first_mismatch then log('first_mismatch '..s.first_mismatch) end
  -- A failure first observed at the end of the window must fail the lane too.
  -- Otherwise the normal stopped/written markers can disguise failed bookkeeping.
  if s.failed then DebugError('[X4LAN] FEATURE '..feature..' DISABLED reason='..s.failed) end
  if s.switch_off then DebugError('[X4LAN] FEATURE ikjoint DISABLED reason='..s.switch_off) end
 end
 if census then
  local lines=handle:summary()
  if lines then for i=1,#lines do log('summary '..lines[i]) end end
 end
 if record or census then
  local path=outputPath(census and 'x4mp-combatcensus.txt' or 'x4mp-combatcap.txt')
  local n,why=handle:write(path)
  if n then log(string.format('written %s=%d path=%s',census and 'frames' or 'records',n,path))
  else log('write failed reason='..tostring(why)) end
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
  local h,why
  if census then h,why=native.census(capacity) else h,why=native.start(capacity,record,shadow,switch,verify) end
  if not h then
   DebugError('[X4LAN] FEATURE '..feature..' DISABLED reason='..tostring(why))
   state='off'
   return
  end
  handle=h
  state='armed'
  nextLog=now+10
  if census then
   DebugError(string.format('[X4LAN] FEATURE combatcensus ENABLED frames=%d',capacity))
  else
   DebugError(string.format('[X4LAN] FEATURE combatcap ENABLED record=%s shadow=%s switch=%s verify=%d capacity=%d',
    tostring(record),tostring(shadow),tostring(switch),verify,capacity))
  end
 elseif state=='armed' and now>=nextLog then
  nextLog=now+10
  local s=handle:stats()
  if s then
   log('stats '..statsLine(s))
   if s.switch_off then DebugError('[X4LAN] FEATURE ikjoint DISABLED reason='..s.switch_off) end
   if s.failed then finish('failed') end
  end
 end
end)
