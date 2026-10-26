-- Opt-in fixture seeding, never shipped.
local A=X4LAN_BUBBLE_API
if not A or not A.cargo or X4LAN_CONFIG.slot~='p1' or X4LAN_CONFIG.test_profile~='45841' then return end
local frame=A.universe.frame;local seeded=false
function A.universe.frame(now)
 frame(now)
 if A.cargo.ready and not seeded then seeded=true;AddUITriggeredEvent('X4LAN_CargoSeed','Seed') end
end

-- Exercise MD deduplication separately from the Lua and relay replay guards.
local trigger=AddUITriggeredEvent
AddUITriggeredEvent=function(screen,control,args)
 trigger(screen,control,args)
 if screen=='X4LAN_Cargo' and control=='Order' then
  trigger(screen,control,args)
  DebugError('[X4LAN] CARGO_SEED duplicate_MD_order='..args.request)
 end
end
