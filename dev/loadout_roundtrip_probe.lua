-- Unshipped native feasibility check for reconstructing host-created ships.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.universe or cfg.test_profile~='45841' or not cfg.loadout_roundtrip then return end
local ffi=require('ffi');local C=ffi.C;local U=A.universe
if not pcall(ffi.typeof,'SoftwareSlot') then ffi.cdef('typedef struct {const char* max;const char* current;} SoftwareSlot;') end
ffi.cdef[[
size_t GetNumUpgradeSlots(UniverseID,const char*,const char*);
UniverseID GetUpgradeSlotCurrentComponent(UniverseID,const char*,size_t);
size_t GetNumVirtualUpgradeSlots(UniverseID,const char*,const char*);
const char* GetVirtualUpgradeSlotCurrentMacro(UniverseID,const char*,size_t);
uint32_t GetNumSoftwareSlots(UniverseID,const char*);
uint32_t GetSoftwareSlots(SoftwareSlot*,uint32_t,UniverseID,const char*);
UpgradeGroup GetUpgradeSlotGroup(UniverseID,const char*,const char*,size_t);
uint32_t GetNumUpgradeGroups(UniverseID,const char*);
uint32_t GetUpgradeGroups(UpgradeGroup*,uint32_t,UniverseID,const char*);
UpgradeGroupInfo GetUpgradeGroupInfo(UniverseID,const char*,const char*,const char*,const char*);
]]
local function log(v) DebugError('[X4LAN] LOADOUT_ROUNDTRIP '..v) end
local function read(object)
 local rows,macros,software,slots,groups,allslots={},{},{},{},{},{}
 for _,kind in ipairs({'engine','shield','weapon','turret'}) do
  local count=tonumber(C.GetNumUpgradeSlots(object,'',kind));assert(count<=512,'upgrade slot limit')
  for index=1,count do
   local child=C.GetUpgradeSlotCurrentComponent(object,kind,index)
   local macro=child~=0 and GetComponentData(A.id(child),'macro') or 'none'
   rows[#rows+1]=kind..','..index..','..macro
   local group=C.GetUpgradeSlotGroup(object,'',kind,index)
   local path,name=ffi.string(group.path),ffi.string(group.group)
   local grouped=(kind=='shield' or kind=='turret') and (path~='..' or name~='')
   allslots[#allslots+1]={kind=kind,index=index,macro=macro,grouped=grouped,path=path,group=name}
   if macro~='none' then
    macros[#macros+1]=macro;slots[#slots+1]={kind=kind,index=index,macro=macro,grouped=grouped}
   end
  end
 end
 local count=tonumber(C.GetNumUpgradeGroups(object,''));assert(count<=512,'equipment group limit')
 local nativeGroups=ffi.new('UpgradeGroup[?]',math.max(1,count));count=tonumber(C.GetUpgradeGroups(nativeGroups,count,object,''))
 for index=0,count-1 do
  local path,group=ffi.string(nativeGroups[index].path),ffi.string(nativeGroups[index].group)
  if path~='..' or group~='' then
   for _,kind in ipairs({'turret','shield'}) do
    local info=C.GetUpgradeGroupInfo(object,'',path,group,kind);local macro=ffi.string(info.currentmacro)
    if macro~='' and info.count>0 then
     local capacity=0
     for _,slot in ipairs(allslots) do if slot.kind==kind and slot.path==path and slot.group==group then capacity=capacity+1 end end
     assert(capacity>0 and capacity<=512,'group slot membership unavailable')
     groups[#groups+1]={kind=kind,path=path,group=group,macro=macro,count=tonumber(info.count),capacity=capacity}
    end
   end
  end
 end
 local count=tonumber(C.GetNumVirtualUpgradeSlots(object,'','thruster'));assert(count<=16,'thruster slot limit')
 for index=1,count do
  local macro=ffi.string(C.GetVirtualUpgradeSlotCurrentMacro(object,'thruster',index))
  rows[#rows+1]='thruster,'..index..','..macro
  if macro~='' then macros[#macros+1]=macro;slots[#slots+1]={kind='thruster',index=index,macro=macro} end
 end
 local count=tonumber(C.GetNumSoftwareSlots(object,''));assert(count<=64,'software limit')
 local softwareSlots=ffi.new('SoftwareSlot[?]',math.max(1,count));count=tonumber(C.GetSoftwareSlots(softwareSlots,count,object,''))
 for index=0,count-1 do
  local ware=ffi.string(softwareSlots[index].current)
  if ware~='' then software[#software+1]=ware;rows[#rows+1]='software,'..ware end
 end
 table.sort(rows);table.sort(macros);table.sort(software)
 return {signature=table.concat(rows,';'),macros=macros,software=software,slots=slots,groups=groups,allslots=allslots}
end
local function savedLoadout(data,index)
 -- Only an opt-in empty disposable profile may exercise persistent presets.
 assert(cfg.loadout_saved_exact and cfg.loadout_probe_id,'saved-loadout mode not authorized')
 assert(pcall(ffi.typeof,'UILoadout2') and pcall(ffi.typeof,'UILoadoutInfo'),'engine loadout ABI unavailable')
 ffi.cdef[[void SaveLoadout2(const char*,UILoadout2,const char*,const char*,bool,const char*,const char*);
 bool RemoveLoadout(const char*,const char*,const char*);
 uint32_t GetNumLoadoutsInfo(UniverseID,const char*);
 uint32_t GetLoadoutsInfo(UILoadoutInfo*,uint32_t,UniverseID,const char*);]]
 local name=cfg.loadout_probe_id..'_'..index
 local function find()
  local n=tonumber(C.GetNumLoadoutsInfo(0,data.macro));assert(n<10000,'preset list limit')
  local info=ffi.new('UILoadoutInfo[?]',math.max(1,n));n=tonumber(C.GetLoadoutsInfo(info,n,0,data.macro))
  for i=0,n-1 do if ffi.string(info[i].name)==name then return ffi.string(info[i].id) end end
 end
 assert(not find(),'diagnostic preset already exists')
 local loadout=ffi.new('UILoadout2');local keep={}
 for kind,category in pairs({engine='engines',shield='shields',weapon='weapons',turret='turrets'}) do
  local values={};for _,slot in ipairs(data.slots) do if slot.kind==kind and not slot.grouped then values[#values+1]=slot end end
  local array=ffi.new('UILoadoutMacroData2[?]',math.max(1,#values));keep[#keep+1]=array
  for i,slot in ipairs(values) do
   array[i-1].macro=slot.macro;array[i-1].upgradetypename=kind;array[i-1].slot=slot.index-1
   array[i-1].weaponsetting.ammomacroname='';array[i-1].weaponsetting.weaponmode=''
  end
  loadout[category]=array;loadout['num'..category]=#values
 end
 for _,kind in ipairs({'turret','shield'}) do
  local values={};for _,group in ipairs(data.groups) do if group.kind==kind then values[#values+1]=group end end
  local array=ffi.new('UILoadoutGroupData2[?]',math.max(1,#values));keep[#keep+1]=array
  for i,group in ipairs(values) do
   array[i-1].macro=group.macro;array[i-1].path=group.path;array[i-1].group=group.group
   array[i-1].count=cfg.loadout_fill_groups and group.capacity or group.count
   array[i-1].weaponsetting.ammomacroname='';array[i-1].weaponsetting.weaponmode=''
  end
  loadout[kind..'groups']=array;loadout['num'..kind..'groups']=#values
 end
 local software=ffi.new('UILoadoutSoftwareData[?]',math.max(1,#data.software));keep[#keep+1]=software
 for i,ware in ipairs(data.software) do software[i-1].ware=ware end
 loadout.software=software;loadout.numsoftware=#data.software;loadout.thruster.macro=''
 for _,slot in ipairs(data.slots) do if slot.kind=='thruster' then loadout.thruster.macro=slot.macro end end
 C.SaveLoadout2(data.macro,loadout,'local',name,false,name,'Disposable X4LAN exact-slot experiment')
 assert(#keep==7,'native loadout arrays must remain owned through save')
 local id=assert(find(),'saved diagnostic loadout not exposed by engine')
 log('saved id='..index..' preset='..id);return id
end
local pending,selected={},{};local requested,completed=0,0
local function complete(index,clone)
 local test=assert(pending[index],'unowned reconstruction callback')
 local ok,result=pcall(read,clone)
 if not ok then log('failed id='..index..' reason='..tostring(result))
 else
  if cfg.loadout_fill_groups then
   -- Read actual remaining equipment/capacity after pruning. Count source
   -- macros absent from the clone, rather than fabricating a zero-unused row.
   local available,unused={},0
   for _,macro in ipairs(result.macros) do available[macro]=(available[macro] or 0)+1 end
   for _,macro in ipairs(test.macros) do
    if (available[macro] or 0)>0 then available[macro]=available[macro]-1 else unused=unused+1 end
   end
   local hull,shield=GetComponentData(A.id(clone),'hullmax','shieldmax')
   DebugError(string.format('[X4LAN] LOADOUT_ROUNDTRIP_NATIVE id=%d source=%s clone=%s input=%d unused=%d sourceHull=%s cloneHull=%s sourceShield=%s cloneShield=%s',
    index,test.code,GetComponentData(A.id(clone),'idcode'),#test.macros,unused,tostring(test.hull),tostring(hull),tostring(test.shield),tostring(shield)))
  end
  log('expected id='..index..' macro='..test.macro..' rows='..test.signature)
  log('observed id='..index..' macro='..test.macro..' rows='..result.signature)
  log('result id='..index..' exactSlots='..tostring(result.signature==test.signature))
 end
 if test.preset then assert(C.RemoveLoadout('local',test.macro,test.preset),'diagnostic preset removal failed');log('preset-removed id='..index) end
 assert(clone~=C.GetPlayerContainerID() and A.coop.entity(clone)==0,'player in diagnostic clone')
 C.RemoveComponent(clone);pending[index]=nil;completed=completed+1
 if completed==8 then log('complete count=8') end
end
RegisterEvent('X4LAN_LoadoutRoundtrip.Created',function(_,payload)
 local index,handle=tostring(payload):match('^(%d+)|(0x[%da-fA-F]+)$');index=tonumber(index)
 local test=index and pending[index];if not test then return end
 local clone=A.cargo.object(handle);assert(clone and C.IsComponentOperational(clone),'created clone unavailable')
 assert(clone~=test.source and clone~=C.GetPlayerContainerID() and A.coop.entity(clone)==0,'unsafe diagnostic clone')
 C.ActivateObject(clone,false)
 if not cfg.loadout_fill_groups then complete(index,clone);return end
 -- A full group gives deterministic occupied positions. Preflight EVERY wanted
 -- slot before removing only surplus hardware from this newly created clone.
 local remove,seen={},{}
 for _,slot in ipairs(test.allslots) do
  local child=C.GetUpgradeSlotCurrentComponent(clone,slot.kind,slot.index)
  local macro=child~=0 and GetComponentData(A.id(child),'macro') or 'none'
  if slot.macro~='none' then assert(macro==slot.macro,'full group still differs at required physical slot')
  elseif child~=0 then
   assert(slot.grouped,'unexpected extra ungrouped clone equipment')
   assert(C.GetContextByClass(child,'container',false)==clone,'surplus component not owned by clone')
   assert(not seen[tostring(child)],'duplicate surplus component');seen[tostring(child)]=true
   remove[#remove+1]={object=child,kind=slot.kind,index=slot.index}
  end
 end
 for _,entry in ipairs(remove) do
  C.RemoveComponent(entry.object);log('pruned id='..index..' kind='..entry.kind..' slot='..entry.index)
 end
 test.clone=clone
end)
local frame=U.frame
function U.frame(now)
 frame(now)
 for index,test in pairs(pending) do
  if test.clone then
   test.readAfter=test.readAfter or now+1
   if now>=test.readAfter then complete(index,test.clone) end
  end
 end
 if not U.ready or A.paused() or requested>=8 then return end
 for _,entry in ipairs(U.publish) do
  if C.IsComponentOperational(entry.object) then
   local macro=GetComponentData(A.id(entry.object),'macro')
   if not selected[macro] then
    selected[macro]=true;requested=requested+1
    local data=read(entry.object);data.macro=macro;data.source=entry.object;pending[requested]=data
    if cfg.loadout_fill_groups then
     data.code,data.hull,data.shield=GetComponentData(A.id(entry.object),'idcode','hullmax','shieldmax')
    end
    if cfg.loadout_saved_exact then data.preset=savedLoadout(data,requested) end
    log('requested id='..requested..' macro='..macro)
    AddUITriggeredEvent('X4LAN_LoadoutRoundtrip','Create',{index=requested,source=A.id(entry.object),macros=data.macros,software=data.software,preset=data.preset or '',fillgroups=cfg.loadout_fill_groups==true})
    break -- Bound native reconstruction work to one request per frame.
   end
  end
 end
end
