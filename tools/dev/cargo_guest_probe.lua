-- Opt-in native guest probe. It exercises the same DropCargo and magnet APIs as the UI.
local A=X4LAN_BUBBLE_API
if not A or not A.cargo or X4LAN_CONFIG.test_profile~='45841' or X4LAN_CONFIG.slot~='p2' then return end
local ffi=require('ffi');local C=ffi.C
ffi.cdef[[bool DropCargo(UniverseID,const char*,uint32_t);void SetLootMagnetActive(bool);]]
local frame=A.universe.frame;local ready,dropped,magnet,deadline
function A.universe.frame(now)
 frame(now)
 if not A.cargo.ready or not A.universe.latest or not A.cargo.inventory.energycells or A.paused() then return end
 ready=ready or now
 if not dropped and now-ready>2 then
  dropped=now
  if not X4LAN_CONFIG.cargo_probe_capacity then
   local ok=C.DropCargo(C.GetPlayerControlledShipID(),'energycells',20)
   DebugError('[X4LAN] CARGO_GUEST_PROBE DropCargo='..tostring(ok))
  end
 end
 if dropped and not magnet and now-dropped>4 then
  C.SetLootMagnetActive(true);magnet=true;deadline=now+18
  DebugError('[X4LAN] CARGO_GUEST_PROBE magnet=true')
 end
 if deadline and now>deadline then C.SetLootMagnetActive(false);deadline=nil end
end
