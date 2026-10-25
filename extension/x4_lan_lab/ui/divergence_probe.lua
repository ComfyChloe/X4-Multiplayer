-- Opt-in, LOG-ONLY divergence recorder (DIVERGENCE-PROBE.txt). Reads native ship state
-- and writes bounded "[X4LAN] DIV1 ..." lines; never writes game objects, sends no
-- packets and changes no authority. md/x4lan_divergence.xml schedules the samples on
-- the saved game clock and enumerates the sectors. Inert unless divergence_probe=true.
local cfg=X4LAN_CONFIG
if not cfg or cfg.divergence_probe~=true then return end
local ffi=require('ffi');local C=ffi.C
local D={failed=false,errors=0,firstSeen={},seenCount=0,seenFull=false,tick=nil,sector=nil}
X4LAN_DIVERGENCE=D
local MAX_ERRORS=20
local MAX_SEEN=50000
local DEFAULT_SECTORS={'cluster_14_sector001_macro','cluster_01_sector001_macro'}
local function out(text) DebugError('[X4LAN] DIV1 '..text) end
local function esc(v)
 if v==nil then return '' end
 v=tostring(v);if #v>64 then v=v:sub(1,64) end
 return (v:gsub('[^%w_%.:%-]',function(c) return string.format('%%%02X',c:byte()) end))
end
local function num(v,fmt)
 v=tonumber(v);if not v or v~=v or v==math.huge or v==-math.huge then return '' end
 return string.format(fmt or '%.2f',v)
end
local function atom(v) return type(v)=='string' and #v>=1 and #v<=128 and v:match('^[%w_%.%-]+$')~=nil end
local function integer(v,low,high,default)
 if v==nil then return default end
 assert(type(v)=='number' and v==math.floor(v) and v>=low and v<=high,'divergence setting outside '..low..'..'..high)
 return v
end
local function key(id) return (tostring(id):gsub('ULL$','')) end
local function luaId(id) return ConvertStringToLuaID(key(id)) end
local function declare(name,definition) if not pcall(ffi.typeof,name) then pcall(ffi.cdef,definition) end end
local function stop(reason)
 D.failed=true;out('STOPPED reason='..esc(reason))
