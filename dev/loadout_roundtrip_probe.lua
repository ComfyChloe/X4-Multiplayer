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
]]
local function log(v) DebugError('[X4LAN] LOADOUT_ROUNDTRIP '..v) end
local function read(object)
 local rows,macros,software={},{},{}
 for _,kind in ipairs({'engine','shield','weapon','turret'}) do
  local count=tonumber(C.GetNumUpgradeSlots(object,'',kind));assert(count<=512,'upgrade slot limit')
  for index=1,count do
   local child=C.GetUpgradeSlotCurrentComponent(object,kind,index)
   local macro=child~=0 and GetComponentData(A.id(child),'macro') or 'none'
   rows[#rows+1]=kind..','..index..','..macro
   if macro~='none' then macros[#macros+1]=macro end
  end
 end
 local count=tonumber(C.GetNumVirtualUpgradeSlots(object,'','thruster'));assert(count<=16,'thruster slot limit')
 for index=1,count do
  local macro=ffi.string(C.GetVirtualUpgradeSlotCurrentMacro(object,'thruster',index))
  rows[#rows+1]='thruster,'..index..','..macro
  if macro~='' then macros[#macros+1]=macro end
 end
 local count=tonumber(C.GetNumSoftwareSlots(object,''));assert(count<=64,'software limit')
 local slots=ffi.new('SoftwareSlot[?]',math.max(1,count));count=tonumber(C.GetSoftwareSlots(slots,count,object,''))
 for index=0,count-1 do
  local ware=ffi.string(slots[index].current)
  if ware~='' then software[#software+1]=ware;rows[#rows+1]='software,'..ware end
 end
 table.sort(rows);table.sort(macros);table.sort(software)
 return {signature=table.concat(rows,';'),macros=macros,software=software}
end
local pending,selected={},{};local requested,completed=0,0
RegisterEvent('X4LAN_LoadoutRoundtrip.Created',function(_,payload)
 local index,handle=tostring(payload):match('^(%d+)|(0x[%da-fA-F]+)$');index=tonumber(index)
 local test=index and pending[index];if not test then return end
 local clone=A.cargo.object(handle);assert(clone and C.IsComponentOperational(clone),'created clone unavailable')
 C.ActivateObject(clone,false)
 local ok,result=pcall(read,clone)
 if not ok then log('failed id='..index..' reason='..tostring(result))
 else
  log('expected id='..index..' macro='..test.macro..' rows='..test.signature)
  log('observed id='..index..' macro='..test.macro..' rows='..result.signature)
  log('result id='..index..' exactSlots='..tostring(result.signature==test.signature))
 end
 assert(clone~=C.GetPlayerContainerID() and A.coop.entity(clone)==0,'player in diagnostic clone')
 C.RemoveComponent(clone);pending[index]=nil;completed=completed+1
 if completed==8 then log('complete count=8') end
end)
local frame=U.frame
function U.frame(now)
 frame(now)
 if not U.ready or A.paused() or requested>=8 then return end
 for _,entry in ipairs(U.publish) do
  if C.IsComponentOperational(entry.object) then
   local macro=GetComponentData(A.id(entry.object),'macro')
   if not selected[macro] then
    selected[macro]=true;requested=requested+1
    local data=read(entry.object);data.macro=macro;pending[requested]=data
    log('requested id='..requested..' macro='..macro)
    AddUITriggeredEvent('X4LAN_LoadoutRoundtrip','Create',{index=requested,source=A.id(entry.object),macros=data.macros,software=data.software})
    break -- Bound native reconstruction work to one request per frame.
   end
  end
 end
end
