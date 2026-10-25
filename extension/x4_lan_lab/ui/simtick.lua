-- Native frame driver for supported Windows builds, bundled by the packager.
-- Linux retains the UI driver until its native scheduling ABI is implemented.
local M={}
X4LAN_SIMTICK=M
local owner,native,registered,stopped,reported
local function log(message) DebugError('[X4LAN] SIMTICK '..message) end
function M.stop()
 stopped=true
 if owner then owner:stop();owner=nil end
end
function M.register(callback,cfg)
 assert(not registered,'simtick driver already registered')
 registered=true
 if package.config:sub(1,1)~='\\' then SetScript('onUpdate',callback);return end
 assert(cfg.test_profile=='45841','simtick requires disposable profile 45841')
 assert(package.loadlib,'native Lua modules unavailable')
 if GetUISafeModeOption then assert(GetUISafeModeOption()==false,'disable Protected UI mode') end
 local initialise,message=package.loadlib(X4LAN_SIMTICK_PATH or
  './extensions/x4_lan_lab/native/x4lan_simtick_64.dll','luaopen_x4lan_simtick')
 assert(initialise,message)
 native=initialise()
 assert(native and native.start,'simtick could not resolve the Lua ABI')
 local nextTry=0
 -- onUpdate only bootstraps/observes registration. All network/engine work is
 -- owned by the native slot after registration; never dispatch it twice.
 SetScript('onUpdate',function()
  if stopped then return end
  if owner then
   if not owner:active() then
    M.stop()
    if not reported then reported=true;log('disarmed; restart the adapter to resume') end
   end
   return
  end
  local now=GetCurRealTime()
  if now<nextTry then return end
  nextTry=now+1
  local ok,result,reason=pcall(native.start,callback)
  if ok and result then owner=result;log('armed in MoveWorker slot 7');return end
  if ok and reason=='loading' then return end
  stopped=true
  log('registration refused: '..tostring(ok and reason or result))
 end)
end
