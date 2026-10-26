-- Station build intents. Guest: the station planner's confirm becomes a BUILDREQ to the
-- authority instead of a local construction sequence. Host (authority): validate and
-- create the native build. Contract: build_protocol.lua and x4lan/build.py.
local cfg,A=X4LAN_CONFIG,X4LAN_BUBBLE_API
local function feature(reason) DebugError('[X4LAN] FEATURE station_build_intents DISABLED reason='..reason) end
if not cfg or not cfg.station_build_intents then return end
if not cfg.world_sync or not A or not A.universe then feature('requires_world_sync');return end
local B=X4LAN_BUILD_PROTOCOL
if not B then feature('protocol_missing');return end
local ffi=require('ffi');local C=ffi.C;local U=A.universe;local s=A.state
local function typedef(name,def) if not pcall(ffi.typeof,name) then ffi.cdef(def) end end
typedef('UniverseID','typedef uint64_t UniverseID;')
typedef('UIPosRot','typedef struct { float x; float y; float z; float yaw; float pitch; float roll; } UIPosRot;')
typedef('Coord3D','typedef struct { float x; float y; float z; } Coord3D;')
typedef('UIConstructionPlanEntry','typedef struct { size_t idx; const char* macroid; UniverseID componentid; UIPosRot offset; const char* connectionid; size_t predecessoridx; const char* predecessorconnectionid; bool isfixed; } UIConstructionPlanEntry;')
typedef('UIConstructionPlanEntry2','typedef struct { size_t idx; const char* macroid; UniverseID componentid; UIPosRot offset; const char* connectionid; size_t predecessoridx; const char* predecessorconnectionid; bool isfixed; uint32_t bookmarknum; } UIConstructionPlanEntry2;')
typedef('UIConstructionPlan','typedef struct { const char* name; const char* id; const char* source; bool deleteable; } UIConstructionPlan;')
typedef('UIConstructionPlanInfo','typedef struct { const char* filename; const char* name; const char* id; } UIConstructionPlanInfo;')
typedef('UILoadoutMacroData','typedef struct { const char* macro; const char* upgradetypename; size_t slot; bool optional; } UILoadoutMacroData;')
typedef('UILoadoutGroupData','typedef struct { const char* macro; const char* path; const char* group; uint32_t count; bool optional; } UILoadoutGroupData;')
typedef('UILoadoutAmmoData','typedef struct { const char* macro; uint32_t amount; bool optional; } UILoadoutAmmoData;')
typedef('UILoadoutSoftwareData','typedef struct { const char* ware; } UILoadoutSoftwareData;')
typedef('UILoadoutVirtualMacroData','typedef struct { const char* macro; bool optional; } UILoadoutVirtualMacroData;')
typedef('UILoadout','typedef struct { UILoadoutMacroData* weapons; uint32_t numweapons; UILoadoutMacroData* turrets; uint32_t numturrets; UILoadoutMacroData* shields; uint32_t numshields; UILoadoutMacroData* engines; uint32_t numengines; UILoadoutGroupData* turretgroups; uint32_t numturretgroups; UILoadoutGroupData* shieldgroups; uint32_t numshieldgroups; UILoadoutAmmoData* ammo; uint32_t numammo; UILoadoutAmmoData* units; uint32_t numunits; UILoadoutSoftwareData* software; uint32_t numsoftware; UILoadoutVirtualMacroData thruster; } UILoadout;')
typedef('UILoadoutCounts','typedef struct { uint32_t numweapons; uint32_t numturrets; uint32_t numshields; uint32_t numengines; uint32_t numturretgroups; uint32_t numshieldgroups; uint32_t numammo; uint32_t numunits; uint32_t numsoftware; } UILoadoutCounts;')
ffi.cdef[[
size_t GetNumBuildMapConstructionPlan(UniverseID holomapid, bool usestoredplan);
size_t GetBuildMapConstructionPlan2(UniverseID holomapid, UniverseID defensibleid, bool usestoredplan, UIConstructionPlanEntry2* result, uint32_t resultlen);
void GetConstructionMapItemLoadoutCounts2(UILoadoutCounts* result, UniverseID holomapid, size_t itemidx, UniverseID defensibleid, UniverseID moduleid);
void GetConstructionMapItemLoadout2(UILoadout* result, UniverseID holomapid, size_t itemidx, UniverseID defensibleid, UniverseID moduleid);
Coord3D GetBuildPlotSize(UniverseID stationid);
Coord3D GetBuildPlotCenterOffset(UniverseID stationid);
bool ExtendBuildPlot(UniverseID stationid, Coord3D poschange, Coord3D negchange, bool allowreduction);
int64_t GetBuildPlotPayment(UniverseID stationid, bool* positionchanged);
int64_t GetBuildPlotPrice(UniverseID sectorid, UIPosRot location, float x, float y, float z, const char* factionid);
Coord3D GetPaidBuildPlotSize(UniverseID stationid);
void PayBuildPlotSize(UniverseID stationid, Coord3D plotsize, Coord3D plotcenter);
UniverseID GetSectorControlStation(UniverseID sectorid);
UIPosRot GetObjectPositionInSector(UniverseID objectid);
uint32_t GetNumAllFactionStations(const char* factionid);
uint32_t GetAllFactionStations(UniverseID* result, uint32_t resultlen, const char* factionid);
uint32_t GetNumStationModules(UniverseID stationid, bool includeconstructions, bool includewrecks);
bool RemoveBuildPlot2(UniverseID stationid, bool removebuildstoragecargo);
uint32_t GetNumImportableConstructionPlans(void);
uint32_t GetImportableConstructionPlans(UIConstructionPlanInfo* result, uint32_t resultlen);
void ImportMapConstructionPlan(const char* filename, const char* id);
uint32_t GetNumConstructionPlans(void);
uint32_t GetConstructionPlans(UIConstructionPlan* result, uint32_t resultlen);
bool RemoveConstructionPlan(const char* source, const char* id);
bool IsConstructionPlanValid(const char* constructionplanid, uint32_t* numinvalidpatches);
size_t GetNumConstructionPlanInfo(const char* constructionplanid);
size_t GetConstructionPlanInfo(UIConstructionPlanEntry* result, size_t resultlen, const char* constructionplanid);
const char* GetMissingConstructionPlanBlueprints3(UniverseID containerid, UniverseID holomapid, const char* constructionplanid, bool useplanned);
bool IsComponentOperational(UniverseID);
]]
local g={jobs={},executed={},executedCount=0,counter=0};A.build=g
local function log(v) DebugError('[X4LAN] BUILDREQ '..v) end
local function notice(text) AddUITriggeredEvent('X4LAN_Build','Notice',text) end
local function send(p) if s.client and s.client.lease and s.transport then s.transport:send(p) end end
local function key(v) return (tostring(v):gsub('ULL$','')) end
local function data(object,...) return GetComponentData(ConvertStringTo64Bit(key(object)),...) end
local reasons={
 not_connected='you are not connected to the host',no_authority='no host world is available yet',
 busy='another build request is still pending',station_not_found_for_owner='the station does not belong to you on the host',
 station_ambiguous='the station could not be identified uniquely on the host',not_owner='only the owner may change this station',
 has_modules='the plot still has modules',funds='not enough money for the build budget and plot',
 missing_blueprints='blueprints are missing on the host',no_plan_inbox='the host server cannot receive construction plans',
 plan_file_missing='the host did not receive the plan file',relay_unsupported='the server does not support build requests',
 empty_plan_unsupported='removing every module is not supported yet; remove the plot or ask the host'}
