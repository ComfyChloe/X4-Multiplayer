"""LuaJIT admission checks. Native loading remains a separate engine test."""
from pathlib import Path
import unittest
from test_game_adapter import MOCK, LuaRuntime

UI = Path(__file__).resolve().parents[1] / "extension/x4_lan_lab/ui"
SETUP = """
X4LAN_CONFIG.bubble_actions=true;X4LAN_CONFIG.coop=true;X4LAN_CONFIG.world_sync=true
X4LAN_CONFIG.test_profile='45841';X4LAN_CONFIG.seed_id=string.rep('b',32)
X4LAN_CONFIG.bubble=X4LAN_CONFIG.seed_id;X4LAN_CONFIG.seed_verified=true
requests,loads={},{}
saveComplete=true;hasSave=true
api.IsSaveListLoadingComplete=function() return saveComplete end
api.ReloadSaveList=function() end
GetSaveList=function() return hasSave and {{filename='save_010'}} or {} end
LoadGame=function(name) loads[#loads+1]=name end
AddUITriggeredEvent=function(screen,control,value) requests[#requests+1]={screen,control,value} end
function count(name) local n=0;for _,r in ipairs(requests) do if r[2]==name then n=n+1 end end;return n end
"""


@unittest.skipUnless(LuaRuntime, "LuaJIT unavailable")
class SeedGateTests(unittest.TestCase):
    def make(self, extra=""):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK + SETUP + extra)
        lua.execute((UI / "seed_game.lua").read_text())
        return lua

    def test_fresh_world_must_load_expected_pristine_seed_before_ready(self):
        lua = self.make()
        lua.execute("""
local g=X4LAN_SEED_GATE
assert(not g.frame(0));assert(not g.frame(1));assert(count('Inspect')==1)
events['X4LAN_Seed.State'](nil,'|1|0')
assert(#loads==1 and loads[1]=='save_010' and not g.ready)
g.frame(2);events['X4LAN_Seed.State'](nil,'|1|0')
assert(not g.ready and #loads==1)
for i=3,20 do assert(not g.frame(i)) end
assert(#loads==1)
events['X4LAN_Seed.Loaded']();g.frame(21)
assert(not g.ready)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1')
assert(g.ready and g.frame(22) and not g.failed)
""")

    def test_unverified_mismatched_session_and_normal_profile_fail_closed(self):
        for setup in ("X4LAN_CONFIG.seed_verified=false",
                      "X4LAN_CONFIG.bubble='another-session'",
                      "X4LAN_CONFIG.seed_id='../save'",
                      "api.GetSaveFolderPath=function() return '/X4/personal/save' end"):
            with self.subTest(setup=setup):
                lua = self.make(setup)
                lua.execute("assert(X4LAN_SEED_GATE.failed and not X4LAN_SEED_GATE.ready and #loads==0)")

    def test_missing_slot_and_timeout_do_not_start_gameplay(self):
        lua = self.make("hasSave=false")
        lua.execute("local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1);assert(g.failed and #loads==0)")
        lua = self.make("saveComplete=false")
        lua.execute("local g=X4LAN_SEED_GATE;g.frame(0);g.frame(181);assert(g.failed and not g.ready and #loads==0)")

    def test_wrong_used_or_malformed_loaded_world_is_rejected(self):
        for payload in ("c" * 32 + "|1|1", "b" * 32 + "|0|1", "|1|1",
                        "b" * 32 + "|1|2", "garbage"):
            with self.subTest(payload=payload):
                lua = self.make()
                lua.execute("events['X4LAN_Seed.Loaded']();X4LAN_SEED_GATE.frame(0);X4LAN_SEED_GATE.frame(1)")
                lua.globals().events["X4LAN_Seed.State"](None, payload)
                lua.execute("assert(X4LAN_SEED_GATE.failed and not X4LAN_SEED_GATE.ready and #loads==0)")

    def test_marker_without_native_load_and_active_reload_are_rejected(self):
        lua = self.make()
        lua.execute("""
local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|0')
assert(g.failed and not g.ready)
""")
        lua = self.make()
        lua.execute("""
local g=X4LAN_SEED_GATE
events['X4LAN_Seed.Loaded']();g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1');assert(g.ready)
events['X4LAN_Seed.Loaded']();assert(g.failed and not g.ready)
""")

    def test_bubble_does_not_spawn_or_request_setup_while_gate_is_pending(self):
        lua = self.make()
        lua.execute((UI / "bubble_game.lua").read_text())
        lua.execute("""
events['X4LAN.Loaded']();now=10;frames.onUpdate();now=11;frames.onUpdate()
events['X4LAN_Seed.State'](nil,'|1|0');now=30;frames.onUpdate()
assert(count('SetupHost')==0 and count('PrepareShips')==0 and positions[42]==nil)
events['X4LAN_Seed.Loaded']();now=31;frames.onUpdate()
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1');frames.onUpdate()
assert(count('SetupHost')==1 and positions[42]~=nil)
""")

    def test_missing_gate_stops_bubble_instead_of_starting_an_independent_world(self):
        lua = self.make()
        lua.execute("X4LAN_SEED_GATE=nil")
        lua.execute((UI / "bubble_game.lua").read_text())
        lua.execute("""
events['X4LAN.Loaded']();now=10;frames.onUpdate()
assert(X4LAN_BUBBLE_API.state.failed and count('SetupHost')==0 and positions[42]==nil)
""")

    def test_md_load_state_survives_missed_lua_notification(self):
        lua = self.make()
        lua.execute("""
local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1')
assert(g.ready and not g.failed and #loads==0)
""")

    def test_seed_cannot_fall_back_to_legacy_game_adapter(self):
        lua = self.make("X4LAN_CONFIG.bubble_actions=false")
        lua.execute((UI / "game.lua").read_text())
        lua.execute("assert(frames.onUpdate==nil and positions[42]==nil and #loads==0)")


if __name__ == "__main__":
    unittest.main()
