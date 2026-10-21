-- Isolated qualification of the production seed + saved-dock admission gates.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.seed_admission_probe then return end
assert(cfg.enabled==true and cfg.test_profile=='45841' and cfg.attachment_sync==true and cfg.seed_probe=='load','explicit isolated admission diagnostic required')
local ffi=require('ffi');local C=ffi.C
if not pcall(ffi.typeof,'UniverseID') then ffi.cdef('typedef uint64_t UniverseID;') end
if not pcall(ffi.typeof,'UIPosRot') then ffi.cdef('typedef struct {float x,y,z,yaw,pitch,roll;} UIPosRot;') end
ffi.cdef[[
bool IsPlayerValid(void);bool IsGamePaused(void);bool IsComponentOperational(UniverseID);
const char* GetObjectIDCode(UniverseID);UniverseID GetContextByClass(UniverseID,const char*,bool);
UIPosRot GetObjectPositionInSector(UniverseID);uint32_t GetAllFactions(const char**,uint32_t,bool);
uint32_t GetNumAllFactionStations(const char*);uint32_t GetAllFactionStations(UniverseID*,uint32_t,const char*);
]]
local function id(object) return ConvertStringToLuaID((tostring(object):gsub('ULL$',''))) end
local function log(text) DebugError('[X4LAN] SEED_PROBE '..text) end
local function admission(text) DebugError('[X4LAN] SEED_ADMISSION '..text) end
local finished
local pauseCalls,resumeCalls=0,0
local nativePause,nativeUnpause=Pause,Unpause
Pause=function() pauseCalls=pauseCalls+1;nativePause() end
Unpause=function() resumeCalls=resumeCalls+1;nativeUnpause() end
local trigger=AddUITriggeredEvent
local importPaused=false
AddUITriggeredEvent=function(screen,control,value)
 if not (finished and X4LAN_ADMISSION_AFTER) and ((screen=='X4LAN_Seed' and control=='Inspect') or (screen=='X4LAN_SeedDocks' and control=='Read')) then
  assert(C.IsGamePaused(),'native admission read requested while simulation runs')
  if screen=='X4LAN_SeedDocks' and not importPaused then
   DebugError('[X4LAN] DOCK_IMPORT_PAUSE mode=load paused=true')
   importPaused=true
  end
  admission('request control='..control..' paused=true ready='..tostring(X4LAN_SEED_GATE.ready))
 end
 return trigger(screen,control,value)
end
local newImporter=X4LAN_SEED_DOCK_IMPORT.new
X4LAN_SEED_DOCK_IMPORT.new=function(...)
 local importer=newImporter(...);local receive=importer.receive
 importer.receive=function(self,kind,value,now)
  local prefix=finished and X4LAN_ADMISSION_AFTER and 'ATTACH_SESSION_DOCK' or 'DOCK_IMPORT'
  DebugError('[X4LAN] '..prefix..' event mode=load kind='..kind..' value='..tostring(value))
  return receive(self,kind,value,now)
 end
 return importer
end
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
local started,failed,released,begun,queries,querying,waitingMarker,count,releaseWait
local function guard(fn)
 if failed or finished then return end
 local ok,why=pcall(fn)
 if not ok then failed=true;admission('STOPPED '..tostring(why));if C.IsPlayerValid() and not C.IsGamePaused() then Pause() end end
end
RegisterEvent('X4LAN_Seed.State',function(_,value) guard(function()
 if not waitingMarker then return end
 assert(value==cfg.seed_id..'|1|1','post-admission world is no longer the exact pristine loaded seed')
 log('loaded id='..cfg.seed_id)
 admission('complete seed='..cfg.seed_id..' count='..count..' pristine=1 loads=1')
 finished=true
end) end)
SetScript('onUpdate',function()
 if finished and X4LAN_ADMISSION_AFTER then X4LAN_ADMISSION_AFTER.frame(GetCurRealTime());return end
 guard(function()
 local now=GetCurRealTime();started=started or now;assert(now-started<240,'native admission diagnostic timeout')
 local G=assert(X4LAN_SEED_GATE);local D=assert(X4LAN_SEED_DOCK_GATE)
 local ready=G.frame(now);assert(not G.failed,G.failed)
 if not ready then return end
 assert(G.markerReady and G.loaded and D.ready and D.importer:ready(),'gameplay admitted before complete native prerequisites')
 if not released then
  local paused=C.IsGamePaused()
  if pauseCalls>0 and resumeCalls==1 and paused then
   releaseWait=releaseWait or now;assert(now-releaseWait<2,'native owned pause did not release');return
  end
  assert((pauseCalls>0 and resumeCalls==1 and not paused) or (pauseCalls==0 and resumeCalls==0 and paused),'startup pause ownership differs')
  admission(string.format('ready seed=%s paused=%s pauseCalls=%d resumeCalls=%d',cfg.seed_id,tostring(paused),pauseCalls,resumeCalls))
  released=true
  if not paused then Pause() end -- Freeze only the following evidence comparison.
 end
 if not begun then
  if not C.IsGamePaused() then Pause();return end
  local fingerprints=D.importer:fingerprints();count=#fingerprints;queries={};local external,storage
  for index,value in ipairs(fingerprints) do
   DebugError('[X4LAN] DOCK_IMPORT fingerprint mode=load index='..index..' value='..value)
   local e,s=value:match('^[^:]+:[^:]+:[^:]+:[^:]+:([01]):([01]):')
   if e=='1' and not external then external=index;queries[#queries+1]={index,'EXTERNAL'} end
   if s=='1' and not storage then storage=index;queries[#queries+1]={index,'STORED'} end
  end
  assert(external and storage,'no native external/storage query coverage');begun=true
 end
 assert(C.IsGamePaused(),'post-admission evidence comparison lost its explicit pause')
 if querying or waitingMarker then return end
 local query=table.remove(queries,1)
 if query then
  querying=true
  assert(D.importer:query(query[1],query[2],now,function(row)
   DebugError('[X4LAN] DOCK_IMPORT queried mode=load index='..query[1]..' state='..query[2]..' fingerprint='..row.fingerprint)
   querying=false
  end))
 else
  DebugError('[X4LAN] DOCK_IMPORT complete mode=load seed='..cfg.seed_id..' count='..count)
  stations();waitingMarker=true;AddUITriggeredEvent('X4LAN_Seed','Inspect')
 end
end) end)
