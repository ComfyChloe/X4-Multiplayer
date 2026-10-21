-- Optional pristine-host-save admission. The launcher verifies bytes/content
-- before staging save_010; the native gate verifies the loaded MD world marker.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.enabled or cfg.seed_id==nil then return end
local G={ready=false};X4LAN_SEED_GATE=G
local ffi=require('ffi');local C=ffi.C
ffi.cdef[[
bool IsPlayerValid(void);bool IsGamePaused(void);bool IsSaveListLoadingComplete(void);
void ReloadSaveList(void);const char* GetSaveFolderPath(void);
]]
local function log(text) DebugError('[X4LAN] SEED_GATE '..text) end
local function fail(reason) G.failed=reason;G.ready=false;log('STOPPED '..reason) end
local function freezeAdmission()
 if cfg.attachment_sync and not G.ready and C.IsPlayerValid() and not C.IsGamePaused() then
  G.ownsPause=true;Pause()
 end
end
local function ready()
 G.ready=true;log('READY seed='..cfg.seed_id..' slot='..cfg.slot)
 if G.ownsPause then G.ownsPause=nil;if C.IsGamePaused() then Unpause() end end
end
local function valid()
 assert(cfg.test_profile=='45841' and cfg.bubble_actions and cfg.coop and cfg.world_sync,
  'seed admission requires isolated cooperative world mode')
 assert(type(cfg.seed_id)=='string' and #cfg.seed_id==32 and cfg.seed_id:match('^[a-f0-9]+$'),
  'invalid seed identity')
 assert(cfg.bubble==cfg.seed_id and cfg.seed_verified==true,'unverified seed or relay session mismatch')
 assert(not cfg.attachment_sync or (cfg.attachment_sync==true and cfg.seed_binding==true),
  'attachment admission requires explicit saved-object binding')
 local path=ffi.string(C.GetSaveFolderPath()):gsub('\\','/'):gsub('/+$','')
 assert(path:sub(-#'/45841/save')=='/45841/save','seed admission outside isolated profile')
end
local ok,err=pcall(valid)
if not ok then fail(tostring(err));return end
RegisterEvent('X4LAN_Seed.Loaded',function()
 -- Reloading an active multiplayer game would retain stale Lua/network state.
 if G.ready then fail('active session reload is unsupported');return end
 freezeAdmission()
 G.loaded=true;G.pending=false;G.started=GetCurRealTime();C.ReloadSaveList()
end)
RegisterEvent('X4LAN_Seed.State',function(_,payload)
 if G.failed or G.ready or not G.pending then return end
 G.pending=false
 local marker,pristine,loads=tostring(payload):match('^(.*)|([01])|(%d+)$')
 if not marker then fail('malformed world marker');return end
 loads=tonumber(loads)
 if pristine~='1' then fail('snapshot already contains multiplayer fixtures');return end
 if marker==cfg.seed_id then
  -- UI handlers may be installed after event_game_loaded. MD retains the load
  -- count, so inspect native state instead of relying on one Lua notification.
  -- A restored marker can become readable before the load-completed MD event.
  -- Zero remains pending and is bounded by the same admission timeout.
  if loads==0 then
   if not G.waitLogged then G.waitLogged=true;log('WAIT_NATIVE_LOAD seed='..marker) end
   return
  end
  if loads~=1 then fail('snapshot has unexpected native load count '..loads);return end
  G.loaded=true
  G.markerReady=true
  if not cfg.attachment_sync then ready() end
  return
 end
 if marker~='' or loads~=0 then
  fail('loaded world differs from the staged session');return
 end
 if G.loadRequested then return end -- The old world may answer while loading starts.
 G.loadRequested=true;G.started=GetCurRealTime()
 log('LOAD_REQUESTED seed='..cfg.seed_id)
 LoadGame('save_010')
end)
function G.frame(now)
 G.started=G.started or now
 freezeAdmission()
 if G.failed then return false end
 if not G.ready and now-G.started>180 then fail('native seed load timed out');return false end
 if cfg.attachment_sync and not G.ready and C.IsPlayerValid() and not C.IsGamePaused() then return false end
 if cfg.attachment_sync and G.markerReady then
  if not G.ready and (not C.IsPlayerValid() or not C.IsGamePaused()) then return false end
  local docks=X4LAN_SEED_DOCK_GATE
  if not docks or type(docks.frame)~='function' then fail('saved dock admission module unavailable');return false end
  local ok,complete=pcall(docks.frame,now)
  if not ok or docks.failed then fail('saved dock admission failed: '..tostring(docks.failed or complete));return false end
  if not complete then
   if G.ready then fail('saved dock admission lost after startup') end
   return false
  end
  if not G.ready then ready() end
  return true
 end
 if G.ready then return true end
 if not C.IsPlayerValid() or G.pending or now<(G.nextInspect or 0) then return false end
 if not G.refreshed then G.refreshed=true;C.ReloadSaveList();return false end
 if not C.IsSaveListLoadingComplete() then return false end
 local found=false
 for _,entry in pairs(GetSaveList({save_010=true}) or {}) do
  if entry.filename=='save_010' then found=true end
 end
 if not found then fail('verified isolated seed slot is absent');return false end
 G.pending=true;G.nextInspect=now+1
 AddUITriggeredEvent('X4LAN_Seed','Inspect')
 return false
end
