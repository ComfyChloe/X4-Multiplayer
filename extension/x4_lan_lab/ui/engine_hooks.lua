-- Default-enabled STATEHASH recorder and Stage 1 MoveWorker reseed; no network changes.
local cfg=X4LAN_CONFIG
if not cfg or cfg.engine_hooks~=true then return end
local ffi=require('ffi');local C=ffi.C;local bit=require('bit')
local M={failed=false,observed=false};X4LAN_ENGINE_HOOKS=M
local function esc(value)
 value=tostring(value==nil and '' or value);return (value:gsub('[^%w_%.:%-]',function(c)return string.format('%%%02X',c:byte())end))
end
local function out(kind,text) DebugError('[X4LAN] '..kind..' '..text) end
local function disabled(reason)
 M.failed=true;out('HOOKS_DISABLED','reason='..esc(reason))
 if M.owner then pcall(function()M.owner:stop()end);M.owner=nil end
end
local function declare(name,definition)if not pcall(ffi.typeof,name)then pcall(ffi.cdef,definition)end end
local function key(id)return(tostring(id):gsub('ULL$',''))end
local function luaId(id)return ConvertStringToLuaID(key(id))end
local function integer(value,low,high,default)
 if value==nil then return default end
 assert(type(value)=='number' and value==math.floor(value) and value>=low and value<=high,'engine hash setting outside bounds')
 return value
