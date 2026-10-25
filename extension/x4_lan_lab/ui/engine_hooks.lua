-- Default-enabled STATEHASH recorder and Stage 1 MoveWorker reseed; no network changes.
local cfg=X4LAN_CONFIG
if not cfg or cfg.engine_hooks~=true then return end
local ffi=require('ffi');local C=ffi.C
local M={failed=false,observed=false,cost={captures=0,completed=0,failed=0,totalMs=0,maxMs=0}};X4LAN_ENGINE_HOOKS=M
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
local function number(value,low,high,default)
 if value==nil then return default end
 assert(type(value)=='number' and value==value and value>=low and value<=high,'engine hash setting outside bounds')
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
 settings={cadence=integer(cfg.engine_hash_cadence,1,3600,30),maxShips=integer(cfg.engine_hash_max_ships,1,20000,4000),
  budgetMs=number(cfg.engine_hash_budget_ms,0.1,1000,8),
  sectors=cfg.engine_hash_sectors or {'cluster_14_sector001_macro','cluster_01_sector001_macro'},
  run=cfg.engine_hash_run or 'default',session=cfg.build or 'disabled'}
 assert(atom(settings.run) and #settings.run<=32,'invalid engine hash run')
 assert(type(settings.sectors)=='table' and #settings.sectors<=16,'engine_hash_sectors must have at most 16 entries')
 local wanted={};for _,sector in ipairs(settings.sectors)do assert(atom(sector),'invalid engine hash sector');wanted[sector]=true end
 settings.wanted=wanted
 local load,message=package.loadlib(X4LAN_HOOKS_PATH or './extensions/x4_lan_lab/native/x4lan_hooks_64.dll','luaopen_x4lan_hooks')
 assert(load,message);native=load();assert(native and native.start and native.inspect and native.walltime and native.hash,'invalid hooks module')
end)
if not ok then disabled(err);return end

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
 local started=native.walltime();local rows={};local stats={scanned=0,ships=0,captureMs=0,hashMs=0}
 local function elapsed()return(native.walltime()-started)*1000 end
 local function fail(reason)stats.ships=#rows;stats.captureMs=elapsed();return nil,reason,stats end
 local factions=ffi.new('const char*[256]');local count=tonumber(C.GetAllFactions(factions,256,true))
 if count<=0 or count>=256 then return fail('faction enumeration incomplete')end
 local sectors={};local total=0;local weak=0
 for i=0,count-1 do
  local faction=ffi.string(factions[i]);local amount=tonumber(C.GetNumAllFactionShips(faction))
  if amount>250000 then return fail('faction fleet limit exceeded')end
  local buffer=ffi.new('UniverseID[?]',amount+64)
  local read=tonumber(C.GetAllFactionShips(buffer,amount+64,faction))
  if read>=amount+64 or tonumber(C.GetNumAllFactionShips(faction))>read then return fail('fleet changed during capture')end
  for j=0,read-1 do
   local object=buffer[j];stats.scanned=stats.scanned+1
   if C.IsComponentOperational(object) then
    local sector=sectorName(object,sectors)
    if sector and settings.wanted[sector] then
     total=total+1;if total>settings.maxShips then return fail('ship limit exceeded')end
     local macro,owner,code=GetComponentData(luaId(object),'macro','owner','idcode')
     if owner~='player' then
      local id,isWeak=identity(object,macro,owner,code);if isWeak then weak=weak+1 end
      local details,problem=inspect(object,M.firstInspect);if not details then return fail(problem)end
      local p=C.GetObjectPositionInSector(object);local degrees=180/math.pi
      rows[#rows+1]={id=id,macro=macro,owner=owner,sector=sector,
       pose=table.concat({quantize(p.x,100),quantize(p.y,100),quantize(p.z,100),
        quantize(p.yaw*degrees,100),quantize(p.pitch*degrees,100),quantize(p.roll*degrees,100)},','),
       hull=string.format('%.6f',details.hull),shield=string.format('%.6f',details.shield)}
     end
    end
   end
   if stats.scanned%16==0 and elapsed()>settings.budgetMs then return fail('capture budget exceeded')end
  end
  if elapsed()>settings.budgetMs then return fail('capture budget exceeded')end
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
 stats.ships=#rows;stats.captureMs=elapsed()
 if stats.captureMs>settings.budgetMs then return nil,'capture budget exceeded',stats end
 local hashStarted=native.walltime()
 local identityHash,poseHash,healthHash=native.hash(identities),native.hash(poses),native.hash(health)
 local timeHash=native.hash({string.format('%.9f',gameTime)})
 local combined=native.hash({identityHash,poseHash,healthHash,rng,timeHash})
 stats.hashMs=(native.walltime()-hashStarted)*1000
 if stats.captureMs+stats.hashMs>settings.budgetMs then return nil,'hash budget exceeded',stats end
 if not M.observed and M.firstInspect then
  local seen=M.firstInspect;M.observed=true
  out('HOOKS_OBSERVE',string.format('id=%s attention=%d hull=%.6f shield=%.6f ownerptr=%s generation=%d rng=%s seed=%s gt=%.9f dt=%.9f',
   seen.id,seen.result.attention,seen.result.hull,seen.result.shield,seen.result.owner_pointer,seen.result.generation,rng,sessionSeed,gameTime,dt))
 end
 out('STATEHASH',string.format('run=%s session=%s frame=%d h=%s identity=%s pose=%s health=%s rng=%s time=%s seed=%s ships=%d weak=%d fresh=1 complete=1 gt=%.9f dt=%.9f',
  esc(settings.run),esc(settings.session),frame,combined,identityHash,poseHash,healthHash,rng,timeHash,sessionSeed,#rows,weak,gameTime,dt))
 return true,nil,stats
end
local function incomplete(frame,gameTime,dt,reason,sessionSeed,stats)
 out('STATEHASH',string.format('run=%s session=%s frame=%d scanned=%d ships=%d fresh=0 complete=0 reason=%s seed=%s gt=%.9f dt=%.9f',
  esc(settings.run),esc(settings.session),frame,stats.scanned,stats.ships,esc(reason),sessionSeed,gameTime,dt))
end
local function hashCost(frame,complete,stats,started)
 local totalMs=(native.walltime()-started)*1000;local cost=M.cost
 cost.captures=cost.captures+1;cost.completed=cost.completed+(complete and 1 or 0);cost.failed=cost.failed+(complete and 0 or 1)
 cost.totalMs=cost.totalMs+totalMs;cost.maxMs=math.max(cost.maxMs,totalMs)
 out('HASHCOST',string.format('frame=%d fresh=%d complete=%d scanned=%d ships=%d capture_ms=%.3f hash_ms=%.3f total_ms=%.3f max_ms=%.3f cumulative_ms=%.3f captures=%d completed=%d failed=%d budget_ms=%.3f cadence=%d',
  frame,complete and 1 or 0,complete and 1 or 0,stats.scanned,stats.ships,stats.captureMs,stats.hashMs,totalMs,cost.maxMs,cost.totalMs,cost.captures,cost.completed,cost.failed,settings.budgetMs,settings.cadence))
end
local function safeSample(frame,gameTime,dt,rng,sessionSeed)
 if M.failed then return end
 local started=native.walltime()
 local ok,result,reason,stats=pcall(sample,frame,gameTime,dt,rng,sessionSeed)
 if not ok then disabled(result);return end
 if not result then incomplete(frame,gameTime,dt,reason,sessionSeed,stats)end
 hashCost(frame,result and true or false,stats,started)
end
function M.start()
 if M.failed or M.owner then return end
 local owner,reason=native.start(safeSample,settings.cadence)
 if not owner and reason=='loading'then return end
 if not owner then disabled(reason or 'registration refused');return end
 M.owner=owner
 out('HOOKS_READY',string.format('slot=12 cadence=%d run=%s session=%s maxships=%d budget_ms=%.3f',settings.cadence,esc(settings.run),esc(settings.session),settings.maxShips,settings.budgetMs))
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
