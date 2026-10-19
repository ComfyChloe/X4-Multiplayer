-- Opt-in fixture seeding, never shipped.
local A=X4LAN_BUBBLE_API
if not A or not A.cargo or X4LAN_CONFIG.slot~='p1' or X4LAN_CONFIG.test_profile~='45841' then return end
local frame=A.universe.frame;local seeded=false
function A.universe.frame(now)
 frame(now)
 if A.cargo.ready and not seeded then seeded=true;AddUITriggeredEvent('X4LAN_CargoSeed','Seed') end
end
