-- Opt-in cadence probe for an explicitly isolated development installation.
-- X4LAN_FIXEDTICK_MODE='fixed60' forces dt=1/60 (Stage 1); 'fixed60catchup' schedules
-- k/60 steps with k Jolt sub-steps so game time tracks wall time (Stage 2).
-- 'profile' measures like the default probe and samples the MoveWorker stack (simprof):
-- X4LAN_SIMPROF_INTERVAL_MS (0.5..10, default 1), X4LAN_SIMPROF_DEPTH (8..64, default 32),
-- X4LAN_SIMPROF_REPORT_S (default 10), X4LAN_SIMPROF_FOLD_LINES (folded stacks logged per frame, default 20).
-- X4LAN_ECONCAP=true also enables the economy capture (econcap.h): MD raises the Lua event
-- 'X4MP.EconWatch' with a station, and its X4ECON records are logged as '[X4LAN] ECONCAP <record>'.
if not X4LAN_CONFIG or X4LAN_CONFIG.test_profile~='45841' then return end
if X4LAN_FIXEDTICK_PROBE then return end
X4LAN_FIXEDTICK_PROBE=true
local initialise,message=package.loadlib(X4LAN_FIXEDTICK_PATH or
 './extensions/x4_lan_lab/native/x4lan_fixedtick_64.dll','luaopen_x4lan_fixedtick')
assert(initialise,message)
local native=initialise()
local owner,reason
local walkRefusedLogged
local sectorHooks={[0]='pending',[1]='original',[2]='chained',[3]='refused'}
local nextTry=0
local nextLog=0
local profiling,reportEvery,foldLines=false,10,20
local nextProfile=0
local profileDisabled=false
local function logStats(now)
 local s=owner:stats()
 DebugError(string.format('[X4LAN] FRAMEPROBE frames=%d wallDt=%.6f maxWallDt=%.6f gameDt=%.9f realDt=%.9f gameTime=%.6f realTime=%.6f speed=%.3f paused=%d loadingSkips=%d regressions=%d',
  s.frames,s.wall_dt,s.max_wall_dt,s.game_dt,s.real_dt,s.game_time,s.real_time,s.speed,
  s.paused_frames,s.loading_skips,s.game_time_regressions)..
  string.format(' fixed=%d fixedFrames=%d wallElapsed=%.6f gameElapsed=%.6f',
  s.fixed or 0,s.fixed_frames or 0,s.wall_elapsed or 0,s.game_elapsed or 0)..
  string.format(' catchup=%d steps=%d zeroStep=%d multiStep=%d dropped=%d lastSteps=%d acc=%.6f',
  s.catchup or 0,s.steps_total or 0,s.zero_step_frames or 0,s.multi_step_frames or 0,
  s.dropped_ticks or 0,s.last_steps or 0,s.accumulator or 0)..
  string.format(' simHooks=%d simFrames=%d simMeanMs=%.3f simP95Ms=%.2f simMaxMs=%.3f',
  s.sim_hooks or 0,s.sim_win_frames or 0,s.sim_win_mean_ms or 0,s.sim_win_p95_ms or 0,s.sim_win_max_ms or 0))
 if (s.walk_installed or 0)==1 and (s.walk_frames or 0)>0 then
  DebugError(string.format('[X4LAN] WALKPROF frames=%d walkMs=%.3f walkMaxMs=%.3f share=%.3f sectors=%.2f sectorSumMs=%.3f sectorMaxMs=%.3f speedup=%.2f zones=%.1f zoneMs=%.3f ships=%.1f shipMs=%.3f stations=%.1f stationMs=%.3f clusterMs=%.3f sectorHook=%s',
   s.walk_frames,s.walk_mean_ms or 0,s.walk_max_ms or 0,s.walk_share or 0,s.sector_calls or 0,
   s.sector_sum_ms or 0,s.sector_max_ms or 0,s.ideal_speedup or 0,s.zone_calls or 0,s.zone_ms or 0,
   s.ship_calls or 0,s.ship_ms or 0,s.station_calls or 0,s.station_ms or 0,s.cluster_ms or 0,
   sectorHooks[s.walk_sector_hook or 0] or 'unknown'))
 elseif (s.walk_refused or 0)==1 and not walkRefusedLogged then
  walkRefusedLogged=true
  DebugError('[X4LAN] FEATURE walkprof DISABLED reason=vtable or signature mismatch')
 end
 nextLog=now+1
