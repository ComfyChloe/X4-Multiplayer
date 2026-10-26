-- Attention load test driver: TEST INSTALLS ONLY (dev/install_attention_load_test.py).
-- Registers NPC ships in distinct busy sectors as remote attention proxies through the
-- engine hooks adapter (X4LAN_ATTENTION_LOAD.sources), steps the proxy count through
-- attention_load_steps and logs ATTNLOAD lines next to the FRAMEPROBE sim-cost lines.
-- It reads ships through the read-only helper md/x4lan_attnload.xml and never creates,
-- moves or changes objects. Inert unless X4LAN_CONFIG.attention_load_test == true.
local cfg=X4LAN_CONFIG
if not cfg or cfg.attention_load_test~=true then return end
if X4LAN_ATTENTION_LOAD then return end
local ffi=require('ffi');local C=ffi.C
local function declare(name,definition)if not pcall(ffi.typeof,name)then pcall(ffi.cdef,definition)end end
declare('UniverseID','typedef uint64_t UniverseID;')
for _,fn in ipairs({'bool IsComponentOperational(UniverseID);','UniverseID GetContextByClass(UniverseID,const char*,bool);',
 'UniverseID GetPlayerID(void);','UniverseID GetPlayerOccupiedShipID(void);','bool IsComponentClass(UniverseID,const char*);'})do pcall(ffi.cdef,fn)end
local function optional(name)local ok,fn=pcall(function()return C[name]end);return ok and fn~=nil end

local MAX_PROXIES=8
local D={sources={},phase='wait',seq=0,queue={},sectors={},ranked={},slots={},used={},bad={},
 timeouts=0,stepIndex=0,census=nil,refusals=0}
X4LAN_ATTENTION_LOAD=D

local function out(text)DebugError('[X4LAN] ATTNLOAD '..text)end
local function key(id)return(tostring(id):gsub('ULL$',''))end
local function luaId(id)return ConvertStringToLuaID(key(id))end
local function list(value,default)return type(value)=='table' and value or default end
local function num(value,default)return type(value)=='number' and value or default end

-- Configuration (all optional; defaults give a 60 s baseline then 8 sectors for 180 s).
local settings={
 steps=list(cfg.attention_load_steps,{0,8}),
 durations=list(cfg.attention_load_durations,nil),
 stepSeconds=num(cfg.attention_load_step_seconds,60),
 warmup=num(cfg.attention_load_warmup,20),
 tick=num(cfg.attention_load_tick,1),
 censusSeconds=num(cfg.attention_load_census_seconds,20),
 maxObjects=num(cfg.attention_load_max_objects,600),
 spares=num(cfg.attention_load_spares,4),
 timeout=num(cfg.attention_load_timeout,5),
 prefer=cfg.attention_load_prefer or 'docked',
 sectors=list(cfg.attention_load_sectors,nil),
 exclude=list(cfg.attention_load_exclude,{}),
}
if cfg.attention_load_steps==nil and cfg.attention_load_durations==nil and cfg.attention_load_step_seconds==nil then
 settings.durations={60,180} -- default: 60 s baseline, then 8 sectors for 3 minutes
end
D.settings=settings

local function fail(reason)
 if D.phase=='failed' then return end
 D.phase='failed';D.sources={};out('error reason='..tostring(reason):gsub('%s','_'))
end
local function validate()
 local maxN=0
 for i,n in ipairs(settings.steps)do
  if type(n)~='number' or n~=math.floor(n) or n<0 or n>MAX_PROXIES then return false,'invalid step '..tostring(n)end
  maxN=math.max(maxN,n)
  local d=settings.durations and settings.durations[i] or settings.stepSeconds
  if type(d)~='number' or d<5 or d>3600 then return false,'invalid duration for step '..i end
 end
 if #settings.steps==0 then return false,'no steps' end
 D.maxN=maxN;return true
end
local okConfig,configError=validate()

local function now()return GetCurRealTime()end
local function gameTime()return GetCurTime and GetCurTime() or 0 end
local function duration(i)return settings.durations and settings.durations[i] or settings.stepSeconds end
local function hooks()return rawget(_G,'X4LAN_ENGINE_HOOKS')end

local macroCache={}
local function sectorOf(id)
 local sector=C.GetContextByClass(id,'sector',false)
 if not sector or sector==0 then return nil,nil end
 local k=key(sector);local macro=macroCache[k]
 if not macro then macro=GetComponentData(luaId(sector),'macro');macroCache[k]=macro end
 return k,macro
