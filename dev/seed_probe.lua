-- Unshipped feasibility test: an engine-created, marked disposable save must
-- preserve native station identity/topology/pose on reload. Gameplay is disabled.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.seed_probe then return end
assert(cfg.enabled==false and cfg.test_profile=='45841','seed probe requires disabled gameplay in isolated profile')
assert(cfg.seed_probe=='capture' or cfg.seed_probe=='load','invalid seed probe action')
assert(type(cfg.seed_id)=='string' and #cfg.seed_id==32 and cfg.seed_id:match('^[a-f0-9]+$'),'invalid seed ID')
local ffi=require('ffi')
if not pcall(ffi.typeof,'UniverseID') then ffi.cdef('typedef uint64_t UniverseID;') end
if not pcall(ffi.typeof,'UIPosRot') then ffi.cdef('typedef struct {float x,y,z,yaw,pitch,roll;} UIPosRot;') end
ffi.cdef[[
bool IsPlayerValid(void);bool IsGamePaused(void);bool IsSaveListLoadingComplete(void);
void ReloadSaveList(void);
const char* GetSaveFolderPath(void);const char* GetObjectIDCode(UniverseID);
UniverseID GetContextByClass(UniverseID,const char*,bool);
UIPosRot GetObjectPositionInSector(UniverseID);
uint32_t GetAllFactions(const char**,uint32_t,bool);
uint32_t GetNumAllFactionStations(const char*);
uint32_t GetAllFactionStations(UniverseID*,uint32_t,const char*);
bool IsComponentOperational(UniverseID);
]]
local C=ffi.C
local savepath=ffi.string(C.GetSaveFolderPath()):gsub('\\','/'):gsub('/+$','')
local suffix='/45841/save'
assert(savepath:sub(-#suffix)==suffix,'seed probe outside isolated profile')
local function id(object) return ConvertStringToLuaID((tostring(object):gsub('ULL$',''))) end
local function log(text) DebugError('[X4LAN] SEED_PROBE '..text) end
local function stations()
 local names=ffi.new('const char*[256]')
 local n=tonumber(C.GetAllFactions(names,256,true));assert(n>0 and n<256,'faction limit')
 local rows,seen={},{}
 for i=0,n-1 do
  local faction=ffi.string(names[i])
  local count=tonumber(C.GetNumAllFactionStations(faction));assert(count<=20000,'station limit')
  local objects=ffi.new('UniverseID[?]',count+64)
  local actual=tonumber(C.GetAllFactionStations(objects,count+64,faction))
  assert(actual<count+64 and tonumber(C.GetNumAllFactionStations(faction))<=actual,'station enumeration changed')
  for j=0,actual-1 do
   local object=objects[j];local key=tostring(object):gsub('ULL$','')
   if not seen[key] and C.IsComponentOperational(object) then
    seen[key]=true
    local sector=C.GetContextByClass(object,'sector',false)
    if sector~=0 then
     local macro,owner=GetComponentData(id(object),'macro','owner')
     local space=GetComponentData(id(sector),'macro');local p=C.GetObjectPositionInSector(object)
     rows[#rows+1]=string.format('object=%s code=%s macro=%s owner=%s sector=%s pose=%.9g,%.9g,%.9g,%.9g,%.9g,%.9g',
      key,ffi.string(C.GetObjectIDCode(object)),macro,owner,space,p.x,p.y,p.z,p.yaw,p.pitch,p.roll)
    end
   end
  end
 end
 table.sort(rows)
 log('begin mode='..cfg.seed_probe..' id='..cfg.seed_id..' count='..#rows)
 for _,row in ipairs(rows) do log('station '..row) end
 log('end mode='..cfg.seed_probe..' id='..cfg.seed_id..' count='..#rows)
end
local started,asked,finished,loading,saveRequested
local function freezeImportBoundary()
 -- The disposable world must not evolve during the initial readiness delay,
 -- including the first frames after native reload. Dock-import start is too late.
 if cfg.seed_dock_import_probe and not finished and C.IsPlayerValid() and not C.IsGamePaused() then Pause() end
end
RegisterEvent('X4LAN_Seed.Loaded',freezeImportBoundary)
RegisterEvent('X4LAN.Loaded',function() started=GetCurRealTime();asked=false;C.ReloadSaveList() end)
RegisterEvent('gameSaved',function(_,success)
 if saveRequested then log('saved success='..tostring(success)..' id='..cfg.seed_id);finished=true end
end)
RegisterEvent('X4LAN_Seed.State',function(_,payload)
 if finished then return end
 local expected=cfg.seed_id..'|1|'..(cfg.seed_probe=='capture' and '0' or '1')
 assert(payload==expected,'production seed marker/load state differs')
 local function observeAndFinish()
  if not C.IsGamePaused() then Pause() end
  stations()
  if cfg.seed_probe=='capture' then
   assert(IsSavingPossible(false),'native saving unavailable')
   saveRequested=true;log('save_requested id='..cfg.seed_id)
   SaveGame('save_010','X4 Multiplayer disposable seed '..cfg.seed_id)
  else finished=true;log('loaded id='..cfg.seed_id) end
 end
 if cfg.dock_registry_probe then
  assert(X4LAN_SEED_DOCKS,'optional dock registry probe missing')
  X4LAN_SEED_DOCKS.start(cfg.seed_probe,cfg.seed_id,observeAndFinish)
 else observeAndFinish() end
end)
RegisterEvent('X4LAN_SeedProbe.State',function(_,value)
 if finished then return end
 if value==cfg.seed_id then
  -- Saving waits for the production marker too, not assumed UI-event ordering.
  AddUITriggeredEvent('X4LAN_Seed','Inspect')
 elseif cfg.seed_probe=='capture' and value=='' then
  AddUITriggeredEvent('X4LAN_Seed','Mark',cfg.seed_id)
  AddUITriggeredEvent('X4LAN_SeedProbe','Mark',cfg.seed_id)
 elseif cfg.seed_probe=='load' and not loading and value=='' then
  loading=true;log('load_requested id='..cfg.seed_id);LoadGame('save_010')
 else error('unexpected seed marker; refusing to use another world') end
end)
SetScript('onUpdate',function()
 freezeImportBoundary()
 if X4LAN_SEED_DOCKS then X4LAN_SEED_DOCKS.frame(GetCurRealTime()) end
 if finished or asked or not C.IsPlayerValid() then return end
 if not started then
  if not loading then AddUITriggeredEvent('X4LAN','Ready') end
  return
 end
 if GetCurRealTime()-started<3 or not C.IsSaveListLoadingComplete() then return end
 local saves=GetSaveList({save_010=true})
 local exists=false
 for _,entry in pairs(saves or {}) do
  if entry.filename=='save_010' then exists=true end
 end
 if cfg.seed_probe=='capture' then assert(not exists,'seed slot already occupied; never overwrite it')
 else assert(exists,'transferred seed slot absent') end
 asked=true;AddUITriggeredEvent('X4LAN_SeedProbe','Read')
end)
