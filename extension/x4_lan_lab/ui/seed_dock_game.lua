-- Optional startup prerequisite. Unregistered until the native/session adapter is
-- ready; the seed gate refuses attachment mode when this dependency is missing.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.enabled or not cfg.attachment_sync then return end
assert(cfg.seed_binding==true and cfg.seed_verified==true and cfg.test_profile=='45841','verified isolated seed required')
local I=assert(X4LAN_SEED_DOCK_IMPORT,'saved dock importer missing')
local D={ready=false};X4LAN_SEED_DOCK_GATE=D
local function fail(why) D.failed=tostring(why);D.ready=false;return false,D.failed end
for _,kind in ipairs({'Begin','Row','End','Error'}) do
 RegisterEvent('X4LAN_SeedDocks.'..kind,function(_,value)
  if D.failed then return end
  if not D.importer then fail('unsolicited native dock event before startup import');return end
  local ok,why=D.importer:receive(kind,tostring(value),GetCurRealTime())
  if not ok then fail(why) end
 end)
end
function D.frame(now)
 if D.failed then return false,D.failed end
 if not D.importer then
  D.importer=I.new(cfg.seed_id,'load',function(value) AddUITriggeredEvent('X4LAN_SeedDocks','Read',value) end)
 end
 local ok,why=D.importer:frame(now)
 if not ok then return fail(why) end
 if D.importer:ready() then
  if not D.ready then DebugError('[X4LAN] SEED_DOCK_GATE READY seed='..cfg.seed_id) end
  D.ready=true
 elseif D.ready then return fail('saved dock registry lost after admission') end
 return D.ready
end