end
local function playerSectorMacro()
 if not optional('GetPlayerID')then return D.playerMacro end
 local player=C.GetPlayerID();if not player or player==0 then return D.playerMacro end
 local _,macro=sectorOf(player);return macro or D.playerMacro
end
local function operational(id)return id and C.IsComponentOperational(id)end
local function inSector(id,sectorKey)
 if not operational(id)then return false,'dead' end
 local k=sectorOf(id);if k~=sectorKey then return false,'left' end
 return true
end

-- Request queue: one MD request outstanding at a time; scan_begin/scan_end bracket the
-- frames whose FRAMEPROBE windows the report excludes.
local function pump(t)
 if D.pending or #D.queue==0 then return end
 local request=table.remove(D.queue,1)
 D.seq=D.seq+1;request.seq=D.seq;request.sentAt=t;D.pending=request
 out(string.format('scan_begin seq=%d kind=%s sector=%s',request.seq,request.kind,request.macro or '-'))
 if request.kind=='Rank' then AddUITriggeredEvent('X4LAN_AttnLoad','Rank',request.seq)
 else AddUITriggeredEvent('X4LAN_AttnLoad','Sector',{seq=request.seq,macro=request.macro,max=settings.maxObjects})end
end
local function enqueue(kind,macro,role)
 for _,r in ipairs(D.queue)do if r.kind==kind and r.macro==macro then return end end
 if D.pending and D.pending.kind==kind and D.pending.macro==macro then return end
 D.queue[#D.queue+1]={kind=kind,macro=macro,role=role}
end
local function finish(seq)
 local request=D.pending
 if not request or request.seq~=seq then return nil end
 D.pending=nil
 out(string.format('scan_end seq=%d kind=%s sector=%s ms=%.1f',seq,request.kind,request.macro or '-',(now()-request.sentAt)*1000))
 return request
end

-- Ranking: busy = ships + 10 * stations; the player's sector and excluded macros are skipped.
function D.onRank(param)
 local seq,macro,stations,ships,isPlayer=tostring(param):match('^(%d+)|([^|]+)|(%d+)|(%d+)|(%d)$')
 if not seq or not D.pending or tonumber(seq)~=D.pending.seq then return end
 D.rankRows=D.rankRows or {}
 D.rankRows[#D.rankRows+1]={macro=macro,stations=tonumber(stations),ships=tonumber(ships),player=isPlayer=='1'}
 if isPlayer=='1' then D.playerMacro=macro end
end
local function rankSectors(rows)
 local excluded={};for _,m in ipairs(settings.exclude)do excluded[m]=true end
 local ranked={}
 if settings.sectors then
  for _,m in ipairs(settings.sectors)do if m~=D.playerMacro and not excluded[m]then ranked[#ranked+1]=m end end
  return ranked
 end
 local keep={}
 for _,row in ipairs(rows)do
  if not row.player and not excluded[row.macro] and row.ships>0 then row.score=row.ships+10*row.stations;keep[#keep+1]=row end
 end
 table.sort(keep,function(a,b)if a.score~=b.score then return a.score>b.score end return a.macro<b.macro end)
 for _,row in ipairs(keep)do ranked[#ranked+1]=row.macro end
 local top={}
 for i=1,math.min(12,#keep)do top[#top+1]=string.format('%s:%d',keep[i].macro,keep[i].score)end
 out(string.format('ranked sectors=%d player=%s top=%s',#keep,D.playerMacro or '-',table.concat(top,',')))
 return ranked
end
function D.onRankEnd(param)
 local seq=tonumber(tostring(param):match('^(%d+)|'))
 if not finish(seq)then return end
 D.ranked=rankSectors(D.rankRows or {});D.rankRows=nil
 if #D.ranked==0 then fail('no candidate sectors');return end
 for i=1,math.min(#D.ranked,D.maxN+settings.spares)do enqueue('Sector',D.ranked[i],'prefetch')end
 D.phase='fetch'
end

-- Sector stream: Begin, SectorObject, Station*, Ship*, End.
function D.onBegin(param)
 local seq,macro,ships,stations,isPlayer=tostring(param):match('^(%d+)|([^|]+)|(%d+)|(%d+)|(%d)$')
 if not seq or not D.pending or tonumber(seq)~=D.pending.seq then return end
 D.current={macro=macro,ships={},stations={},total=tonumber(ships),stationCount=tonumber(stations),player=isPlayer=='1'}
 if isPlayer=='1' then D.playerMacro=macro end
end
function D.onSectorObject(object)if D.current then D.current.id=key(ConvertIDTo64Bit(object))end end
function D.onStation(object)if D.current then local s=D.current.stations;s[#s+1]=ConvertIDTo64Bit(object)end end
function D.onShip(object)if D.current then local s=D.current.ships;s[#s+1]=ConvertIDTo64Bit(object)end end

local function candidateList(sector)
 local docked,free={},{}
 local playerShip=optional('GetPlayerOccupiedShipID') and key(C.GetPlayerOccupiedShipID()) or nil
 local classCheck=optional('IsComponentClass')
 for _,id in ipairs(sector.ships)do
  local k=key(id)
  if not D.bad[k] and k~=playerShip and operational(id) and not(classCheck and C.IsComponentClass(id,'ship_xs'))then
   local owner,isDocked=GetComponentData(luaId(id),'owner','isdocked')
   if owner~='player' then
    if settings.prefer=='any' or not isDocked then free[#free+1]=id else docked[#docked+1]=id end
   end
  end
 end
 local first,second=docked,free
 if settings.prefer=='undocked' then first,second=free,docked end
 local result={}
 for _,id in ipairs(first)do result[#result+1]=id end
 for _,id in ipairs(second)do result[#result+1]=id end
 return result
end

local function levelHistogram(sector)
 local h=hooks();local histogram={};for level=0,11 do histogram[level]=0 end
 local hi,read=0,0
 if not(h and h.attentionLevel)then return nil end
 local function count(id)
  local value=h.attentionLevel(id);value=tonumber(value)
  if value and value>=0 and value<=11 then histogram[value]=histogram[value]+1;read=read+1;if value>=9 then hi=hi+1 end end
 end
 for _,id in ipairs(sector.ships)do count(id)end
 for _,id in ipairs(sector.stations)do count(id)end
 local levels={};for level=0,11 do levels[#levels+1]=histogram[level]end
 local sectorLevel=sector.id and tonumber((h.attentionLevel(sector.id))) or -1
 return hi,read,table.concat(levels,'.'),sectorLevel
end

function D.onEnd(param)
 local seq,macro,status=tostring(param):match('^(%d+)|([^|]+)|(%a+)$')
 local request=finish(tonumber(seq))
 if not request then D.current=nil;return end
 local sector=D.current;D.current=nil
 if status~='ok' or not sector then
  out('sector_missing sector='..tostring(macro))
  D.sectors[macro]={macro=macro,missing=true,candidates={},ships={},stations={}}
 else
  if not sector.id and sector.ships[1] then sector.id=sectorOf(sector.ships[1])end
  sector.candidates=candidateList(sector);sector.updated=now()
  D.sectors[macro]=sector
  if request.role and request.role~='prefetch' then
   local hi,read,levels,sectorLevel=levelHistogram(sector)
   if hi then
    out(string.format('census round=%d step=%d role=%s sector=%s ships=%d stations=%d read=%d hi9=%d sector_level=%d levels=%s',
     D.census and D.census.round or 0,D.stepN or 0,request.role,macro,sector.total or #sector.ships,sector.stationCount or #sector.stations,read,hi,sectorLevel,levels))
    if D.census and request.role=='proxy' then D.census.hi=D.census.hi+hi;D.census.sectors=D.census.sectors+1 end
    if D.census and request.role=='player' then D.census.playerHi=hi end
   end
  end
 end
 for _,slot in ipairs(D.slots)do if slot.waiting==macro then slot.waiting=nil;slot.refreshed=true end end
end

-- Slots: one proxy per distinct sector.
local function sourcesText()
 local ids={};for _,slot in ipairs(D.slots)do if slot.proxy then ids[#ids+1]=key(slot.proxy)end end
 return table.concat(ids,',')
end
local function publish()
 local ids={};for _,slot in ipairs(D.slots)do if slot.proxy then ids[#ids+1]=slot.proxy end end
 D.sources=ids
end
local function takeCandidate(macro)
 local sector=D.sectors[macro];if not sector or not sector.id then return nil end
 for _,id in ipairs(sector.candidates)do
  local k=key(id)
  if not D.used[k] and not D.bad[k] and inSector(id,sector.id)then return id end
 end
 return nil
end
local function assign(slot,id)
 if slot.proxy then D.used[key(slot.proxy)]=nil end
 slot.proxy=id;if id then D.used[key(id)]=true end
end
local function usedSectors()
 local set={};for _,slot in ipairs(D.slots)do set[slot.macro]=true end;return set
end
local function nextFreeSector()
 local set=usedSectors()
 for _,macro in ipairs(D.ranked)do
  local sector=D.sectors[macro]
  if not set[macro] and macro~=D.playerMacro and not(sector and sector.missing) and (not sector or #sector.candidates>0 or not sector.updated)then return macro end
 end
end

local function beginStep(index,t)
 D.stepIndex=index;D.stepStart=t;local n=settings.steps[index];D.stepN=n
 for _,slot in ipairs(D.slots)do assign(slot,nil)end
 D.slots={}
 for _,macro in ipairs(D.ranked)do
  if #D.slots>=n then break end
  if macro~=D.playerMacro then
   local id=takeCandidate(macro)
   if id then local slot={macro=macro,index=#D.slots+1};D.slots[#D.slots+1]=slot;assign(slot,id)end
  end
 end
 publish()
 local macros={};for _,slot in ipairs(D.slots)do macros[#macros+1]=slot.macro end
 out(string.format('step=%d index=%d sectors=%s proxies=%s duration=%d available=%d t=%.3f gt=%.3f',
  n,index,#macros>0 and table.concat(macros,',') or '-',#D.slots>0 and sourcesText() or '-',duration(index),#D.slots,t,gameTime()))
 if #D.slots<n then out(string.format('short step=%d wanted=%d available=%d',n,n,#D.slots))end
 D.nextCensus=t+math.min(5,settings.censusSeconds)
end

local function replace(slot,reason,t)
 local old=slot.proxy and key(slot.proxy) or '-'
 assign(slot,nil)
 local id=takeCandidate(slot.macro)
 if not id then
  local sector=D.sectors[slot.macro]
  if not slot.refreshed and not(sector and sector.missing)then
   slot.waiting=slot.macro;enqueue('Sector',slot.macro,'refresh')
   out(string.format('replace slot=%d sector=%s old=%s new=- reason=%s pending=refresh',slot.index,slot.macro,old,reason))
   publish();return
  end
  local macro=nextFreeSector()
  if macro then
   out(string.format('resector slot=%d from=%s to=%s',slot.index,slot.macro,macro))
   slot.macro=macro;slot.refreshed=nil
   id=takeCandidate(macro)
   if not id then slot.waiting=macro;enqueue('Sector',macro,'refresh');publish()
    out(string.format('replace slot=%d sector=%s old=%s new=- reason=%s pending=refresh',slot.index,slot.macro,old,reason));return end
  end
 end
 slot.refreshed=nil
 assign(slot,id);publish()
 out(string.format('replace slot=%d sector=%s old=%s new=%s reason=%s',slot.index,slot.macro,old,id and key(id) or '-',reason))
end

local function checkRefusal()
 local h=hooks();if not h then return end
 local refusals=h.refusals or 0
 if refusals==D.refusals then return end
 D.refusals=refusals
 local applied={};for id in tostring(h.attentionSources or ''):gmatch('[^,]+')do applied[id]=true end
 for _,slot in ipairs(D.slots)do
  if slot.proxy and not applied[key(slot.proxy)]then
   D.bad[key(slot.proxy)]=true;slot.badPending=true
  end
 end
end

local function tick(t)
 checkRefusal()
 for _,slot in ipairs(D.slots)do
  if slot.badPending then slot.badPending=nil;replace(slot,'refused',t)
  elseif slot.proxy then
   local sector=D.sectors[slot.macro]
   local ok,reason=inSector(slot.proxy,sector and sector.id)
   if not ok then replace(slot,reason,t)end
  elseif not slot.waiting then replace(slot,'empty',t)end
 end
 local parts={}
 for _,slot in ipairs(D.slots)do
  local where='-'
  if slot.proxy and operational(slot.proxy)then local _,m=sectorOf(slot.proxy);where=m or '-' end
  parts[#parts+1]=string.format('%d:%s@%s',slot.index,slot.proxy and key(slot.proxy) or '-',where)
 end
 local h=hooks();local counters=h and h.attentionCounters and h.attentionCounters() or nil
 out(string.format('tick step=%d t=%.3f gt=%.3f proxies=%s attn_sources=%d attn_resolved=%d attn_raises=%d',
  D.stepN or 0,t,gameTime(),#parts>0 and table.concat(parts,',') or '-',
  counters and counters.sources or -1,counters and counters.resolved or -1,counters and counters.raises or -1))
end

local function startCensus(t)
 D.round=(D.round or 0)+1
 D.census={round=D.round,hi=0,sectors=0,playerHi=-1,open=true}
 out(string.format('census_begin round=%d step=%d',D.round,D.stepN or 0))
 local planned={}
 if (D.stepN or 0)==0 then
  -- Baseline: the sectors the next non-zero step will use, without proxies.
  local want=D.maxN;for _,macro in ipairs(D.ranked)do if #planned>=want then break end
   if macro~=D.playerMacro then planned[#planned+1]=macro end end
  for _,macro in ipairs(planned)do enqueue('Sector',macro,'baseline')end
 else
  for _,slot in ipairs(D.slots)do enqueue('Sector',slot.macro,'proxy')end
 end
 local player=playerSectorMacro()
 if player then enqueue('Sector',player,'player')end
end
local function endCensus()
 local c=D.census;if not c or not c.open then return end
 c.open=false
 out(string.format('census_total round=%d step=%d proxy_sectors=%d hi9=%d player_hi9=%d',c.round,D.stepN or 0,c.sectors,c.hi,c.playerHi))
end

function D.update(t)
 if D.phase=='failed' or D.phase=='done' then return end
 if not okConfig then fail(configError);return end
 local h=hooks()
 if not h then if t>(D.bootAt or t)+30 then fail('hooks_adapter_missing')end;D.bootAt=D.bootAt or t;return end
 if h.failed then fail('hooks_disabled');return end
 if D.pending and t-D.pending.sentAt>settings.timeout then
  out(string.format('scan_timeout seq=%d kind=%s sector=%s',D.pending.seq,D.pending.kind,D.pending.macro or '-'))
  local request=D.pending;D.pending=nil;D.current=nil;D.timeouts=D.timeouts+1
  if D.timeouts>=3 then fail('md_helper_unavailable');return end
  request.seq=nil;table.insert(D.queue,1,request)
 end
 if D.phase=='wait' then
  if not h.owner then return end
  if not D.armedAt then
   D.armedAt=t
   out(string.format('armed warmup=%d steps=%s durations=%s prefer=%s',settings.warmup,table.concat(settings.steps,','),
    (function()local d={};for i=1,#settings.steps do d[i]=duration(i)end;return table.concat(d,',')end)(),settings.prefer))
  end
  if t<D.armedAt+settings.warmup then return end
  D.phase='rank';enqueue('Rank',nil,'rank')
 end
 pump(t)
 if D.phase=='fetch' then
  if D.pending or #D.queue>0 then return end
  D.phase='run';beginStep(1,t);D.nextTick=t;return
 end
 if D.phase~='run' then return end
 if t>=D.stepStart+duration(D.stepIndex)then
  if D.census and D.census.open and not D.pending and #D.queue==0 then endCensus()end
  if D.stepIndex>=#settings.steps then
   for _,slot in ipairs(D.slots)do assign(slot,nil)end
   D.slots={};publish();D.phase='done'
   out(string.format('done t=%.3f gt=%.3f',t,gameTime()));return
  end
  if D.census then D.census.open=false end
  beginStep(D.stepIndex+1,t);D.nextTick=t
 end
 if t>=(D.nextTick or 0)then D.nextTick=t+settings.tick;tick(t)end
 if D.census and D.census.open and not D.pending and #D.queue==0 then endCensus()end
 if settings.censusSeconds>0 and t>=(D.nextCensus or math.huge) and not D.pending and #D.queue==0 then
  D.nextCensus=t+settings.censusSeconds;startCensus(t)
 end
end

local function guarded(name,fn)
 return function(_,param)
  local ok,err=pcall(fn,param)
  if not ok then fail(name..':'..tostring(err))end
 end
end
RegisterEvent('X4LAN_AttnLoad.Rank',guarded('rank',D.onRank))
RegisterEvent('X4LAN_AttnLoad.RankEnd',guarded('rankend',D.onRankEnd))
RegisterEvent('X4LAN_AttnLoad.Begin',guarded('begin',D.onBegin))
RegisterEvent('X4LAN_AttnLoad.SectorObject',guarded('sectorobject',D.onSectorObject))
RegisterEvent('X4LAN_AttnLoad.Station',guarded('station',D.onStation))
RegisterEvent('X4LAN_AttnLoad.Ship',guarded('ship',D.onShip))
RegisterEvent('X4LAN_AttnLoad.End',guarded('end',D.onEnd))
SetScript('onUpdate',function()
 local ok,err=pcall(D.update,GetCurRealTime())
 if not ok then fail('update:'..tostring(err))end
end)
out('loaded')
