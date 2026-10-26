-- Opt-in server frame diet (dedicated server mode) for an explicitly isolated test installation.
-- Inert unless X4LAN_FRAMEDIET_MODE is set; see reports/REWRITE-FRAME-DIET.md.
-- X4LAN_FRAMEDIET_MODE        'diet' (skip presentation), 'pass' (wrappers inert), 'audit' (measure)
-- X4LAN_FRAMEDIET_SITES       default 'server' (= ui_scenes,draw); also ui_render, camera, all
-- X4LAN_FRAMEDIET_PERIOD      every Nth frame presents everything (default 0 = never)
-- X4LAN_FRAMEDIET_AUDIT_EVERY frames between component digests in audit mode (default 30)
-- X4LAN_FRAMEDIET_DIGEST      components per digest (default 16384)
-- X4LAN_FRAMEDIET_REPORT_S    seconds between FRAMEDIET lines (default 10)
-- X4LAN_FRAMEDIET_AB_S        diet mode: alternate diet/pass every N seconds (A/B in one session)
-- X4LAN_FRAMEDIET_DELAY_S     seconds after the first UI update before arming (default 0)
-- X4LAN_FRAMEDIET_PATH        development DLL path
if not X4LAN_FRAMEDIET_MODE then return end
if not X4LAN_CONFIG or X4LAN_CONFIG.test_profile~='45841' then return end
if X4LAN_FRAMEDIET_PROBE then return end
X4LAN_FRAMEDIET_PROBE=true
local function disabled(reason)
 DebugError('[X4LAN] FEATURE framediet DISABLED reason='..tostring(reason))
end
local initialise,message=package.loadlib(X4LAN_FRAMEDIET_PATH or
 './extensions/x4_lan_lab/native/x4lan_framediet_64.dll','luaopen_x4lan_framediet')
if not initialise then disabled('load failed: '..tostring(message));return end
local native=initialise()
local mode=X4LAN_FRAMEDIET_MODE
local sites=X4LAN_FRAMEDIET_SITES or 'server'
local reportEvery=X4LAN_FRAMEDIET_REPORT_S or 10
local abEvery=mode=='diet' and X4LAN_FRAMEDIET_AB_S or nil
local owner,finished,armAt,nextTry,nextReport,nextPhase,phase
local SITE_NAMES={{1,'ui_scenes'},{2,'ui_render'},{4,'camera'},{8,'draw'}}
local function siteText(mask)
 local names={}
 for _,pair in ipairs(SITE_NAMES) do
  if math.floor(mask/pair[1])%2==1 then names[#names+1]=pair[2] end
 end
 return #names>0 and table.concat(names,',') or 'none'
end
local function report(s,now)
 DebugError(string.format('[X4LAN] FRAMEDIET mode=%s sites=%s phase=%s t=%.3f frames=%d loading=%d present=%d',
  s.mode,siteText(s.sites or 0),phase or s.mode,now,s.frames,s.loading_frames,s.present_frames)..
  string.format(' scenesCalls=%d scenesSkipped=%d uiRenderCalls=%d uiRenderSkipped=%d cameraCalls=%d cameraSkipped=%d drawOwned=%d drawSet=%d drawReapplied=%d',
  s.ui_scenes_calls,s.ui_scenes_skipped,s.ui_render_calls,s.ui_render_skipped,s.camera_calls,s.camera_skipped,
  s.draw_owned,s.draw_set,s.draw_reapplied))
 if s.mode~='audit' then return end
 for _,row in ipairs({'ui_scenes','ui_render','camera','control'}) do
  local function f(k) return s[row..'_'..k] or 0 end
  if f('audited')>0 or f('digests')>0 then
   DebugError(string.format('[X4LAN] FRAMEDIET_AUDIT row=%s audited=%d msMean=%.4f msMax=%.3f rngCalls=%d rngSteps=%d rngUnknown=%d idCalls=%d ids=%d eventCalls=%d eventsUp=%d timeChanged=%d',
    row,f('audited'),f('ms_mean'),f('ms_max'),f('rng_calls'),f('rng_steps'),f('rng_unknown'),f('id_calls'),f('ids'),
    f('event_calls'),f('events_up'),f('time_changed'))..
    string.format(' digests=%d components=%d changed=%d unreadable=%d words=%s first=%s',
    f('digests'),f('digest_components'),f('digest_changed'),f('digest_unreadable'),
    s[row..'_words']~='' and s[row..'_words'] or '-',s[row..'_first']~='' and s[row..'_first'] or '-'))
  end
 end
end
local function arm(now)
 local ok,result,why=pcall(native.start,mode,sites,X4LAN_FRAMEDIET_PERIOD,X4LAN_FRAMEDIET_AUDIT_EVERY,X4LAN_FRAMEDIET_DIGEST)
 if not ok then finished=true;disabled(result);return end
 if not result then
  if why=='loading' then return end
  finished=true;disabled(why);return
 end
 owner=result;phase=mode
 local s=owner:stats()
 DebugError(string.format('[X4LAN] FRAMEDIET armed mode=%s sites=%s period=%d digest=%d t=%.3f',
  mode,siteText(s.sites),s.period,s.digest_max,now))
 nextReport=now+reportEvery
 if abEvery then nextPhase=now+abEvery end
end
SetScript('onUpdate',function()
 if finished then return end
 local now=GetCurRealTime()
 if owner then
  if not owner:active() then
   local s=owner:stats()
   if s then report(s,now) end
   disabled(s and s.reason or 'disarmed');owner:stop();owner=nil;finished=true;return
  end
  if abEvery and now>=nextPhase then
   phase=phase=='diet' and 'pass' or 'diet'
   local ok,why=owner:set(phase)
   if not ok then disabled('phase switch refused: '..tostring(why));owner:stop();owner=nil;finished=true;return end
   nextPhase=now+abEvery
   DebugError(string.format('[X4LAN] FRAMEDIET phase=%s t=%.3f frames=%d',phase,now,owner:stats().frames))
  end
  if now>=nextReport then report(owner:stats(),now);nextReport=now+reportEvery end
  return
 end
 armAt=armAt or now+(X4LAN_FRAMEDIET_DELAY_S or 0)
 if now<armAt or now<(nextTry or 0) then return end
 nextTry=now+1
 arm(now)
end)
