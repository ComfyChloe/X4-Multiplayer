"""Default-enabled engine observer, STATEHASH recorder and MoveWorker seed (engine doubles only)."""
from pathlib import Path
import unittest

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension/x4_lan_lab/ui/engine_hooks.lua"

MOCK = r'''
logs, events = {}, {}
savePath = "C:/Users/x/Documents/Egosoft/X4/45841/save"
callback, cadence, nativeStarts = nil, nil, 0
wallNow, wallDt = 0, 0.001
ships = {
 [1001]={sector=22,macro='ship_arg_m_trans_01_a_macro',owner='argon',idcode='AAA-1',pos={x=1,y=2,z=3,yaw=0,pitch=0,roll=0}},
 [1002]={sector=22,macro='ship_arg_s_fighter_01_a_macro',owner='player',idcode='PLAY-1',pos={x=4,y=5,z=6,yaw=0,pitch=0,roll=0}},
}
api = {
 GetSaveFolderPath=function() return savePath end,
 IsPlayerValid=function() return true end,
 IsComponentOperational=function(id) return ships[id] ~= nil end,
 GetContextByClass=function(id, class) if class=='sector' then return ships[id].sector end return 0 end,
 GetObjectPositionInSector=function(id) return ships[id].pos end,
 GetAllFactions=function(buf, n, include) buf[0]='argon'; return 1 end,
 GetNumAllFactionShips=function(faction) return 2 end,
 GetAllFactionShips=function(buf, n, faction) buf[0]=1001;buf[1]=1002;return 2 end,
}
native = {
 start=function(cb, rate) nativeStarts=nativeStarts+1;callback=cb;cadence=rate
  return {stop=function() end,active=function() return true end,frames=function() return 0 end} end,
 inspect=function(id) return {attention=9,hull=50,shield=75,owner_pointer='0000000000001234',generation=3,registry=true} end,
 walltime=function() wallNow=wallNow+wallDt;return wallNow end,
 hash=function(rows)
  local value=14695981039346656037ULL;local b=require('bit')
  for _,row in ipairs(rows) do for i=1,#row do value=(b.bxor(value,row:byte(i))*1099511628211ULL) end
   value=(b.bxor(value,10)*1099511628211ULL)
  end
  return string.format('%08x%08x',tonumber(value/4294967296ULL),tonumber(value%4294967296ULL))
 end,
}
local realRequire = require
require = function(name)
 if name=="ffi" then return {os="Windows",C=api,typeof=function() error('unknown') end,
  cdef=function() end,string=function(s) return s end,new=function(name)
   if name=='const char*[256]' then return {} end
   if name:match('^UniverseID') then return {} end
   error('unexpected ffi type '..name)
  end} end
 return realRequire(name)
end
package.loadlib=function(path,symbol)
 assert(symbol=='luaopen_x4lan_hooks')
 return function() return native end
end
DebugError=function(msg) logs[#logs+1]=msg end
GetCurRealTime=function() return 12.5 end
GetUISafeModeOption=function() return false end
ConvertStringToLuaID=function(s) return tonumber(s) end
ConvertIDTo64Bit=function(v) return v end
GetComponentData=function(id, ...)
 local out={};local s=ships[id]
 for i,field in ipairs({...}) do
  if id==22 then out[i]=(field=='macro') and 'cluster_14_sector001_macro' or nil
  elseif s then out[i]=s[field] end
 end
 return unpack(out,1,select('#',...))
end
RegisterEvent=function(name,fn) events[name]=fn end
SetScript=function(name,fn) onUpdate=fn end
AddUITriggeredEvent=function(screen,control) events['X4LAN.Loaded']() end
X4LAN_CONFIG={enabled=false,test_profile="45841",slot="p1",engine_hooks=true,build="test-build",
 engine_hash_sectors={'cluster_14_sector001_macro'}}
'''


