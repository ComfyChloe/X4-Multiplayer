-- Native calls confined to the opt-in dev harness and disposable profile.
local A=X4LAN_BUBBLE_API
if not A or not A.universe or X4LAN_CONFIG.test_profile~='45841' then return end
local ffi=require('ffi');local C=ffi.C
ffi.cdef[[bool DropCargo(UniverseID,const char*,uint32_t);void SetLootMagnetActive(bool);]]
local original=A.universe.frame
local started,ready,dropped,magnet,deadline
function A.universe.frame(now)
 original(now)
 if not A.coop.ready or A.paused() then return end
 if not started then started=now;AddUITriggeredEvent('X4LAN_CargoProbe','Start') end
 if ready and not dropped and now-ready>2 then
  dropped=now;local ok=C.DropCargo(C.GetPlayerControlledShipID(),'energycells',20)
  DebugError('[X4LAN] CARGO_PROBE native_DropCargo='..tostring(ok))
 end
 if dropped and not magnet and now-dropped>2 then
  C.SetLootMagnetActive(true);magnet=true;deadline=now+12
  DebugError('[X4LAN] CARGO_PROBE native_magnet=true')
 end
 if deadline and now>deadline then C.SetLootMagnetActive(false);deadline=nil end
end
RegisterEvent('X4LAN_CargoProbe.Ready',function() ready=GetCurRealTime() end)
RegisterEvent('X4LAN_CargoProbe.Table',function(_,value)
 local result=type(value)
 if type(value)=='table' then for k,v in pairs(value) do result=result..' '..tostring(k)..'='..tostring(v) end end
 DebugError('[X4LAN] CARGO_PROBE table_payload='..result)
end)
RegisterEvent('X4LAN_CargoProbe.Drop',function(_,object)
 local id=ConvertIDTo64Bit(object)
 DebugError('[X4LAN] CARGO_PROBE lua_drop_key='..tostring(id)..' operational='..tostring(C.IsComponentOperational(id)))
end)
RegisterEvent('X4LAN_CargoProbe.Collected',function(_,object)
 local id=ConvertIDTo64Bit(object)
 DebugError('[X4LAN] CARGO_PROBE lua_collected_key='..tostring(id)..' operational='..tostring(C.IsComponentOperational(id)))
end)
