-- Opt-in cadence probe for an explicitly isolated development installation.
-- X4LAN_FIXEDTICK_MODE='fixed60' forces dt=1/60 (Stage 1); 'fixed60catchup' schedules
-- k/60 steps with k Jolt sub-steps so game time tracks wall time (Stage 2).
-- 'profile' measures like the default probe and samples the MoveWorker stack (simprof):
-- X4LAN_SIMPROF_INTERVAL_MS (0.5..10, default 1), X4LAN_SIMPROF_DEPTH (8..64, default 32),
-- X4LAN_SIMPROF_REPORT_S (default 10), X4LAN_SIMPROF_FOLD_LINES (folded stacks logged per frame, default 20).
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
-- X4LAN_AIDRIVER='census' arms the AI phase census (aidriver.h) once the probe is armed and logs an
-- AIPROF line each second. 'normal' / 'rounds' / 'compare' run the stage-1 driver (our AIDirector::Move,
-- aidriver_move.h) and add an AIDRIVE line; a refused or failed driver mode falls back to the census
-- with a loud FEATURE aidriver DISABLED line. Opt-in; absent = no AI hooks.
local aidriver=false
local aiRefusedLogged=false
local aiDriverRefusedLogged=false
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
 if aidriver and owner.aistats then
  local a=owner:aistats()
  if (a.installed or 0)==1 then
   DebugError(string.format('[X4LAN] AIPROF mode=%s frames=%d ms=%.3f maxMs=%.3f share=%.3f runs=%.2f irqRuns=%.2f passes=%.2f maxPasses=%d loopFrames=%d',
    a.mode,a.frames or 0,a.ms or 0,a.max_ms or 0,a.share or 0,a.runs or 0,a.irq_runs or 0,a.passes or 0,a.max_passes or 0,a.loop_frames or 0)..
    string.format(' extWakes=%.2f timedDue=%.2f wakeOwn=%.2f wakeForeign=%.2f evOwn=%.2f evForeign=%.2f evCue=%.2f evDirector=%.2f hazard=%.3f keyless=%.2f',
    a.ext_wakes or 0,a.timed_due or 0,a.wake_own or 0,a.wake_foreign or 0,a.ev_own or 0,a.ev_foreign or 0,a.ev_cue or 0,a.ev_director or 0,a.hazard or 0,a.keyless_runs or 0)..
    string.format(' sectors=%.2f secSumMs=%.3f secMaxMs=%.3f serialMs=%.3f speedup=%.2f maxShare=%.3f fleets=%.2f fleetSpeedup=%.2f dropped=%d',
    a.sectors or 0,a.sector_sum_ms or 0,a.sector_max_ms or 0,a.serial_ms or 0,a.speedup or 0,a.max_share or 0,a.fleets or 0,a.fleet_speedup or 0,a.dropped or 0)..
    string.format(' fleetResolver=%d fleetMaxShare=%.3f wakeFleetSame=%.2f evFleetSame=%.3f evPendingForeign=%.3f',
    a.fleet_resolver or 0,a.fleet_max_share or 0,a.wake_fleet_same or 0,a.ev_fleet_same or 0,a.ev_pending_foreign or 0)..
    string.format(' attRestarts=%.3f removed=%.3f callerWakes=%.3f globalApplied=%.3f',
    a.attention_restarts or 0,a.removed or 0,a.caller_wakes or 0,a.global_applied or 0))
   DebugError('[X4LAN] AIPROF scripts '..(a.scripts or ''))
   DebugError('[X4LAN] AIPROF sectors '..(a.top_sectors or ''))
   if a.mode~='census' or (a.driver_frames or 0)>0 then
    DebugError(string.format('[X4LAN] AIDRIVE mode=%s driverFrames=%d preflightRefusals=%d buckets=%.2f deferred=%.2f evbuf=%d cmpClean=%d cmpDirty=%d cmpEventPending=%.3f cmpEventRan=%.3f cmpNullLost=%.3f cmpWakeAbsorb=%.3f cmpPostCancel=%.3f cmpUnchecked=%d',
     a.mode,a.driver_frames or 0,a.preflight_refusals or 0,a.buckets_run or 0,a.deferred or 0,a.evbuf or 0,
     a.cmp_passes_clean or 0,a.cmp_passes_dirty or 0,a.cmp_event_pending or 0,a.cmp_event_ran or 0,
     a.cmp_null_lost or 0,a.cmp_wake_absorb or 0,a.cmp_post_cancel or 0,a.cmp_unchecked or 0)..
     string.format(' globalApplied=%.2f attentionRestarts=%.2f removed=%.2f badCaller=%d loopLogs=%d evPendingForeign=%.3f hash=%s',
     a.global_applied or 0,a.attention_restarts or 0,a.removed or 0,a.bad_caller or 0,a.loop_logs or 0,
     a.ev_pending_foreign or 0,tostring(a.state_hash or '-')))
   end
   if a.driver_reason and not aiDriverRefusedLogged then
    aiDriverRefusedLogged=true
    DebugError('[X4LAN] FEATURE aidriver DISABLED reason='..tostring(a.driver_reason)..' (census continues)')
   end
  elseif (a.refused or 0)==1 and not aiRefusedLogged then
   aiRefusedLogged=true
   DebugError('[X4LAN] FEATURE aidriver DISABLED reason='..tostring(a.reason or 'unknown'))
  end
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
  aidriver=false;aiRefusedLogged=false;aiDriverRefusedLogged=false
  if X4LAN_AIDRIVER and owner.aidriver then
   local okai,res,whyai=pcall(owner.aidriver,owner,X4LAN_AIDRIVER)
   local a=okai and owner:aistats() or {}
   if okai and res and (a.installed or 0)==1 then
    aidriver=true
    DebugError('[X4LAN] FEATURE aidriver ENABLED mode='..tostring(a.mode))
   elseif okai and (a.installed or 0)==1 then
    aidriver=true;aiDriverRefusedLogged=true
    DebugError('[X4LAN] FEATURE aidriver DISABLED reason='..tostring(whyai or 'unknown')..' (census continues)')
   else
    DebugError('[X4LAN] FEATURE aidriver DISABLED reason='..tostring(whyai or 'unknown'))
   end
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