@unittest.skipUnless(LuaRuntime, "LuaJIT unavailable")
class EngineHookTests(unittest.TestCase):
    def runtime(self, setup=""):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK)
        lua.execute(setup)
        lua.execute(SOURCE.read_text(encoding="utf-8"))
        return lua

    def logs(self, lua):
        return [lua.eval("logs")[i] for i in range(1, len(lua.eval("logs")) + 1)]

    def state_lines(self, lua):
        return [line for line in self.logs(lua) if "STATEHASH" in line]

    def cost_lines(self, lua):
        return [line for line in self.logs(lua) if "HASHCOST" in line]

    def fields(self, line, kind="STATEHASH"):
        return dict(part.split("=", 1) for part in line.split(kind + " ", 1)[1].split())

    def test_default_enabled_and_registered(self):
        config = (ROOT / "extension/x4_lan_lab/ui/config.lua").read_text()
        self.assertIn("engine_hooks = true", config)
        self.assertEqual((ROOT / "extension/x4_lan_lab/ui.xml").read_text().count('name="ui/engine_hooks.lua"'), 1)

    def test_start_and_deterministic_complete_hash(self):
        lua = self.runtime()
        lua.eval("events['X4LAN.Loaded']")()
        self.assertEqual(lua.eval("nativeStarts"), 1)
        self.assertEqual(lua.eval("cadence"), 30)
        self.assertIn("HOOKS_READY", self.logs(lua)[-1])
        lua.eval("callback")(1, 100.0, 1 / 60, "0123456789abcdef", "0011223344556677")
        first = self.fields(self.state_lines(lua)[0])
        self.assertEqual((first["complete"], first["fresh"]), ("1", "1"))
        self.assertEqual((first["ships"], first["weak"]), ("1", "0"))
        for name in ("h", "identity", "pose", "health", "rng", "time"):
            self.assertRegex(first[name], "^[0-9a-f]{16}$")
        lua.eval("callback")(2, 100.0, 1 / 60, "0123456789abcdef", "0011223344556677")
        second = self.fields(self.state_lines(lua)[1])
        self.assertEqual(first["h"], second["h"])
        self.assertIn("HOOKS_OBSERVE", "\n".join(self.logs(lua)))

    def test_cadence_override_and_wall_cost_accounting(self):
        lua = self.runtime("X4LAN_CONFIG.engine_hash_cadence=1")
        lua.eval("events['X4LAN.Loaded']")()
        self.assertEqual(lua.eval("cadence"), 1)
        for frame in (1, 2):
            lua.eval("callback")(frame, 1000000.0 + frame, 500.0, "0123456789abcdef", "0011223344556677")
        first, second = [self.fields(line, "HASHCOST") for line in self.cost_lines(lua)]
        self.assertEqual((first["captures"], first["completed"], first["failed"]), ("1", "1", "0"))
        self.assertEqual((second["captures"], second["completed"], second["failed"]), ("2", "2", "0"))
        self.assertEqual((first["scanned"], first["ships"], first["cadence"]), ("2", "1", "1"))
        for field in ("capture_ms", "hash_ms", "total_ms", "max_ms", "cumulative_ms"):
            self.assertGreater(float(first[field]), 0)
        self.assertGreater(float(second["cumulative_ms"]), float(first["cumulative_ms"]))
        self.assertNotEqual(float(first["total_ms"]), 500.0)

    def test_capture_budget_is_incomplete_then_recovers(self):
        lua = self.runtime("X4LAN_CONFIG.engine_hash_budget_ms=1")
        lua.eval("events['X4LAN.Loaded']")()
        lua.eval("callback")(30, 100.0, 0.1, "0123456789abcdef", "0011223344556677")
        failed = self.fields(self.state_lines(lua)[0])
        self.assertEqual((failed["complete"], failed["fresh"], failed["reason"]), ("0", "0", "capture%20budget%20exceeded"))
        self.assertNotIn("h", failed)
        cost = self.fields(self.cost_lines(lua)[0], "HASHCOST")
        self.assertEqual((cost["complete"], cost["fresh"], cost["failed"]), ("0", "0", "1"))
        lua.execute("wallDt=0.0001")
        lua.eval("callback")(60, 100.1, 0.1, "0123456789abcdef", "0011223344556677")
        recovered = self.fields(self.state_lines(lua)[1])
        self.assertEqual((recovered["complete"], recovered["fresh"]), ("1", "1"))
        self.assertIn("h", recovered)

    def test_hash_budget_cannot_emit_a_complete_hash(self):
        lua = self.runtime("X4LAN_CONFIG.engine_hash_budget_ms=2.5")
        lua.eval("events['X4LAN.Loaded']")()
        lua.eval("callback")(30, 100.0, 0.1, "0123456789abcdef", "0011223344556677")
        failed = self.fields(self.state_lines(lua)[0])
        self.assertEqual((failed["complete"], failed["fresh"], failed["reason"]), ("0", "0", "hash%20budget%20exceeded"))
        self.assertNotIn("h", failed)
        self.assertGreater(float(self.fields(self.cost_lines(lua)[0], "HASHCOST")["hash_ms"]), 0)

    def test_pose_health_and_rng_changes_are_bucketed(self):
        lua = self.runtime()
        lua.eval("events['X4LAN.Loaded']")()
        lua.eval("callback")(1, 100.0, 0.1, "0123456789abcdef", "0011223344556677")
        first = self.fields(self.state_lines(lua)[0])
        lua.execute("ships[1001].pos.x=2")
        lua.eval("callback")(2, 100.1, 0.1, "0123456789abcdef", "0011223344556677")
        pose = self.fields(self.state_lines(lua)[1])
        self.assertNotEqual(first["pose"], pose["pose"])
        self.assertEqual(first["identity"], pose["identity"])
        self.assertNotEqual(first["time"], pose["time"])
        lua.execute("native.inspect=function(id) return {attention=9,hull=49,shield=75,owner_pointer='1',generation=3} end")
        lua.eval("callback")(3, 100.2, 0.1, "0123456789abcdef", "0011223344556677")
        health = self.fields(self.state_lines(lua)[2])
        self.assertNotEqual(pose["health"], health["health"])
        lua.eval("callback")(4, 100.2, 0.0, "fedcba9876543210", "0011223344556677")
        rng = self.fields(self.state_lines(lua)[3])
        self.assertEqual(health["identity"], rng["identity"])
        self.assertNotEqual(health["rng"], rng["rng"])
        self.assertNotEqual(health["h"], rng["h"])

    def test_weak_identity_and_incomplete_capture(self):
        lua = self.runtime("ships[1001].idcode=''")
        lua.eval("events['X4LAN.Loaded']")()
        lua.eval("callback")(1, 100.0, 0.1, "0123456789abcdef", "0011223344556677")
        self.assertEqual(self.fields(self.state_lines(lua)[0])["weak"], "1")
        lua = self.runtime("X4LAN_CONFIG.engine_hash_max_ships=1")
        lua.eval("events['X4LAN.Loaded']")()
        lua.eval("callback")(1, 100.0, 0.1, "0123456789abcdef", "0011223344556677")
        row = self.fields(self.state_lines(lua)[0])
        self.assertEqual((row["complete"], row["fresh"]), ("0", "0"))
        self.assertNotIn("h", row)

    def test_onupdate_requests_readiness_without_network_adapter(self):
        lua = self.runtime()
        lua.eval("onUpdate")()
        self.assertEqual(lua.eval("nativeStarts"), 1)
        self.assertIn("HOOKS_READY", self.logs(lua)[-1])

    def test_loading_is_retried_without_disabling(self):
        lua = self.runtime('''native.start=function(cb,rate)
          nativeStarts=nativeStarts+1
          if nativeStarts==1 then return false,'loading' end
          callback=cb
          return {stop=function() end,active=function() return true end,frames=function() return 0 end}
        end''')
        lua.eval("events['X4LAN.Loaded']")()
        self.assertNotIn("HOOKS_DISABLED", "\n".join(self.logs(lua)))
        lua.eval("events['X4LAN.Loaded']")()
        self.assertEqual(lua.eval("nativeStarts"), 2)
        self.assertIn("HOOKS_READY", self.logs(lua)[-1])

    def test_wrong_profile_and_native_refusal_disable(self):
        lua = self.runtime('savePath="C:/Users/x/Documents/Egosoft/X4/70000001/save"')
        self.assertEqual(lua.eval("nativeStarts"), 0)
        self.assertIn("HOOKS_DISABLED", self.logs(lua)[-1])
        lua = self.runtime("native.start=function() return false,'bad build' end")
        lua.eval("events['X4LAN.Loaded']")()
        self.assertIn("HOOKS_DISABLED reason=bad%20build", self.logs(lua)[-1])

    def test_lua_has_no_gameplay_writes_or_transport(self):
        text = SOURCE.read_text()
        for forbidden in ("SetObjectSectorPos", "RemoveComponent", "SpawnObject", "ActivateObject",
                          "SetOrder", "transport", "Pause(", "set_object_hull"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
