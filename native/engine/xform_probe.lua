-- View-cache capture probe (transforms rewrite; test lanes only, never packaged).
-- Loads native/x4lan_xform_64.dll and arms it from this UI callback (MoveWorker, slot 6).
-- X4LAN_XFORM_DIR (required): existing directory for the capture files.
-- X4LAN_XFORM_FIRST (game seconds, default 20), X4LAN_XFORM_EVERY (default 30),
-- X4LAN_XFORM_COUNT (captures, default 6), X4LAN_XFORM_TRACE (trace every Nth capture, 0 = never,
-- default 2), X4LAN_XFORM_PATH (DLL path).
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
local owner,done
local nextTry,nextLog,seen,lastSkip=0,0,0,nil
SetScript('onUpdate',function()
 if done then return end
 local now=GetCurRealTime()
 if owner then
  if now<nextLog then return end
  nextLog=now+0.5
  local s=owner:stats()
  if not owner:active() then
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
  if s.captures+s.failures>=count then
   DebugError(string.format('[X4LAN] XFORM done captures=%d failures=%d frames=%d loadingSkips=%d foreignThread=%d',
    s.captures,s.failures,s.frames,s.loading_skips,s.foreign_thread))
   owner:stop();owner=nil;done=true
  end
  return
 end
 if now<nextTry then return end
 nextTry=now+1
 local result,why=native.start(X4LAN_XFORM_DIR,X4LAN_XFORM_FIRST or 20,X4LAN_XFORM_EVERY or 30,count,X4LAN_XFORM_TRACE or 2)
 if result then
  owner=result
  DebugError('[X4LAN] XFORM armed call=0x9fe549 captures='..count..' dir='..X4LAN_XFORM_DIR)
  return
 end
 if why=='loading' then return end
 disabled(why);done=true
end)
