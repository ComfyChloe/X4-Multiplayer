-- Opt-in cadence probe for an explicitly isolated development installation.
-- X4LAN_FIXEDTICK_MODE='fixed60' forces dt=1/60 (Stage 1); 'fixed60catchup' schedules
-- k/60 steps with k Jolt sub-steps so game time tracks wall time (Stage 2);
-- 'fixed60stage3' adds the main-loop wait wrapper that re-presents the last completed
-- frame while a simulation frame is late (Stage 3).
-- X4LAN_FIXEDTICK_OPTIONS='key=value,...' (kcap, represent, shortwait, stall_ms,
-- stall_every, stall_slot); the stall injector is a test tool and off unless stall_ms is set.
if not X4LAN_CONFIG or X4LAN_CONFIG.test_profile~='45841' then return end
if X4LAN_FIXEDTICK_PROBE then return end
X4LAN_FIXEDTICK_PROBE=true
local initialise,message=package.loadlib(X4LAN_FIXEDTICK_PATH or
 './extensions/x4_lan_lab/native/x4lan_fixedtick_64.dll','luaopen_x4lan_fixedtick')
assert(initialise,message)
local native=initialise()
local owner,reason
local nextTry=0
local nextLog=0
local function stage3Mode() return X4LAN_FIXEDTICK_MODE=='fixed60stage3' end
local function logStats(now)
 local s=owner:stats()
 local line=string.format('[X4LAN] FRAMEPROBE frames=%d wallDt=%.6f maxWallDt=%.6f gameDt=%.9f realDt=%.9f gameTime=%.6f realTime=%.6f speed=%.3f paused=%d loadingSkips=%d regressions=%d',
  s.frames,s.wall_dt,s.max_wall_dt,s.game_dt,s.real_dt,s.game_time,s.real_time,s.speed,
  s.paused_frames,s.loading_skips,s.game_time_regressions)..
  string.format(' fixed=%d fixedFrames=%d wallElapsed=%.6f gameElapsed=%.6f',
  s.fixed or 0,s.fixed_frames or 0,s.wall_elapsed or 0,s.game_elapsed or 0)..
  string.format(' catchup=%d steps=%d zeroStep=%d multiStep=%d dropped=%d lastSteps=%d acc=%.6f',
  s.catchup or 0,s.steps_total or 0,s.zero_step_frames or 0,s.multi_step_frames or 0,
  s.dropped_ticks or 0,s.last_steps or 0,s.accumulator or 0)
 if (s.catchup or 0)~=0 then
  line=line..string.format(' kCap=%d capped=%d slow=%d',s.k_cap or 0,s.capped_ticks or 0,s.slow_frames or 0)
 end
 if (s.stalls_injected or 0)~=0 then line=line..string.format(' stalls=%d',s.stalls_injected) end
 if (s.stage3 or 0)~=0 then
  line=line..string.format(' stage3=1 represent=%d active=%d rePresents=%d wouldRepresent=%d waitsOk=%d waitsTimeout=%d waitsOther=%d budgetSkips=%d maxPresentGap=%.6f indexChanged=%d queueChanged=%d inFlightChanged=%d gates=%s',
   s.represent or 0,s.represent_active or 0,s.re_presents or 0,s.would_represent or 0,
   s.waits_ok or 0,s.waits_timeout or 0,s.waits_other or 0,s.budget_skips or 0,
   s.max_present_gap or 0,s.index_changed or 0,s.queue_count_changed or 0,
   s.in_flight_changed or 0,(s.gate_summary and s.gate_summary~='') and s.gate_summary or 'none')
 end
 DebugError(line)
 nextLog=now+1
end
SetScript('onUpdate',function()
 local now=GetCurRealTime()
 if owner then
  if not owner:active() then
   local why=tostring(owner:stats().reason or 'unknown')
   DebugError('[X4LAN] FRAMEPROBE disarmed: '..why)
   if stage3Mode() then DebugError('[X4LAN] FEATURE fixedtick_stage3 DISABLED reason='..why) end
   owner:stop();owner=nil;return
  end
  if now>=nextLog then logStats(now) end
  return
 end
 if now<nextTry then return end
 nextTry=now+1
 local ok,result,why
 if X4LAN_FIXEDTICK_OPTIONS then ok,result,why=pcall(native.start,X4LAN_FIXEDTICK_MODE or 'probe',X4LAN_FIXEDTICK_OPTIONS)
 elseif X4LAN_FIXEDTICK_MODE then ok,result,why=pcall(native.start,X4LAN_FIXEDTICK_MODE)
 else ok,result,why=pcall(native.start) end
 if not ok then error(result) end
 if result then
  owner=result;nextLog=0
  DebugError('[X4LAN] FRAMEPROBE armed in MoveWorker slot 5 mode='..(X4LAN_FIXEDTICK_MODE or 'probe'))
  if stage3Mode() then
   local s=owner:stats()
   if (s.represent or 0)~=0 then
    DebugError(string.format('[X4LAN] FEATURE fixedtick_stage3 ON represent=1 kCap=%d',s.k_cap or 0))
   else
    DebugError('[X4LAN] FEATURE fixedtick_represent DISABLED reason=option represent=0 (counting wouldRepresent only)')
   end
  end
  return
 end
 if why=='loading' then return end
 if stage3Mode() then DebugError('[X4LAN] FEATURE fixedtick_stage3 DISABLED reason='..tostring(why)) end
 error('FRAMEPROBE registration refused: '..tostring(why))
end)
