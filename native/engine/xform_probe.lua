-- View-cache capture and shadow probe (transforms rewrite; test lanes only, never packaged).
-- Loads native/x4lan_xform_64.dll and arms it from this UI callback (MoveWorker, slot 6).
-- X4LAN_XFORM_DIR (required): existing directory for the capture files.
-- X4LAN_XFORM_FIRST (game seconds, default 20), X4LAN_XFORM_EVERY (default 30),
-- X4LAN_XFORM_COUNT (captures, default 6), X4LAN_XFORM_TRACE (trace every Nth capture, 0 = never,
-- default 2), X4LAN_XFORM_PATH (probe DLL path).
-- Shadow: X4LAN_XFORM_SHADOW (check every Nth frame with the Rust model, 0/nil = off),
-- X4LAN_XFORM_SHADOW_FRAMES (stop after this many checked frames, default 2000),
-- X4LAN_XFORM_SHADOW_DLL (default native/x4mp_transform_ffi.dll of this extension).
-- Switch: X4LAN_XFORM_SWITCH (threads of the Rust build that replaces BuildCache, 0/nil = off; same DLL),
-- X4LAN_XFORM_SWITCH_AB (1 = default: plain and shadow frames alternate between the original and the
-- Rust build; 0 = the Rust build on every plain frame), X4LAN_XFORM_SWITCH_FRAMES (stop after this many
-- frames built by Rust, default 5000), X4LAN_XFORM_SWITCH_IMPL (1 = copy + model, 2 = default: exact walk
-- on engine memory).
if not X4LAN_CONFIG or X4LAN_CONFIG.test_profile~='45841' then return end
if X4LAN_XFORM_PROBE then return end
X4LAN_XFORM_PROBE=true
local function disabled(reason) DebugError('[X4LAN] FEATURE xform DISABLED reason='..tostring(reason)) end
if not X4LAN_XFORM_DIR then disabled('X4LAN_XFORM_DIR not set') return end
local init,message=package.loadlib(X4LAN_XFORM_PATH or './extensions/x4_lan_lab/native/x4lan_xform_64.dll','luaopen_x4lan_xform')
if not init then disabled('load failed: '..tostring(message)) return end
local native=init()
if not native then disabled('Lua ABI unavailable') return end
local count=X4LAN_XFORM_COUNT or 6
local shadowEvery=X4LAN_XFORM_SHADOW or 0
local shadowTarget=X4LAN_XFORM_SHADOW_FRAMES or 2000
local switchThreads=X4LAN_XFORM_SWITCH or 0
local switchAB=X4LAN_XFORM_SWITCH_AB or 1
local switchTarget=X4LAN_XFORM_SWITCH_FRAMES or 5000
local switchImpl=X4LAN_XFORM_SWITCH_IMPL or 2
local owner,done
local nextTry,nextLog,nextShadowLog,seen,lastSkip=0,0,0,0,nil
local function shadowLine(s)
 return string.format('[X4LAN] XFORM SHADOW frames=%d bad=%d mismatches=%d refs=%d errors=%d components=%d refreshes=%d meanMs=%.2f maxMs=%.2f first=%s',
  s.shadow_frames,s.shadow_bad_frames,s.shadow_mismatches,s.shadow_refs,s.shadow_errors,s.shadow_components,
  s.shadow_refreshes,s.shadow_ms_mean,s.shadow_ms_max,tostring(s.shadow_first or '-'))
end
local function switchLine(s)
 return string.format('[X4LAN] XFORM SWITCH impl=%d threads=%d ab=%d frames=%d fallbacks=%d components=%d tasks=%d refreshes=%d planUs=%.0f execUs=%.0f planMaxUs=%.0f switchUs=%.0f/%d switchMaxUs=%.0f originalUs=%.0f/%d originalMaxUs=%.0f shadowSwitched=%d shadowSwitchBad=%d refusal=%s',
  s.switch_impl,s.switch_threads,s.switch_alternate,s.switch_frames,s.switch_fallbacks,s.switch_components,s.switch_tasks,s.switch_refreshes,
  s.switch_plan_us_mean,s.switch_exec_us_mean,s.switch_plan_us_max,s.switch_us_mean,s.timed_switch,s.switch_us_max,
  s.original_us_mean,s.timed_original,s.original_us_max,s.shadow_switch_frames,s.shadow_switch_bad_frames,tostring(s.switch_refusal or '-'))
