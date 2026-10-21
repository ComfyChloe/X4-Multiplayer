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

    def test_marker_without_native_completion_waits_then_times_out(self):
        lua = self.make()
        lua.execute("""
local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|0')
assert(not g.failed and not g.ready and #loads==0)
g.frame(181);assert(g.failed and not g.ready)
""")

    def test_active_reload_is_rejected(self):
        lua = self.make()
        lua.execute("""
local g=X4LAN_SEED_GATE
events['X4LAN_Seed.Loaded']();g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1');assert(g.ready)
events['X4LAN_Seed.Loaded']();assert(g.failed and not g.ready)
""")

    def test_marker_readable_before_native_completion_stays_pending(self):
        lua = self.make()
        lua.execute("""
local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,'|1|0')
g.frame(2);events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|0')
assert(not g.failed and not g.ready and #loads==1)
g.frame(3);events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1')
assert(g.ready and not g.failed and #loads==1)
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

    def attachment(self, extra=''):
        return self.make('''
X4LAN_CONFIG.attachment_sync=true;X4LAN_CONFIG.seed_binding=true
pauseCalls=0;resumeCalls=0;dockReady=false;dockFrames=0
function Pause() paused=true;pauseCalls=pauseCalls+1 end
function Unpause() paused=false;resumeCalls=resumeCalls+1 end
X4LAN_SEED_DOCK_GATE={frame=function()
 dockFrames=dockFrames+1
 if not dockReady then assert(paused,'import must remain frozen') end
 return dockReady
end}
'''+extra)

    def test_attachment_marker_alone_does_not_release_pause_or_spawn_fixtures(self):
        lua=self.attachment()
        lua.execute((UI/'bubble_game.lua').read_text())
        lua.execute('''
events['X4LAN.Loaded']();now=10;frames.onUpdate();now=11;frames.onUpdate()
assert(paused and pauseCalls==1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1')
now=12;frames.onUpdate();assert(not X4LAN_SEED_GATE.ready and count('SetupHost')==0 and positions[42]==nil)
assert(dockFrames==1 and paused)
dockReady=true;now=13;frames.onUpdate()
assert(X4LAN_SEED_GATE.ready and not paused and resumeCalls==1 and count('SetupHost')==1)
now=14;frames.onUpdate();assert(resumeCalls==1 and dockFrames==3)
''')

    def test_attachment_load_notification_and_first_valid_frame_freeze_before_inspection(self):
        self.attachment().execute('''
events['X4LAN_Seed.Loaded']();assert(paused and pauseCalls==1)
paused=false;X4LAN_SEED_GATE.frame(1);assert(paused and pauseCalls==2 and dockFrames==0)
''')

    def test_attachment_admission_preserves_preexisting_pause(self):
        self.attachment('paused=true').execute('''
local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1');dockReady=true;g.frame(2)
assert(g.ready and paused and pauseCalls==0 and resumeCalls==0)
''')

    def test_attachment_missing_failed_or_lost_registry_never_admits_gameplay(self):
        for change in ('X4LAN_SEED_DOCK_GATE=nil',
                       "X4LAN_SEED_DOCK_GATE.failed='changed witness'",
                       "X4LAN_SEED_DOCK_GATE.frame=function() error('bad import') end"):
            with self.subTest(change=change):
                self.attachment().execute('''
local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1')
'''+change+";g.frame(2);assert(g.failed and not g.ready and paused and resumeCalls==0)")
        self.attachment().execute('''
local g=X4LAN_SEED_GATE;g.frame(0);g.frame(1)
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|1');dockReady=true;g.frame(2)
assert(g.ready and not paused);dockReady=false
X4LAN_SEED_DOCK_GATE.failed='query witness changed';g.frame(3)
assert(g.failed and not g.ready);g.frame(4);assert(paused)
''')

    def test_attachment_does_not_inspect_until_native_pause_is_observed(self):
        self.attachment('function Pause() pauseCalls=pauseCalls+1 end').execute('''
local g=X4LAN_SEED_GATE;g.frame(0)
assert(not paused and pauseCalls==1 and count('Inspect')==0 and dockFrames==0)
g.frame(.5);assert(not paused and count('Inspect')==0 and dockFrames==0)
paused=true;g.frame(1);g.frame(2)
assert(count('Inspect')==1 and dockFrames==0)
''')


if __name__ == "__main__":
    unittest.main()
