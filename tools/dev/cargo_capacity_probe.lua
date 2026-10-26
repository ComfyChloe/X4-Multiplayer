-- Native host capacity fixture; synthetic peer submits real cargo orders.
local A=X4LAN_BUBBLE_API
if not A or not A.cargo or X4LAN_CONFIG.slot~='p1' or X4LAN_CONFIG.test_profile~='45841' then return end
local frame=A.universe.frame;local seeded,freed=false,false
function A.universe.frame(now)
 frame(now)
 if A.cargo.ready and not seeded then seeded=true;AddUITriggeredEvent('X4LAN_CargoCapacity','Seed') end
 local count=0
 for _,r in pairs(A.cargo.executed) do if r.fence then count=count+1 end end
 if count==3 and not freed then freed=true;AddUITriggeredEvent('X4LAN_CargoCapacity','Free') end
end
