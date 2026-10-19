"""Engine API doubles test adapter behaviour, not X4 engine compatibility."""
from pathlib import Path
import unittest

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

GAME = Path(__file__).resolve().parents[1] / "extension/x4_lan_lab/ui/game.lua"
MOCK = '''
now, valid, paused, controlled, sector = 0, true, false, 11, 22
shipPos = {x=100, y=200, z=300, yaw=.1, pitch=.2, roll=.3}
remotePose = {1000, 0, 0, 3.1, .2, -.3}
positions, writes, logs, removed, frames, events = {}, {}, {}, {}, {}, {}
positions[11] = shipPos
noop, timeout, closeCount = false, false, 0
local function copy(t) local r={}; for k,v in pairs(t) do r[k]=v end; return r end
api = {
  IsPlayerValid=function() return valid end,
  IsGamePaused=function() return paused end,
  GetPlayerControlledShipID=function() return controlled end,
  GetPlayerOccupiedShipID=function() return controlled end,
  GetPlayerContainerID=function() return container or controlled end,
  GetContextByClass=function() return sector end,
  GetObjectPositionInSector=function(id) return copy(positions[id]) end,
  IsComponentOperational=function(id) return positions[id] ~= nil end,
  GetGameVersion=function() return {major=9,minor=0} end,
  GetSaveFolderPath=function() return "/isolated/X4/45841/save/" end,
  SpawnObjectAtPos2=function(macro, sector, pos, owner)
    assert(owner=="player"); positions[42]=copy(pos); return 42
  end,
  SetObjectSectorPos=function(id, sector, pos)
    assert(id==42); writes[#writes+1]=copy(pos)
    if not noop then positions[id]=copy(pos) end
  end,
  RemoveComponent=function(id) removed[#removed+1]=id; positions[id]=nil end,
}
local realRequire = require
require = function(name)
  if name=="ffi" then return {os="Linux", C=api, typeof=function(n) return n end,
    cdef=function() end, string=function(s) return s end, new=function(name,t) return copy(t) end} end
  return realRequire(name)
end
DebugError=function(msg) logs[#logs+1]=msg end
GetCurRealTime=function() return now end
GetCurTime=function() return now end
GetComponentData=function(id,key) return "sector_macro" end
ConvertStringToLuaID=function(s) return s end
RegisterEvent=function(name,fn) events[name]=fn end
AddUITriggeredEvent=function(screen,control) readyRequest={screen,control} end
SetScript=function(name,fn) frames[name]=fn end
X4LAN_CONFIG={enabled=true,host="127.0.0.1",port=45841,token=string.rep("a",32),
  build="test",bubble="bubble",slot="p1",mode="telemetry",proxy_macro="test_macro"}
X4LAN_TRANSPORT={open=function() return {close=function() closeCount=closeCount+1 end} end}
X4LAN_CLIENT={new=function()
  return {lease="mock",samples={},update=function(self, time, pose) lastPose=pose; return not timeout end,
    render=function() return remotePose end}
end}
'''


@unittest.skipUnless(LuaRuntime, "lupa.luajit21 not installed")
class GameAdapterTests(unittest.TestCase):
    def run_adapter(self, setup="", loaded=True):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK)
        lua.execute(setup)
        lua.execute(GAME.read_text())
        if loaded:
            lua.execute('if events["X4LAN.Loaded"] then events["X4LAN.Loaded"]() end')
        return lua

    def test_waits_for_world_load_before_connecting_or_spawning(self):
        lua = self.run_adapter('X4LAN_CONFIG.mode="proxy"', loaded=False)
        lua.execute('''
            frames.onUpdate()
            assert(#writes==0 and positions[42]==nil)
            assert(readyRequest[1]=="X4LAN" and readyRequest[2]=="Ready")
            events["X4LAN.Loaded"]()
            frames.onUpdate()
            assert(#writes==2 and positions[42]~=nil)
        ''')

    def test_refuses_to_activate_in_an_existing_player_profile(self):
        lua = self.run_adapter('''
            X4LAN_CONFIG.mode="proxy"
            api.GetSaveFolderPath=function() return "/X4/70000001/save/" end
        ''')
        lua.execute('''
            assert(frames.onUpdate==nil and positions[42]==nil)
            assert(logs[1]:find("inactive outside isolated test profile"))
        ''')
        lua = self.run_adapter('api.GetSaveFolderPath=function() return [[C:\\Users\\test\\X4\\45841\\save\\]] end')
        lua.execute('assert(frames.onUpdate~=nil)')

    def test_interaction_harness_hands_off_only_proxy_once(self):
        lua = self.run_adapter('X4LAN_CONFIG.mode="proxy"; X4LAN_CONFIG.interaction_test=true; requests={}\n'
            'AddUITriggeredEvent=function(screen,control,value) requests[#requests+1]={screen,control,value} end')
        lua.execute('frames.onUpdate(); now=1; frames.onUpdate()')
        self.assertEqual(lua.eval('#requests'), 1)
        self.assertEqual(lua.eval('requests[1][2]'), 'InteractionTest')
        self.assertEqual(lua.eval('requests[1][3]'), '42')
        self.assertEqual(lua.eval('#writes'), 0)
        self.assertIsNotNone(lua.eval('positions[11]'))

    def test_telemetry_is_readonly_and_pause_sends_idle(self):
        lua = self.run_adapter()
        lua.execute('''
            frames.onUpdate()
            assert(lastPose[1]==-1000)
            assert(#writes==0 and positions[42]==nil)
            paused=true; now=.1; frames.onUpdate()
            assert(lastPose==nil)
            assert(#writes==0 and #removed==0)
        ''')

    def test_only_proxy_moves_and_radians_convert_to_degrees(self):
        lua = self.run_adapter('X4LAN_CONFIG.mode="proxy"')
        lua.execute('''
            frames.onUpdate()
            assert(#writes==2)
            assert(writes[1].x==2125 and writes[2].x==2100)
            assert(math.abs(writes[2].yaw-3.1*180/math.pi)<.001)
            assert(positions[11].x==100)
            remotePose=nil; now=.1; frames.onUpdate()
            assert(#removed==1 and removed[1]==42)
        ''')

    def test_noop_setter_fails_the_displacement_probe(self):
        lua = self.run_adapter('X4LAN_CONFIG.mode="proxy"; noop=true')
        lua.execute('''
            frames.onUpdate()
            assert(#removed==1 and removed[1]==42)
            local stopped=false
            for _,message in ipairs(logs) do if message:find("STOPPED") then stopped=true end end
            assert(stopped)
            now=3; frames.onUpdate()
            assert(positions[42]==nil)
        ''')

    def test_reconnect_preserves_the_coordinate_origin(self):
        lua = self.run_adapter()
        lua.execute('''
            frames.onUpdate()
            timeout=true; now=.1; frames.onUpdate()
            assert(closeCount==1)
            shipPos.x=600
            timeout=false; now=2; frames.onUpdate()
            assert(lastPose[1]==-500)
        ''')

    def test_unloaded_world_prevents_game_queries_and_missing_export_stops_init(self):
        lua = self.run_adapter('valid=false; api.GetPlayerControlledShipID=function() error("world not ready") end')
        lua.execute('frames.onUpdate(); assert(#logs==1 and logs[1]:find("adapter armed"))')
        lua = self.run_adapter('api.GetObjectPositionInSector=nil')
        lua.execute('assert(frames.onUpdate==nil); assert(logs[1]:find("initialisation failed"))')


if __name__ == "__main__":
    unittest.main()
