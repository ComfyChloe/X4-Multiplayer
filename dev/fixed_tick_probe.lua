-- Opt-in cadence probe for an explicitly isolated development installation.
-- X4LAN_FIXEDTICK_MODE='fixed60' forces dt=1/60 (Stage 1); 'fixed60catchup' schedules
-- k/60 steps with k Jolt sub-steps so game time tracks wall time (Stage 2).
-- 'profile' measures like the default probe and samples the MoveWorker stack (simprof):
-- X4LAN_SIMPROF_INTERVAL_MS (0.5..10, default 1), X4LAN_SIMPROF_DEPTH (8..64, default 32),
-- X4LAN_SIMPROF_REPORT_S (default 10), X4LAN_SIMPROF_FOLD_LINES (folded stacks logged per frame, default 20).
-- Jolt's jobs run in joltwait mode by default: X4's schedule minus the per-job Sleep(0), identical results
-- (FEATURE joltwait ENABLED once applied; FEATURE joltwait DISABLED reason=... on any refusal, sticky).
-- X4LAN_PHYSICS_MODE='original' keeps X4's own barrier; 'ab' alternates 'original' and 'joltwait' every
-- X4LAN_PHYSICS_AB_S seconds (default 20).
-- X4LAN_FLIGHTPROF=true adds the flight-model census (FLIGHTPROF: Update calls per frame and us per call for
-- each of the six U::Path flight control models, cheap-mode calls; data-only, refusal FEATURE flightprof DISABLED).
-- X4LAN_FLIGHTCAP='forced' records that model's Update inputs/outputs (X4LAN_FLIGHTCAP_N records, default 20000,
-- at most X4LAN_FLIGHTCAP_FRAMES frames, default 3000) into <profile>/x4mp-flightcap-<model>.log for offline replay.
-- Capture waits for a non-loading stats window with at least one second of game time; startup loads UI twice.
if not X4LAN_CONFIG or X4LAN_CONFIG.test_profile~='45841' then return end
if X4LAN_FIXEDTICK_PROBE then return end
X4LAN_FIXEDTICK_PROBE=true
local initialise,message=package.loadlib(X4LAN_FIXEDTICK_PATH or
 './extensions/x4_lan_lab/native/x4lan_fixedtick_64.dll','luaopen_x4lan_fixedtick')