end
local function atom(value)return type(value)=='string' and #value>0 and #value<=128 and value:match('^[%w_%.%-]+$')~=nil end
local function optional(name)local ok,fn=pcall(function()return C[name]end);return ok and fn~=nil end
local settings,native
local ok,err=pcall(function()
 assert(cfg.test_profile=='45841','engine hooks require profile 45841')
 assert(package.loadlib,'native Lua modules unavailable')
 if GetUISafeModeOption then assert(GetUISafeModeOption()==false,'disable Protected UI mode')end
 declare('UniverseID','typedef uint64_t UniverseID;')
 declare('UIPosRot','typedef struct { float x,y,z,yaw,pitch,roll; } UIPosRot;')
 for _,fn in ipairs({'const char* GetSaveFolderPath(void);','bool IsPlayerValid(void);','bool IsComponentOperational(UniverseID);',
  'UniverseID GetContextByClass(UniverseID,const char*,bool);','UIPosRot GetObjectPositionInSector(UniverseID);',
  'uint32_t GetAllFactions(const char**,uint32_t,bool);','uint32_t GetNumAllFactionShips(const char*);',
  'uint32_t GetAllFactionShips(UniverseID*,uint32_t,const char*);'})do pcall(ffi.cdef,fn)end
 for _,name in ipairs({'GetSaveFolderPath','IsPlayerValid','IsComponentOperational','GetContextByClass','GetObjectPositionInSector',
  'GetAllFactions','GetNumAllFactionShips','GetAllFactionShips'})do assert(optional(name),'missing game export: '..name)end
 local path=ffi.string(C.GetSaveFolderPath()):gsub('\\','/'):gsub('/+$','')
 assert(path:sub(-11)=='/45841/save','inactive outside isolated profile 45841')
 settings={cadence=integer(cfg.engine_hash_cadence,1,3600,1),maxShips=integer(cfg.engine_hash_max_ships,1,20000,4000),
  sectors=cfg.engine_hash_sectors or {'cluster_14_sector001_macro','cluster_01_sector001_macro'},
  run=cfg.engine_hash_run or 'default',session=cfg.build or 'disabled'}
 assert(atom(settings.run) and #settings.run<=32,'invalid engine hash run')
 assert(type(settings.sectors)=='table' and #settings.sectors<=16,'engine_hash_sectors must have at most 16 entries')
 local wanted={};for _,sector in ipairs(settings.sectors)do assert(atom(sector),'invalid engine hash sector');wanted[sector]=true end
 settings.wanted=wanted
 local load,message=package.loadlib(X4LAN_HOOKS_PATH or './extensions/x4_lan_lab/native/x4lan_hooks_64.dll','luaopen_x4lan_hooks')
 assert(load,message);native=load();assert(native and native.start and native.inspect,'invalid hooks module')
end)
if not ok then disabled(err);return end

local function hasher()
 local value=14695981039346656037ULL
 return function(text)
  for i=1,#text do value=(bit.bxor(value,text:byte(i))*1099511628211ULL)end
 end,function()
  local high=tonumber(value/4294967296ULL);local low=tonumber(value%4294967296ULL)
  return string.format('%08x%08x',high,low)
 end
end
local function hashRows(rows)
 local update,digest=hasher();for _,row in ipairs(rows)do update(row);update('\n')end;return digest()
end
local function quantize(value,scale)return tostring(math.floor(value*scale+0.5))end
local function identity(object,macro,owner,code)
 local k=key(object);local api=rawget(_G,'X4LAN_BUBBLE_API')
 local shared=api and api.universe and api.universe.identities and api.universe.identities[k]
 if shared then return 'net:'..tostring(shared.id),false end
 if code and code~='' then return 'code:'..esc(macro)..':'..esc(owner)..':'..esc(code),false end
 return 'local:'..k..':'..esc(macro)..':'..esc(owner),true
end
local function sectorName(object,cache)
 local sector=C.GetContextByClass(object,'sector',false);if sector==0 then return nil end
 local k=key(sector);if cache[k]then return cache[k]end
 local macro=GetComponentData(luaId(sector),'macro');cache[k]=macro;return macro
end
local function inspect(object,details)
 local result,reason=native.inspect(key(object))
 if not result then return nil,reason or 'inspect failed'end
 if not details then M.firstInspect={id=key(object),result=result}end
 return result
end
local function sample(frame,gameTime,dt,rng,sessionSeed)
 local factions=ffi.new('const char*[256]');local count=tonumber(C.GetAllFactions(factions,256,true))
 if count<=0 or count>=256 then return nil,'faction enumeration incomplete'end
 local rows={};local sectors={};local total=0;local weak=0
 for i=0,count-1 do
  local faction=ffi.string(factions[i]);local amount=tonumber(C.GetNumAllFactionShips(faction))
  if amount>250000 then return nil,'faction fleet limit exceeded'end
  local buffer=ffi.new('UniverseID[?]',amount+64)
  local read=tonumber(C.GetAllFactionShips(buffer,amount+64,faction))
  if read>=amount+64 or tonumber(C.GetNumAllFactionShips(faction))>read then return nil,'fleet changed during capture'end
  for j=0,read-1 do
   local object=buffer[j]
   if C.IsComponentOperational(object) then
    local sector=sectorName(object,sectors)
    if sector and settings.wanted[sector] then
     total=total+1;if total>settings.maxShips then return nil,'ship limit exceeded'end
     local macro,owner,code=GetComponentData(luaId(object),'macro','owner','idcode')
     if owner~='player' then
      local id,isWeak=identity(object,macro,owner,code);if isWeak then weak=weak+1 end
      local details,problem=inspect(object,M.firstInspect);if not details then return nil,problem end
      local p=C.GetObjectPositionInSector(object);local degrees=180/math.pi
      rows[#rows+1]={id=id,macro=macro,owner=owner,sector=sector,
       pose=table.concat({quantize(p.x,100),quantize(p.y,100),quantize(p.z,100),
        quantize(p.yaw*degrees,100),quantize(p.pitch*degrees,100),quantize(p.roll*degrees,100)},','),
       hull=string.format('%.6f',details.hull),shield=string.format('%.6f',details.shield)}
     end
    end
   end
  end
 end
 table.sort(rows,function(a,b)
  if a.sector~=b.sector then return a.sector<b.sector end
  if a.id~=b.id then return a.id<b.id end
  return a.macro<b.macro
 end)
 local identities,poses,health={},{},{}
 for _,row in ipairs(rows)do
  local prefix=row.sector..'|'..row.id
  identities[#identities+1]=prefix..'|'..esc(row.macro)..'|'..esc(row.owner)
  poses[#poses+1]=prefix..'|'..row.pose
  health[#health+1]=prefix..'|'..row.hull..'|'..row.shield
 end
 local identityHash,poseHash,healthHash=hashRows(identities),hashRows(poses),hashRows(health)
 local timeHash=hashRows({string.format('%.9f',gameTime)})
 local combined=hashRows({identityHash,poseHash,healthHash,rng,timeHash})
 if not M.observed and M.firstInspect then
  local seen=M.firstInspect;M.observed=true
  out('HOOKS_OBSERVE',string.format('id=%s attention=%d hull=%.6f shield=%.6f ownerptr=%s generation=%d rng=%s seed=%s gt=%.9f dt=%.9f',
   seen.id,seen.result.attention,seen.result.hull,seen.result.shield,seen.result.owner_pointer,seen.result.generation,rng,sessionSeed,gameTime,dt))
 end
 out('STATEHASH',string.format('run=%s session=%s frame=%d h=%s identity=%s pose=%s health=%s rng=%s time=%s seed=%s ships=%d weak=%d complete=1 gt=%.9f dt=%.9f',
  esc(settings.run),esc(settings.session),frame,combined,identityHash,poseHash,healthHash,rng,timeHash,sessionSeed,#rows,weak,gameTime,dt))
 return true
end
local function incomplete(frame,gameTime,dt,reason,sessionSeed)
 out('STATEHASH',string.format('run=%s session=%s frame=%d complete=0 reason=%s seed=%s gt=%.9f dt=%.9f',
  esc(settings.run),esc(settings.session),frame,esc(reason),sessionSeed,gameTime,dt))
end
local function safeSample(frame,gameTime,dt,rng,sessionSeed)
 if M.failed then return end
 local ok,result,reason=pcall(sample,frame,gameTime,dt,rng,sessionSeed)
 if not ok then disabled(result);return end
 if not result then incomplete(frame,gameTime,dt,reason,sessionSeed)end
end
function M.start()
 if M.failed or M.owner then return end
 local owner,reason=native.start(safeSample,settings.cadence)
 if not owner and reason=='loading'then return end
 if not owner then disabled(reason or 'registration refused');return end
 M.owner=owner
 out('HOOKS_READY',string.format('slot=12 cadence=%d run=%s session=%s maxships=%d',settings.cadence,esc(settings.run),esc(settings.session),settings.maxShips))
end
RegisterEvent('X4LAN.Loaded',M.start)
local nextProbe=0
SetScript('onUpdate',function()
 if M.failed then return end
 if M.owner then
  if not M.owner:active()then disabled('observer disarmed');return end
  local now=GetCurRealTime()
  if now>=(M.nextStatus or 0)then out('HOOKS_STATUS','frames='..tostring(M.owner:frames()));M.nextStatus=now+5 end
  return
 end
 if not C.IsPlayerValid()then return end
 local now=GetCurRealTime()
 if now>=nextProbe then AddUITriggeredEvent('X4LAN','Ready');nextProbe=now+1 end
end)
