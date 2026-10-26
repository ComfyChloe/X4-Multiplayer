-- Opt-in cadence probe for an explicitly isolated development installation.
-- X4LAN_FIXEDTICK_MODE='fixed60' forces dt=1/60 (Stage 1); 'fixed60catchup' schedules
-- k/60 steps with k Jolt sub-steps so game time tracks wall time (Stage 2).
if not X4LAN_CONFIG or X4LAN_CONFIG.test_profile~='45841' then return end
if X4LAN_FIXEDTICK_PROBE then return end
X4LAN_FIXEDTICK_PROBE=true
local initialise,message=package.loadlib(X4LAN_FIXEDTICK_PATH or
 './extensions/x4_lan_lab/native/x4lan_fixedtick_64.dll','luaopen_x4lan_fixedtick')
assert(initialise,message)
local native=initialise()
local owner,reason
local walkRefusedLogged
local nextTry=0
local nextLog=0
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
  DebugError(string.format('[X4LAN] WALKPROF frames=%d walkMs=%.3f walkMaxMs=%.3f share=%.3f sectors=%.2f sectorSumMs=%.3f sectorMaxMs=%.3f speedup=%.2f zones=%.1f zoneMs=%.3f ships=%.1f shipMs=%.3f stations=%.1f stationMs=%.3f clusterMs=%.3f',
   s.walk_frames,s.walk_mean_ms or 0,s.walk_max_ms or 0,s.walk_share or 0,s.sector_calls or 0,
   s.sector_sum_ms or 0,s.sector_max_ms or 0,s.ideal_speedup or 0,s.zone_calls or 0,s.zone_ms or 0,
   s.ship_calls or 0,s.ship_ms or 0,s.station_calls or 0,s.station_ms or 0,s.cluster_ms or 0))
 elseif (s.walk_refused or 0)==1 and not walkRefusedLogged then
  walkRefusedLogged=true
  DebugError('[X4LAN] FEATURE walkprof DISABLED reason=vtable or signature mismatch')
 end
 nextLog=now+1
end
SetScript('onUpdate',function()
 local now=GetCurRealTime()
 if owner then
  if not owner:active() then
   DebugError('[X4LAN] FRAMEPROBE disarmed: '..tostring(owner:stats().reason or 'unknown'))
   owner:stop();owner=nil;return
  end
  if now>=nextLog then logStats(now) end
  return
 end
 if now<nextTry then return end
 nextTry=now+1
 local ok,result,why
 if X4LAN_FIXEDTICK_MODE then ok,result,why=pcall(native.start,X4LAN_FIXEDTICK_MODE)
 else ok,result,why=pcall(native.start) end
 if not ok then error(result) end
 if result then owner=result;nextLog=0;DebugError('[X4LAN] FRAMEPROBE armed in MoveWorker slot 5 mode='..(X4LAN_FIXEDTICK_MODE or 'probe'));return end
 if why=='loading' then return end
 error('FRAMEPROBE registration refused: '..tostring(why))
end)