assert(initialise,message)
local native=initialise()
local owner,reason
local walkRefusedLogged
local physRefusedLogged
local joltwaitLogged,joltwaitOnLogged
local flightRequested,flightRefusedLogged
local capRequested,capLogged
local captureReady=false
local physMode,nextPhysMode=nil,0
local sectorHooks={[0]='pending',[1]='original',[2]='chained',[3]='refused'}
local nextTry=0
local nextLog=0
local profiling,reportEvery,foldLines=false,10,20
local nextProfile=0
local profileDisabled=false
local function logStats(now)
 local s=owner:stats()
 captureReady=(s.frames or 0)>0 and (s.loading_skips or 0)==0 and (s.game_time or 0)>=1
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
 -- Physics/flight-job probe (physics_probe.h): Jolt step on the MoveWorker, pathing pass, pool occupancy.
 if (s.phys_installed or 0)==1 and (s.phys_frames or 0)>0 then
  DebugError(string.format('[X4LAN] PHYSPROF frames=%d updates=%.2f joltMs=%.3f jolt0Ms=%.3f jolt1Ms=%.3f joltMaxMs=%.3f jobs=%.1f added=%.1f persisted=%.1f removed=%.1f bodies0=%.1f bodies1=%.1f bodiesMax=%d',
   s.phys_frames,s.phys_updates or 0,s.phys_jolt_ms or 0,s.phys_jolt0_ms or 0,s.phys_jolt1_ms or 0,s.phys_jolt_max_ms or 0,
   s.phys_jobs or 0,s.phys_added or 0,s.phys_persisted or 0,s.phys_removed or 0,s.phys_bodies0 or 0,s.phys_bodies1 or 0,s.phys_bodies_max or 0)..
   string.format(' pairs=%.1f multiPairs=%.2f multiPairsTotal=%d pairOverflow=%d',
   s.phys_pairs or 0,s.phys_multi_pairs or 0,s.phys_multi_pairs_total or 0,s.phys_pair_overflow or 0)..
   string.format(' pathAgents=%d pathBatchesA=%d pathBatchesB=%d pathBudgetMs=%.3f pathDt=%.4f pathInflight=%.3f pathPasses=%d pathPassMs=%.3f pathPassMaxMs=%.3f pathJoins=%d pathTightJoins=%d pathSlackMs=%.3f',
   s.path_agents or 0,s.path_batches_a or -1,s.path_batches_b or -1,s.path_budget_ms or 0,s.path_dt or 0,s.path_inflight or 0,s.path_passes or 0,
   s.path_pass_ms or 0,s.path_pass_max_ms or 0,s.path_joins or 0,s.path_tight_joins or 0,s.path_slack_ms or 0)..
   string.format(' jtmWorkers=%d jtmSamples=%d jtmBusy=%.2f jtmBusyMax=%d jtmCat1=%.2f jtmCat2=%.2f jtmOther=%.2f jtmQueue=%.2f jtmQueueMax=%d joltSamples=%d jtmBusyInJolt=%.2f',
   s.jtm_workers or 0,s.jtm_samples or 0,s.jtm_busy or 0,s.jtm_busy_max or 0,s.jtm_cat1 or 0,s.jtm_cat2 or 0,s.jtm_other or 0,
   s.jtm_queue or 0,s.jtm_queue_max or 0,s.jtm_jolt_samples or 0,s.jtm_busy_in_jolt or 0)..
   string.format(' mode=%d modeFrames=%d joltwaitJobs=%d joltwaitMaxJobs=%d joltwaitAnomalies=%d luaForeign=%d',
   s.phys_mode or 0,s.phys_mode_frames or 0,s.joltwait_jobs or 0,s.joltwait_max_jobs or 0,s.joltwait_anomalies or 0,
   s.phys_lua_foreign or 0))
  if s.joltwait_reason and not joltwaitLogged then
   joltwaitLogged=true
   physMode='refused'
   DebugError('[X4LAN] FEATURE joltwait DISABLED reason='..tostring(s.joltwait_reason))
  elseif (s.phys_mode or 0)==1 and not joltwaitOnLogged then
   joltwaitOnLogged=true
   DebugError('[X4LAN] FEATURE joltwait ENABLED')
  end
  if (s.flight_installed or 0)==1 and (s.flight_frames or 0)>0 then
   local parts={}
   for _,m in ipairs({'forced','highway','linear','masstraffic','spline','steering'}) do
    parts[#parts+1]=string.format('%s=%.1f/%.2f',m,s['flight_'..m..'_calls'] or 0,s['flight_'..m..'_us'] or 0)
   end
   DebugError(string.format('[X4LAN] FLIGHTPROF frames=%d calls=%.1f callsMax=%d cheap=%.2f ms=%.3f ownerCalls=%d ',
    s.flight_frames,s.flight_calls or 0,s.flight_calls_max or 0,s.flight_cheap or 0,s.flight_ms or 0,
    s.flight_owner_calls or 0)..table.concat(parts,' '))
  end
  if (s.flight_cap_written or 0)~=0 and not capLogged then
   capLogged=true
   if s.flight_cap_written==1 then
    DebugError(string.format('[X4LAN] FLIGHTCAP written records=%d dropped=%d bytes=%d',
     s.flight_cap_records or 0,s.flight_cap_dropped or 0,s.flight_cap_bytes or 0))
   else
    DebugError('[X4LAN] FEATURE flightcap DISABLED reason=capture file write failed')
   end
  end
  if s.flight_reason and not flightRefusedLogged then
   flightRefusedLogged=true
   DebugError('[X4LAN] FEATURE flightprof DISABLED reason='..tostring(s.flight_reason))
  end
 elseif (s.phys_installed or 1)==0 and not physRefusedLogged then
  physRefusedLogged=true
  DebugError('[X4LAN] FEATURE physprof DISABLED reason='..tostring(s.phys_reason or 'unknown'))
 end
 nextLog=now+1
end
-- Physics job mode: the DLL queues the request and switches at the next frame end on the frame
-- brackets' thread; FEATURE joltwait ENABLED/DISABLED follow from stats().
local function setPhysicsMode(mode,now)
 local ok,why=owner:physics_mode(mode)
 if ok then
  DebugError(string.format('[X4LAN] PHYSMODE mode=%s t=%.3f',mode,now))
 else
  DebugError('[X4LAN] FEATURE joltwait DISABLED reason='..tostring(why))
  physMode='refused'
 end
end
local function capturePath(model)
 local ok,ffi=pcall(require,'ffi')
 if not ok then return nil end
 pcall(ffi.cdef,'const char* GetSaveFolderPath(void);')
 local okp,folder=pcall(function() return ffi.string(ffi.C.GetSaveFolderPath()) end)
 if not okp or not folder then return nil end
 folder=folder:gsub('\\','/'):gsub('/+$',''):gsub('/save$','')
 return folder..'/x4mp-flightcap-'..model..'.log'
end
local function applyFlightCapture()
 if capRequested or not captureReady or not X4LAN_FLIGHTCAP or not owner.flight_capture then return end
 capRequested=true
 local path=X4LAN_FLIGHTCAP_PATH or capturePath(X4LAN_FLIGHTCAP)
 local n,frames=X4LAN_FLIGHTCAP_N or 20000,X4LAN_FLIGHTCAP_FRAMES or 3000
 local ok,why
 if path then ok,why=owner:flight_capture(X4LAN_FLIGHTCAP,n,path,frames) else why='no profile folder' end
 if ok then
  DebugError(string.format('[X4LAN] FLIGHTCAP armed model=%s records=%d frames=%d path=%s',X4LAN_FLIGHTCAP,n,frames,path))
 else
  capLogged=true
  DebugError('[X4LAN] FEATURE flightcap DISABLED reason='..tostring(why))
 end
end
local function applyFlightCensus()
 if flightRequested or not X4LAN_FLIGHTPROF or not owner.flight_census then return end
 flightRequested=true
 local ok,why=owner:flight_census('on')
 if not ok and not flightRefusedLogged then
  flightRefusedLogged=true
  DebugError('[X4LAN] FEATURE flightprof DISABLED reason='..tostring(why))
 end
end
local function applyPhysicsMode(now)
 if not owner.physics_mode or physMode=='refused' then return end
 local setting=X4LAN_PHYSICS_MODE or 'joltwait'
 if setting=='ab' then
  if now>=nextPhysMode then
   nextPhysMode=now+(X4LAN_PHYSICS_AB_S or 20)
   physMode=(physMode=='joltwait') and 'original' or 'joltwait'
   setPhysicsMode(physMode,now)
  end
 elseif setting~='original' and not physMode then
  physMode=setting;setPhysicsMode(setting,now)   -- an unknown setting is refused by the DLL, loudly
 end
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
  applyPhysicsMode(now)
  applyFlightCensus()
  applyFlightCapture()
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