local function explain(reason) return reasons[reason:match('^[^:]*')] or reason end
-- ---------------------------------------------------------------- guest side
local function stationIdentity(station)
 local code,macro,owner,sector=data(station,'idcode','macro','owner','sectorid')
 local sectorMacro=sector and GetComponentData(ConvertStringTo64Bit(key(sector)),'macro')
 if not B.atom(code) or not B.atom(macro,true) or not B.atom(sectorMacro,true) then return nil end
 return {code=code,macro=macro,owner=owner,sector=sectorMacro}
end
g.stationIdentity=stationIdentity
local function groupsOf(holomap,station,idx,component)
 local counts=ffi.new('UILoadoutCounts')
 C.GetConstructionMapItemLoadoutCounts2(counts,holomap,idx,station,component)
 local l=ffi.new('UILoadout');local keep={}
 for _,k in ipairs({'weapons','turrets','shields','engines'}) do
  l['num'..k]=counts['num'..k];keep[#keep+1]=ffi.new('UILoadoutMacroData[?]',math.max(1,counts['num'..k]));l[k]=keep[#keep]
 end
 for _,k in ipairs({'turretgroups','shieldgroups'}) do
  l['num'..k]=counts['num'..k];keep[#keep+1]=ffi.new('UILoadoutGroupData[?]',math.max(1,counts['num'..k]));l[k]=keep[#keep]
 end
 for _,k in ipairs({'ammo','units'}) do
  l['num'..k]=counts['num'..k];keep[#keep+1]=ffi.new('UILoadoutAmmoData[?]',math.max(1,counts['num'..k]));l[k]=keep[#keep]
 end
 l.numsoftware=counts.numsoftware;keep[#keep+1]=ffi.new('UILoadoutSoftwareData[?]',math.max(1,counts.numsoftware));l.software=keep[#keep]
 l.thruster.macro=''
 C.GetConstructionMapItemLoadout2(l,holomap,idx,station,component)
 if l.numweapons+l.numturrets+l.numshields+l.numengines>0 then return nil,'loadout_slot_macros_unsupported' end
 if l.numammo+l.numunits+l.numsoftware>0 then return nil,'loadout_ammo_unsupported' end
 local groups={}
 for _,pair in ipairs({{'shieldgroups','shields'},{'turretgroups','turrets'}}) do
  for i=0,l['num'..pair[1]]-1 do
   local v=l[pair[1]][i]
   if v.count>0 then groups[#groups+1]={kind=pair[2],macro=ffi.string(v.macro),path=ffi.string(v.path),group=ffi.string(v.group),exact=tonumber(v.count)} end
  end
 end
 return groups
end
-- Read the planner's holomap: the full wanted station (kept, new and moved modules).
function g.capturePlan(menu)
 local station,holomap=menu.container,menu.holomap
 if not station or station==0 or not holomap or holomap==0 then return nil,'planner_state' end
 local n=tonumber(C.GetNumBuildMapConstructionPlan(holomap,false))
 if n==0 then return nil,'empty_plan_unsupported' end
 if n>B.maxEntries then return nil,'plan_size' end
 local buf=ffi.new('UIConstructionPlanEntry2[?]',n)
 n=tonumber(C.GetBuildMapConstructionPlan2(holomap,station,false,buf,n))
 -- Plan files number entries 1..n. Engine indices are renumbered and a predecessor
 -- exists only when it names a connection on an entry of this plan.
 local entries,number={},{}
 for i=0,n-1 do number[tonumber(buf[i].idx)]=i+1 end
 for i=0,n-1 do
  local e=buf[i];local o=e.offset
  local groups,reason=groupsOf(holomap,station,e.idx,e.componentid);if not groups then return nil,reason end
  local predconnection=e.predecessorconnectionid~=nil and ffi.string(e.predecessorconnectionid) or ''
  local pred=predconnection~='' and number[tonumber(e.predecessoridx)] or 0
  entries[#entries+1]={index=i+1,macro=ffi.string(e.macroid),connection=e.connectionid~=nil and ffi.string(e.connectionid) or '',
   pred=pred,predconnection=pred~=0 and predconnection or '',
   pos={o.x,o.y,o.z},rot={math.deg(o.yaw),math.deg(o.pitch),math.deg(o.roll)},groups=groups}
 end
 local size,center=C.GetBuildPlotSize(station),C.GetBuildPlotCenterOffset(station)
 local storage=data(station,'buildstorage')
 return {station=station,entries=entries,plot={size.x,size.y,size.z,center.x,center.y,center.z},
  budget=storage and storage~=0 and tonumber(data(storage,'money')) or nil}
end
local function finish(status,reason)
 local q=g.active;if not q then return end
 g.active=nil;g.last={request=q.request,status=status,reason=reason}
 log((status=='ACCEPTED' and 'accepted' or status=='UNCERTAIN' and 'uncertain' or 'refused')..' request='..q.request..' kind='..q.kind..' station='..q.code..' reason='..reason)
 if status=='ACCEPTED' then
  notice(q.kind=='EXPAND' and 'The host accepted the station build. Construction runs on the host; new modules are not shown in your game yet.'
   or 'The host accepted the station change.')
 elseif status=='UNCERTAIN' then
  notice('Station build outcome is uncertain ('..reason..'). Do not send it again; collect diagnostics.')
 else notice('Station build refused: '..explain(reason)..'.') end
end
-- Build a request; refuse locally (nothing is sent) when it cannot be valid.
function g.submit(kind,station,plan,option)
 local function refuse(reason) log('refused request=local kind='..kind..' reason='..reason);notice('Station build refused: '..explain(reason)..'.');return false,reason end
 if g.active then return refuse('busy') end
 if not s.client or not s.client.lease then return refuse('not_connected') end
 if not U.latest then return refuse('no_authority') end
 local id=stationIdentity(station);if not id then return refuse('station_identity') end
 if not B.ownerAllowed(cfg.slot,id.owner) then return refuse('not_owner') end
 g.counter=g.counter+1
 local request=s.client.lease:sub(1,16)..'b1'..string.format('%014x',g.counter)
 local xml,parts,plot,digest='',{},'-',1
 if kind=='EXPAND' then
  local reason;xml,reason=B.encode('x4lan_'..request,plan.entries);if not xml then return refuse(reason) end
  parts=B.parts(xml);plot=B.plotText(plan.plot);digest=B.digest(xml)
  if not plot then return refuse('plot') end
  if #parts>B.maxParts then return refuse('plan_size') end
 end
 local q={request=request,kind=kind,code=id.code,host=U.latest.epoch,parts=parts,xml=xml,since=GetCurRealTime(),nextSend=0,cursor=1,
  header={'BUILDREQ','1',s.client.lease,request,U.latest.epoch,kind,id.code,id.macro,id.sector,plot,option or '-',
   tostring(kind=='EXPAND' and #plan.entries or 0),tostring(#xml),tostring(#parts),string.format('%.0f',digest)}}
 g.active=q
 log('sent request='..request..' kind='..kind..' station='..id.code..' entries='..q.header[12]..' bytes='..#xml..' parts='..#parts)
 notice('Station build sent to the host for approval.')
 return true,request
end
local function guestFrame(now)
 local q=g.active;if not q or now<q.nextSend then return end
 q.nextSend=now+.5
 if not q.replied and now-q.since>15 then
  feature('relay_unsupported');finish('REFUSED','relay_unsupported');return
 end
 if q.replied and now-q.replied>60 then finish('UNCERTAIN','relay_silent');return end
 send(table.concat(q.header,'|'))
 if q.state=='PENDING' then return end
 local list=q.missing
 if not list then
  list={};for i=1,math.min(32,#q.parts) do list[i]=(q.cursor+i-2)%#q.parts+1 end
  if #q.parts>0 then q.cursor=(q.cursor+#list-1)%#q.parts+1 end
 end
 for _,i in ipairs(list) do
  if q.parts[i] then send(table.concat({'BUILDPART','1',s.client.lease,q.request,i,q.parts[i]},'|')) end
 end
end
local function guestReceive(f,now)
 local q=g.active
 if #f~=7 or not q or f[4]~=q.request or not B.status[f[5]] or not B.reason(f[6]) then return end
 q.replied=now
 if f[5]=='RECEIVING' then
  local missing={}
  if f[7]~='-' then for _,v in ipairs(B.split(f[7],',')) do local i=B.integer(v,B.maxParts);if i and i>=1 then missing[#missing+1]=i end end end
  q.missing=#missing>0 and missing or nil
 elseif f[5]=='PENDING' then
  if q.state~='PENDING' then q.state='PENDING';log('pending request='..q.request) end
 elseif B.final[f[5]] then
  finish(f[5]=='STALE' and 'REFUSED' or f[5]=='BUSY' and 'REFUSED' or f[5],f[5]=='STALE' and 'stale:'..f[6] or f[5]=='BUSY' and 'busy' or f[6])
 end
end
-- Planner hooks. Only the guest is redirected; the host keeps vanilla behaviour.
function g.confirm(menu)
 local plan,reason=g.capturePlan(menu)
 if not plan then log('refused request=local kind=EXPAND reason='..reason);notice('Station build refused: '..explain(reason)..'.');return false,reason end
 return g.submit('EXPAND',plan.station,plan,plan.budget and string.format('%.0f',plan.budget) or '-')
end
function g.install()
 if g.installed then return true end
 local menu=Helper and Helper.getMenu and Helper.getMenu('StationConfigurationMenu')
 if not menu or type(menu.buttonConfirm)~='function' then return false end
 local original=menu.buttonConfirm
 menu.buttonConfirm=function(...)
  if cfg.slot~='p2' then return original(...) end
  -- Never fall through to SetConstructionSequenceFromConstructionMap on a guest.
  local ok,err=pcall(g.confirm,menu)
  if not ok then log('refused request=local kind=EXPAND reason=capture_error '..tostring(err));notice('Station build was not sent (internal error).') end
 end
 local map=Helper.getMenu('MapMenu')
 if map and type(map.buttonRemovePlot)=='function' then
  local removePlot=map.buttonRemovePlot
  map.buttonRemovePlot=function(station,confirmed,removestorage,...)
   if cfg.slot~='p2' or not confirmed then return removePlot(station,confirmed,removestorage,...) end
   local ok,err=pcall(g.submit,'REMOVEPLOT',station,nil,removestorage and '1' or '0')
   if not ok then log('refused request=local kind=REMOVEPLOT reason=capture_error '..tostring(err)) end
   if map.closeContextMenu then pcall(map.closeContextMenu) end
  end
 else log('map plot removal hook unavailable') end
 g.installed=true;log('planner hook installed slot='..tostring(cfg.slot))
 return true
end
-- ---------------------------------------------------------------- authority side
local function done(request,status,reason)
 send(table.concat({'BUILDDONE','1',s.client.lease,request,status,reason},'|'))
end
local function finalize(job,status,reason)
 g.jobs[job.request]=nil
 g.executed[job.request]={status=status,reason=reason};g.executedCount=g.executedCount+1
 log((status=='ACCEPTED' and 'accepted' or status=='UNCERTAIN' and 'uncertain' or 'refused')..' request='..job.request..' slot='..job.slot..' kind='..job.h.kind..' station='..job.h.code..' reason='..reason)
 done(job.request,status,reason)
end
g.finalize=finalize
function g.findStation(h,slot)
 local faction=B.factionFor(slot);if not faction then return nil,'not_owner' end
 local n=tonumber(C.GetNumAllFactionStations(faction))
 local buf=ffi.new('UniverseID[?]',math.max(1,n));n=tonumber(C.GetAllFactionStations(buf,n,faction))
 local found
 for i=0,n-1 do
  local id=stationIdentity(buf[i])
  if id and id.code==h.code and id.macro==h.macro and id.sector==h.sector then
   if found then return nil,'station_ambiguous' end
   found=buf[i]
  end
 end
 if not found then return nil,'station_not_found_for_owner' end
 if not B.ownerAllowed(slot,data(found,'owner')) then return nil,'not_owner' end
 return found
end
local function libraryHas(id)
 local n=tonumber(C.GetNumConstructionPlans());local buf=ffi.new('UIConstructionPlan[?]',math.max(1,n))
 n=tonumber(C.GetConstructionPlans(buf,n))
 for i=0,n-1 do if buf[i].id~=nil and ffi.string(buf[i].id)==id then return true,buf[i].source~=nil and ffi.string(buf[i].source) or '' end end
 return false
end
local function importable(id)
 local n=tonumber(C.GetNumImportableConstructionPlans());local buf=ffi.new('UIConstructionPlanInfo[?]',math.max(1,n))
 n=tonumber(C.GetImportableConstructionPlans(buf,n))
 for i=0,n-1 do
  local file=buf[i].filename~=nil and ffi.string(buf[i].filename) or ''
  if file==id or file==id..'.xml' then return id end
 end
end
local function libraryPlan(id)
 local n=tonumber(C.GetNumConstructionPlanInfo(id));local buf=ffi.new('UIConstructionPlanEntry[?]',math.max(1,n))
 n=tonumber(C.GetConstructionPlanInfo(buf,n,id))
 local out,number={},{}
 for i=0,n-1 do number[tonumber(buf[i].idx)]=i+1 end
 for i=0,n-1 do
  local e=buf[i];local o=e.offset
  local predconnection=e.predecessorconnectionid~=nil and ffi.string(e.predecessorconnectionid) or ''
  local pred=predconnection~='' and number[tonumber(e.predecessoridx)] or 0
  out[#out+1]={macro=ffi.string(e.macroid),connection=e.connectionid~=nil and ffi.string(e.connectionid) or '',pred=pred,
   predconnection=pred~=0 and predconnection or '',pos={o.x,o.y,o.z},rot={o.yaw,o.pitch,o.roll}}
 end
 return out
end
-- Plot change and its price. Reverts the extension when the player cannot pay.
local function plotStep(job,station)
 if not job.h.plot then return {price=0} end
 local size,center=C.GetBuildPlotSize(station),C.GetBuildPlotCenterOffset(station)
 local delta=B.plotDelta(job.h.plot,{size.x,size.y,size.z,center.x,center.y,center.z})
 if delta then
  local pos=ffi.new('Coord3D',delta.pos[1],delta.pos[2],delta.pos[3]);local neg=ffi.new('Coord3D',delta.neg[1],delta.neg[2],delta.neg[3])
  if not C.ExtendBuildPlot(station,pos,neg,true) then return nil,'plot_extend_failed' end
  job.plotUndo={pos=ffi.new('Coord3D',-delta.pos[1],-delta.pos[2],-delta.pos[3]),neg=ffi.new('Coord3D',-delta.neg[1],-delta.neg[2],-delta.neg[3])}
 end
 size,center=C.GetBuildPlotSize(station),C.GetBuildPlotCenterOffset(station)
 local sector=data(station,'sectorid');local sector64=ConvertStringTo64Bit(key(sector))
 local sectorOwner,sectorPlayer=GetComponentData(sector64,'owner'),GetComponentData(sector64,'isplayerowned')
 local owned=sectorOwner~='ownerless' and sectorOwner~='xenon'
 local paid=C.GetPaidBuildPlotSize(station)
 local price=0
 if owned and not sectorPlayer then
  local full=tonumber(C.GetBuildPlotPrice(sector64,C.GetObjectPositionInSector(station),size.x,size.y,size.z,data(station,'owner')))
  local changed=ffi.new('bool[1]',false)
  price=math.max(0,full-tonumber(C.GetBuildPlotPayment(station,changed)))
  if paid.x>=size.x and paid.y>=size.y and paid.z>=size.z and not changed[0] then price=0 end
 end
 return {price=price,size=size,center=center,sector=sector64,free=(not owned) or sectorPlayer}
end
local function undoPlot(job,station)
 if job.plotUndo then C.ExtendBuildPlot(station,job.plotUndo.pos,job.plotUndo.neg,true);job.plotUndo=nil end
end
-- One validation/execution step per frame; returns nothing while waiting.
function g.step(job,now)
 local h=job.h
 local station,reason=g.findStation(h,job.slot)
 if not station then return finalize(job,'REFUSED',reason) end
 if h.kind=='REMOVEPLOT' then
  if tonumber(C.GetNumStationModules(station,true,true))>0 then return finalize(job,'REFUSED','has_modules') end
  if C.RemoveBuildPlot2(station,h.removeStorage) then return finalize(job,'ACCEPTED','plot_removed') end
  return finalize(job,'REFUSED','native_refused')
 end
 if job.stage=='native' then
  if now>job.deadline then return finalize(job,'UNCERTAIN','native_timeout') end
  return
 end
 local storage=data(station,'buildstorage')
 if not storage or storage==0 then return finalize(job,'REFUSED','no_build_storage') end
 if h.kind=='RECYCLE' then
  job.stage='native';job.deadline=now+30
  AddUITriggeredEvent('X4LAN_Build','Recycle',{request=job.request,station=A.id(station),owner=B.factionFor(job.slot)})
  return
 end
 -- EXPAND
 if job.inbox~='1' then return finalize(job,'REFUSED','no_plan_inbox') end
 local id=job.plan.id
 if not job.imported then
  local file=importable(id)
  if not file then
   job.waitFile=job.waitFile or now+5
   if now>job.waitFile then return finalize(job,'REFUSED','plan_file_missing') end
   return
  end
  if libraryHas(id) then C.RemoveConstructionPlan('local',id) end
  C.ImportMapConstructionPlan(file,id)
  job.imported=true
  if not libraryHas(id) then return finalize(job,'REFUSED','plan_import_failed') end
 end
 local invalid=ffi.new('uint32_t[1]',0)
 if not C.IsConstructionPlanValid(id,invalid) then return finalize(job,'REFUSED','plan_invalid_patches:'..tonumber(invalid[0])) end
 local same,why=B.samePlan(job.plan.entries,libraryPlan(id))
 if not same then return finalize(job,'REFUSED',why) end
 local missing=C.GetMissingConstructionPlanBlueprints3(station,0,id,false)
 if missing~=nil and ffi.string(missing)~='' then return finalize(job,'REFUSED','missing_blueprints') end
 local plot,err=plotStep(job,station);if not plot then return finalize(job,'REFUSED',err) end
 local money=B.funds(GetPlayerMoney(),tonumber(GetComponentData(ConvertStringTo64Bit(key(storage)),'money')) or 0,h.budget,plot.price)
 if not money then undoPlot(job,station);return finalize(job,'REFUSED','funds') end
 if plot.size then
  if plot.price>0 then TransferPlayerMoneyTo(plot.price,ConvertStringTo64Bit(key(C.GetSectorControlStation(plot.sector)))) end
  if plot.price>0 or plot.free then C.PayBuildPlotSize(station,plot.size,plot.center) end
 end
 job.money=money;job.storage=storage;job.stage='native';job.deadline=now+30
 AddUITriggeredEvent('X4LAN_Build','Expand',{request=job.request,station=A.id(station),plan=id,owner=B.factionFor(job.slot)})
end
local function hostReceive(f,now)
 if f[3]~=s.client.lease then return end
 local request=f[4];if not B.hex(request) then return end
 local old=g.executed[request]
 if f[1]=='BUILDEXEC' then
  if #f~=16 or (f[5]~='p1' and f[5]~='p2') or (f[16]~='0' and f[16]~='1') then return end
  if old then return done(request,old.status,old.reason) end
  local job=g.jobs[request]
  if job then if job.plan or job.h.kind~='EXPAND' then done(request,'RECEIVED','ok') end;return end
  local h=B.header(f,6)
  if not h then return done(request,'REFUSED','header') end
  local active=0;for _ in pairs(g.jobs) do active=active+1 end
  if active>=16 or g.executedCount>=4096 then return done(request,'REFUSED','authority_busy') end
  g.jobs[request]={request=request,slot=f[5],h=h,inbox=f[16],parts={},got=0}
  log('exec request='..request..' slot='..f[5]..' kind='..h.kind..' station='..h.code..' entries='..h.entries..' bytes='..h.bytes)
  if h.kind~='EXPAND' then done(request,'RECEIVED','ok') end
 elseif f[1]=='BUILDXPART' then
  local job=g.jobs[request];if #f~=6 or not job or job.plan then return end
  local i=B.integer(f[5],job.h.parts);if not i or i<1 or #f[6]>B.partSize or #f[6]==0 then return end
  if job.parts[i] and job.parts[i]~=f[6] then return finalize(job,'REFUSED','part_conflict') end
  if not job.parts[i] then job.parts[i]=f[6];job.got=job.got+1 end
  if job.got<job.h.parts then return end
  local xml=table.concat(job.parts)
  if #xml~=job.h.bytes or B.digest(xml)~=job.h.digest then return finalize(job,'REFUSED','digest') end
  local plan,reason=B.parse(xml)
  if not plan then return finalize(job,'REFUSED',reason) end
  if plan.id~='x4lan_'..request or #plan.entries~=job.h.entries then return finalize(job,'REFUSED','plan_identity') end
  job.plan=plan;job.parts=nil;done(request,'RECEIVED','ok')
 end
end
RegisterEvent('X4LAN_Build.Result',function(_,payload)
 if cfg.slot~='p1' or type(payload)~='string' then return end
 local f=B.split(payload,'|');local job=g.jobs[f[1]]
 if #f<2 or not job or job.stage~='native' then return end
 local built=f[2]=='1'
 if not built then return finalize(job,'REFUSED','native_build_failed') end
 if job.h.kind=='EXPAND' and job.money and job.money.transfer~=0 then
  local storage=ConvertStringTo64Bit(key(job.storage))
  if job.money.transfer>0 then TransferPlayerMoneyTo(job.money.transfer,storage) else TransferMoneyToPlayer(-job.money.transfer,storage) end
 end
 finalize(job,'ACCEPTED',job.h.kind=='EXPAND' and 'build_created' or 'recycle_created')
end)
-- ---------------------------------------------------------------- chaining
local receive=U.receive
function U.receive(f,now)
 if f[1]=='BUILDREPLY' then if cfg.slot=='p2' then guestReceive(f,now) end;return true end
 if f[1]=='BUILDEXEC' or f[1]=='BUILDXPART' then if cfg.slot=='p1' and s.client and s.client.lease then hostReceive(f,now) end;return true end
 return receive(f,now)
end
local function frame(now)
 if cfg.slot=='p2' then
  if not g.installed then
   if now>=(g.nextInstall or 0) then
    g.nextInstall=now+2;g.installTries=(g.installTries or 0)+1
    if not g.install() and g.installTries==30 then feature('planner_menu_missing') end
   end
  end
  if s.client and s.client.lease then guestFrame(now) end
 elseif s.client and s.client.lease and not A.paused() then
  for _,job in pairs(g.jobs) do
   if job.plan or job.h.kind~='EXPAND' then g.step(job,now);break end
  end
 end
end
local originalFrame=U.frame
function U.frame(now)
 originalFrame(now)
 if g.failed then return end
 local ok,err=pcall(frame,now)
 if not ok then g.failed=true;log('STOPPED '..tostring(err));feature('runtime_error') end
end
if cfg.slot=='p2' then pcall(g.install) end
