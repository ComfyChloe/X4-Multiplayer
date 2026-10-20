-- Opt-in native role readback after loading a pristine shared seed.
local A=X4LAN_BUBBLE_API;local G=X4LAN_SEED_GATE;local cfg=X4LAN_CONFIG
if not A or not G or cfg.test_profile~='45841' then return end
local C=require('ffi').C
local frame=A.universe.frame;local observed=false
function A.universe.frame(now)
 frame(now)
 if observed or not G.ready or not A.coop.ready or not A.cargo.ready or
    not A.trade.state.ready or not A.quest.ready or not A.state.client or not A.state.client.lease then return end
 observed=true
 local own=cfg.slot=='p1' and 1 or 2
 local controlled=C.GetPlayerControlledShipID()
 DebugError(string.format('[X4LAN] SEED_PLAY_READY seed=%s slot=%s controlled=%s ownAlive=%s proxyAlive=%s',
  cfg.seed_id,cfg.slot,tostring(controlled==A.coop.ships[own]),
  tostring(C.IsComponentOperational(A.coop.ships[own])),tostring(C.IsComponentOperational(A.coop.ships[3-own]))))
 AddUITriggeredEvent('X4LAN_SeedPlayProbe','Read')
end