end
SetScript('onUpdate',function()
 if done then return end
 local now=GetCurRealTime()
 if owner then
  if now<nextLog then return end
  nextLog=now+0.5
  local s=owner:stats()
  if not owner:active() then
   if shadowEvery>0 then DebugError(shadowLine(s)) end
   if switchThreads>0 then DebugError(switchLine(s)) end
   disabled(s.reason or 'inactive')
   owner:stop();owner=nil;done=true
   return
  end
  if s.captures>seen then
   seen=s.captures
   DebugError(string.format('[X4LAN] XFORM capture n=%d time=%.3f nodes=%d visits=%d open=%d bytes=%d events=%d captureMs=%.1f writeMs=%.1f file=%s',
    s.captures,s.time,s.nodes,s.visits,s.opens,s.bytes,s.events,s.capture_ms,s.write_ms,tostring(s.file)))
  end
  if s.skip and s.skip~=lastSkip then
   lastSkip=s.skip
   DebugError('[X4LAN] XFORM skip reason='..s.skip..' failures='..s.failures)
  end
  if (shadowEvery>0 or switchThreads>0) and now>=nextShadowLog then
   nextShadowLog=now+5
   if shadowEvery>0 then DebugError(shadowLine(s)) end
   if switchThreads>0 then DebugError(switchLine(s)) end
  end
  local capturesDone=s.captures+s.failures>=count
  local shadowDone=shadowEvery==0 or s.shadow_frames+s.shadow_errors>=shadowTarget
  local switchDone=switchThreads==0 or s.switch_frames+s.switch_fallbacks>=switchTarget
  if capturesDone and shadowDone and switchDone then
   if shadowEvery>0 then DebugError(shadowLine(s)) end
   if switchThreads>0 then DebugError(switchLine(s)) end
   DebugError(string.format('[X4LAN] XFORM done captures=%d failures=%d frames=%d loadingSkips=%d foreignThread=%d shadowFrames=%d shadowBad=%d shadowErrors=%d switchFrames=%d switchFallbacks=%d',
    s.captures,s.failures,s.frames,s.loading_skips,s.foreign_thread,s.shadow_frames,s.shadow_bad_frames,s.shadow_errors,s.switch_frames,s.switch_fallbacks))
   owner:stop();owner=nil;done=true
  end
  return
 end
 if now<nextTry then return end
 nextTry=now+1
 local result,why
 local dll=X4LAN_XFORM_SHADOW_DLL or './extensions/x4_lan_lab/native/x4mp_transform_ffi.dll'
 if switchThreads>0 then
  result,why=native.start(X4LAN_XFORM_DIR,X4LAN_XFORM_FIRST or 20,X4LAN_XFORM_EVERY or 30,count,X4LAN_XFORM_TRACE or 2,
   shadowEvery,dll,switchThreads,switchAB,switchImpl)
 elseif shadowEvery>0 then
  result,why=native.start(X4LAN_XFORM_DIR,X4LAN_XFORM_FIRST or 20,X4LAN_XFORM_EVERY or 30,count,X4LAN_XFORM_TRACE or 2,
   shadowEvery,dll)
 else
  result,why=native.start(X4LAN_XFORM_DIR,X4LAN_XFORM_FIRST or 20,X4LAN_XFORM_EVERY or 30,count,X4LAN_XFORM_TRACE or 2)
 end
 if result then
  owner=result
  DebugError('[X4LAN] XFORM armed call=0x9fe549 captures='..count..' shadowEvery='..shadowEvery..' switchThreads='..switchThreads..' switchAB='..switchAB..' switchImpl='..switchImpl..' dir='..X4LAN_XFORM_DIR)
  return
 end
 if why=='loading' then return end
 disabled(why);done=true
end)
