-- Read-only equipment verification in the isolated native test. Never shipped.
local A=X4LAN_BUBBLE_API
if not A or not A.coop or X4LAN_CONFIG.test_profile~='45841' then return end
local ffi=require('ffi');local C=ffi.C
if not pcall(ffi.typeof,'SoftwareSlot') then
 ffi.cdef[[typedef struct { const char* max; const char* current; } SoftwareSlot;]]
end
ffi.cdef[[
uint32_t GetNumSoftwareSlots(UniverseID,const char*);
uint32_t GetSoftwareSlots(SoftwareSlot*,uint32_t,UniverseID,const char*);
size_t GetNumVirtualUpgradeSlots(UniverseID,const char*,const char*);
const char* GetVirtualUpgradeSlotCurrentMacro(UniverseID,const char*,size_t);
]]
local frame=A.universe.frame;local done=false
function A.universe.frame(now)
 frame(now)
 if done or not A.coop.ready then return end
 done=true
 local ok,err=pcall(function()
  for i=1,2 do
   local ship=A.coop.ships[i];local n=C.GetNumSoftwareSlots(ship,'')
   assert(n<=64,'unexpected software slot count')
   local slots=ffi.new('SoftwareSlot[?]',math.max(1,n))
   n=C.GetSoftwareSlots(slots,n,ship,'')
   local installed,wares={},{}
   for j=0,n-1 do
    local ware=ffi.string(slots[j].current);installed[ware]=true;wares[#wares+1]=ware
   end
   table.sort(wares)
   local passed=true
   for _,ware in ipairs({'software_dockmk2','software_flightassistmk1','software_scannerlongrangemk2',
                         'software_scannerobjectmk2','software_targetmk1','software_trademk1'}) do
    passed=passed and installed[ware]==true
   end
   local count=tonumber(C.GetNumVirtualUpgradeSlots(ship,'','thruster'));local thrusters={}
   assert(count<=16,'unexpected thruster slot count')
   for j=1,count do
    thrusters[j]=ffi.string(C.GetVirtualUpgradeSlotCurrentMacro(ship,'thruster',j))
    passed=passed and thrusters[j]=='thruster_gen_s_combat_01_mk3_macro'
   end
   DebugError('[X4LAN] EQUIPMENT_PROBE ship='..i..' software='..table.concat(wares,',')..
    ' thrusters='..table.concat(thrusters,',')..' success='..tostring(passed and count>0))
  end
 end)
 if not ok then DebugError('[X4LAN] EQUIPMENT_PROBE failed='..tostring(err)) end
end