end
local settings
local ok,err=pcall(function()
 local profile=cfg.test_profile or '45841'
 assert(profile=='45841','divergence probe requires isolated test profile 45841')
 declare('UniverseID','typedef uint64_t UniverseID;')
 declare('UIPosRot','typedef struct { float x, y, z, yaw, pitch, roll; } UIPosRot;')
 declare('Order','typedef struct { size_t queueidx; const char* state; const char* statename; const char* orderdef; size_t actualparams; bool enabled; bool isinfinite; bool issyncpointreached; bool istemporder; } Order;')
 for _,fn in ipairs({'const char* GetSaveFolderPath(void);','bool IsComponentOperational(UniverseID);',
  'UniverseID GetContextByClass(UniverseID,const char*,bool);','UIPosRot GetObjectPositionInSector(UniverseID);',
  'bool IsComponentClass(UniverseID,const char*);','uint32_t GetNumOrders(UniverseID);',
  'uint32_t GetOrders(Order*,uint32_t,UniverseID);','bool GetDefaultOrder(Order*,UniverseID);',
  'double GetCurrentGameTime(void);'}) do pcall(ffi.cdef,fn) end
 for _,name in ipairs({'GetSaveFolderPath','IsComponentOperational','GetContextByClass','GetObjectPositionInSector'}) do
  assert(pcall(function() return C[name] end),'missing game export: '..name)
 end
 local path=ffi.string(C.GetSaveFolderPath()):gsub('\\','/'):gsub('/+$','')
 assert(path:sub(-#'/45841/save')=='/45841/save','inactive outside isolated test profile 45841')
 local sectors=cfg.divergence_sectors or DEFAULT_SECTORS
 assert(type(sectors)=='table' and #sectors<=16,'divergence_sectors must be a list of at most 16 sector macros')
 for _,s in ipairs(sectors) do assert(atom(s),'invalid sector macro in divergence_sectors') end
 local label=cfg.divergence_label or cfg.slot or 'none'
 assert(atom(label) and #label<=16,'invalid divergence_label')
 settings={interval=integer(cfg.divergence_interval,5,3600,30),maxShips=integer(cfg.divergence_max_ships,1,2000,400),
  maxLines=integer(cfg.divergence_max_lines,1,10000,2000),sectors=sectors,label=label}
end)
if not ok then out('STOPPED reason='..esc(err));return end
D.settings=settings
local function optional(name) local present,f=pcall(function() return C[name] end);return present and f~=nil end
local hasOrders=optional('GetNumOrders') and optional('GetOrders') and optional('GetDefaultOrder')
local hasClass=optional('IsComponentClass')
local hasGameTime=optional('GetCurrentGameTime')
local function gameTime() if hasGameTime then return num(C.GetCurrentGameTime(),'%.3f') end return '' end
local function guarded(name,fn)
 return function(...)
  if D.failed then return end
  local done,problem=pcall(fn,...)
  if not done then
   D.errors=D.errors+1;if D.tick then D.tick.errors=D.tick.errors+1 end
   if D.sector then D.sector.errors=D.sector.errors+1 end
   out('ERROR event='..name..' reason='..esc(problem))
   if D.errors>=MAX_ERRORS then stop('too many errors') end
  end
 end
end
local function orderdef(o)
 if o.orderdef==nil then return '' end
 return ffi.string(o.orderdef)
end
local function orders(id)
 if not hasOrders then return '?','','?' end
 local current,state,default='','',''
 local n=math.min(tonumber(C.GetNumOrders(id)) or 0,32)
 if n>0 then
  local buf=ffi.new('Order[?]',n);n=tonumber(C.GetOrders(buf,n,id)) or 0
  if n>0 then current=orderdef(buf[0]);if buf[0].state~=nil then state=ffi.string(buf[0].state) end end
 end
 local d=ffi.new('Order');if C.GetDefaultOrder(d,id) then default=orderdef(d) end
 return current,state,default
end
local function sizeClass(id)
 if not hasClass then return '?' end
 for _,c in ipairs({'ship_xl','ship_l','ship_m','ship_s','ship_xs'}) do
  if C.IsComponentClass(id,c) then return c:sub(6) end
 end
 return ''
end
local function closeSector()
 local s=D.sector;if not s then return end
 D.sector=nil
 out(string.format('SECTOR n=%s sector=%s player=%s found=%s requested=%s written=%s gone=%s dropped=%s errors=%s lgt=%s',
  esc(s.n),esc(s.macro),esc(s.player),esc(s.found),esc(s.requested),s.written,s.gone,s.dropped,s.errors,gameTime()))
end
function D.config()
 out(string.format('CFG label=%s slot=%s enabled=%s world_mode=%s interval=%d maxships=%d maxlines=%d sectors=%s orders=%s lgt=%s rt=%s os=%s',
  esc(settings.label),esc(cfg.slot),esc(cfg.enabled),esc(cfg.world_mode or ''),settings.interval,settings.maxShips,settings.maxLines,
  esc(table.concat(settings.sectors,',')),hasOrders and 1 or 0,gameTime(),num(GetCurRealTime(),'%.3f'),esc(ffi.os)))
end
function D.hello(_,age)
 age=tonumber(age);assert(age and age>=0,'invalid MD game age')
 for _,s in ipairs(settings.sectors) do AddUITriggeredEvent('X4LAN_Divergence','Sector',s) end
 -- Every probe samples the same multiples of the interval on the SAVED game clock,
 -- so two games loaded from one save sample at equal scheduled game times.
 local first=(math.floor(age/settings.interval)+1)*settings.interval
 AddUITriggeredEvent('X4LAN_Divergence','Start',{interval=settings.interval,first=first,maxships=settings.maxShips})
 out('START age='..num(age,'%.3f')..' first='..first)
end
function D.missing(_,macro) out('MISSING sector='..esc(macro)) end
function D.tickStart(_,payload)
 closeSector()
 local n,gt,age,wall,psector=tostring(payload):match('^(%d+)|([^|]*)|([^|]*)|([^|]*)|([^|]*)$')
 assert(n,'invalid tick payload')
 D.tick={n=n,lines=0,dropped=0,errors=0,gone=0,ships=0}
 out(string.format('TICK n=%s gt=%s age=%s lgt=%s rt=%s wall=%s psector=%s',
  n,num(gt,'%.3f'),num(age,'%.3f'),gameTime(),num(GetCurRealTime(),'%.3f'),esc(wall),esc(psector)))
end
function D.sectorStart(_,payload)
 closeSector()
 local n,macro,found,requested,player=tostring(payload):match('^(%d+)|([^|]*)|(%d+)|(%d+)|([01])$')
 assert(n,'invalid sector payload')
 D.sector={n=n,macro=macro,found=found,requested=requested,player=player,written=0,gone=0,dropped=0,errors=0}
end
function D.ship(_,object)
 local t,s=D.tick,D.sector
 assert(t and s,'ship sample without tick/sector context')
 if t.lines>=settings.maxLines then t.dropped=t.dropped+1;s.dropped=s.dropped+1;return end
 local id=ConvertIDTo64Bit(object)
 if not C.IsComponentOperational(id) then t.gone=t.gone+1;s.gone=s.gone+1;return end
 local k=key(id)
 local macro,owner,name,code,hull,shield,docked,stored=GetComponentData(object,'macro','owner','name','idcode','hullpercent','shieldpercent','isdocked','isinternallystored')
 local sector=C.GetContextByClass(id,'sector',false)
 local actual=sector~=0 and GetComponentData(luaId(sector),'macro') or ''
 local p=C.GetObjectPositionInSector(id)
 local order,state,default=orders(id)
 local dock=''
 if docked or stored then
  local container=C.GetContextByClass(id,'container',false)
  if container~=0 then
   local dcode,dmacro=GetComponentData(luaId(container),'idcode','macro')
   dock=tostring(dcode or '')..'/'..tostring(dmacro or '')
  end
 end
 local first=D.firstSeen[k]
 if not first and not D.seenFull then
  if D.seenCount>=MAX_SEEN then D.seenFull=true;out('FIRSTSEEN_FULL limit='..MAX_SEEN)
  else first=t.n;D.firstSeen[k]=first;D.seenCount=D.seenCount+1 end
 end
 t.lines=t.lines+1;t.ships=t.ships+1;s.written=s.written+1
 out(string.format('SHIP n=%s s=%s id=%s macro=%s owner=%s code=%s name=%s cls=%s sector=%s x=%s y=%s z=%s yaw=%s pitch=%s roll=%s hull=%s shield=%s order=%s ostate=%s dorder=%s docked=%d stored=%d dock=%s first=%s',
  t.n,esc(s.macro),k,esc(macro),esc(owner),esc(code),esc(name),sizeClass(id),esc(actual),
  num(p.x,'%.1f'),num(p.y,'%.1f'),num(p.z,'%.1f'),num(p.yaw,'%.4f'),num(p.pitch,'%.4f'),num(p.roll,'%.4f'),
  num(hull),num(shield),esc(order),esc(state),esc(default),docked and 1 or 0,stored and 1 or 0,esc(dock),esc(first)))
end
function D.tickEnd(_,payload)
 closeSector()
 local t=D.tick;D.tick=nil
 local n=tostring(payload):match('^(%d+)|')
 if not t or t.n~=n then out('END n='..esc(n)..' orphan=1');return end
 out(string.format('END n=%s ships=%d dropped=%d gone=%d errors=%d lgt=%s rt=%s',
  t.n,t.ships,t.dropped,t.gone,t.errors,gameTime(),num(GetCurRealTime(),'%.3f')))
end
RegisterEvent('X4LAN_Divergence.Hello',guarded('hello',D.hello))
RegisterEvent('X4LAN_Divergence.Missing',guarded('missing',D.missing))
RegisterEvent('X4LAN_Divergence.Tick',guarded('tick',D.tickStart))
RegisterEvent('X4LAN_Divergence.Sector',guarded('sector',D.sectorStart))
RegisterEvent('X4LAN_Divergence.Ship',guarded('ship',D.ship))
RegisterEvent('X4LAN_Divergence.End',guarded('end',D.tickEnd))
D.config()