end
local function checkProfileDisabled()
 if profileDisabled then return end
 local s=owner:stats()
 if s.simprof==2 then
  profileDisabled=true
  DebugError('[X4LAN] FEATURE simprof DISABLED reason='..tostring(s.simprof_reason or 'unknown'))
 end
end
local function logProfile(now)
 nextProfile=now+reportEvery
 local p=owner:profile()
 if p then
  DebugError(string.format('[X4LAN] SIMPROF window seconds=%.2f samples=%d rateHz=%.0f depthMean=%.1f suspendUsMean=%.1f suspendUsMax=%.1f walkUsMean=%.1f overheadPct=%.2f',
   p.seconds,p.samples,p.rate_hz,p.depth_mean,p.suspend_us_mean,p.suspend_us_max,p.walk_us_mean,p.overhead_pct)..
   string.format(' truncated=%d scanned=%d lost=%d loading=%d failures=%d skipped=%d droppedFunctions=%d droppedStacks=%d foldPending=%d',
   p.truncated,p.scanned,p.lost,p.loading,p.failures,p.skipped,p.dropped_functions,p.dropped_stacks,p.fold_pending))
  DebugError('[X4LAN] SIMPROF phases '..p.phases)
  DebugError('[X4LAN] SIMPROF self '..p.self)
  DebugError('[X4LAN] SIMPROF incl '..p.incl)
 end
 checkProfileDisabled()
end
local econWatchRegistered
local function econWatch(_,station)
 if not owner or not owner.econ_watch then return end
 local id=ConvertStringTo64Bit(tostring(station))
 if not owner:econ_watch(tonumber(id)) then DebugError('[X4LAN] ECONCAP watch refused '..tostring(id)) end
end
local function drainEcon()
 local lines=owner:econ_lines(400)
 for i=1,#lines do DebugError('[X4LAN] ECONCAP '..lines[i]) end
end
local function drainFolds()
 local lines=owner:folds(foldLines)
 for i=1,#lines do DebugError('[X4LAN] SIMPROF fold '..lines[i]) end
end
SetScript('onUpdate',function()
 local now=GetCurRealTime()
 if owner then
  if not owner:active() then
   DebugError('[X4LAN] FRAMEPROBE disarmed: '..tostring(owner:stats().reason or 'unknown'))
   owner:stop();owner=nil;return
  end
  if now>=nextLog then logStats(now) end
  if profiling then
   if now>=nextProfile then logProfile(now) end
   drainFolds()
  end
  if X4LAN_ECONCAP and owner.econ_lines then drainEcon() end
  return
 end
 if now<nextTry then return end
 nextTry=now+1
 local ok,result,why
 profiling=X4LAN_FIXEDTICK_MODE=='profile'
 reportEvery=X4LAN_SIMPROF_REPORT_S or 10
 foldLines=X4LAN_SIMPROF_FOLD_LINES or 20
 if profiling then ok,result,why=pcall(native.start,'profile',X4LAN_SIMPROF_INTERVAL_MS,X4LAN_SIMPROF_DEPTH)
 elseif X4LAN_FIXEDTICK_MODE then ok,result,why=pcall(native.start,X4LAN_FIXEDTICK_MODE)
 else ok,result,why=pcall(native.start) end
 if not ok then error(result) end
 if result then
  owner=result;nextLog=0
  DebugError('[X4LAN] FRAMEPROBE armed in MoveWorker slot 5 mode='..(X4LAN_FIXEDTICK_MODE or 'probe'))
  if X4LAN_ECONCAP and not econWatchRegistered then
   econWatchRegistered=true
   RegisterEvent('X4MP.EconWatch',econWatch)
   DebugError('[X4LAN] FEATURE econcap ENABLED')
  end
  if profiling then
   profileDisabled=false;nextProfile=now+reportEvery
   local s=owner:stats()
   if s.simprof==1 then
    DebugError(string.format('[X4LAN] FEATURE simprof ENABLED intervalUs=%d depth=%d reportS=%g',
     s.simprof_interval_us or 0,s.simprof_depth or 0,reportEvery))
   else checkProfileDisabled() end
  end
  return
 end
 if why=='loading' then return end
 error('FRAMEPROBE registration refused: '..tostring(why))
end)
