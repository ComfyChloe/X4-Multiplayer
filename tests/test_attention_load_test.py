"""Attention load test driver under real LuaJIT with a fake hooks adapter and MD helper."""
from pathlib import Path
import re
import unittest

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dev/attention_load_test.lua"
MD = ROOT / "dev/attention_load_test.xml"

MOCK = r'''
logs, events, now, gt = {}, {}, 0, 0
mdCalls, mdSilent = {}, false
-- 14 sectors; sector 101 is the busiest and holds the player.
world = {sectors={}, ships={}, stations={}}
for i=1,14 do
 local sid=100+i
 world.sectors[sid]={id=sid,macro=string.format('cluster_%02d_sector001_macro',i),ships={},stations={}}
 for j=1,(22-i) do
  local id=sid*1000+j
  world.ships[id]={sector=sid,owner=(j==1) and 'player' or 'argon',docked=(j%3==0),alive=true,xs=(j==2)}
  table.insert(world.sectors[sid].ships,id)
 end
 for k=1,((i<=7) and 2 or 1) do local id=sid*1000+900+k;world.stations[id]={sector=sid};table.insert(world.sectors[sid].stations,id) end
end
playerSector=101
attention={}
local function sectorByMacro(m) for _,s in pairs(world.sectors) do if s.macro==m then return s end end end
api = {
 IsComponentOperational=function(id) id=tonumber(id);local s=world.ships[id];return s~=nil and s.alive end,
 GetContextByClass=function(id,class) id=tonumber(id)
  if id==1 then return playerSector end
  local s=world.ships[id] or world.stations[id];return s and s.sector or 0 end,
 GetPlayerID=function() return 1 end,
 GetPlayerOccupiedShipID=function() return 0 end,
 IsComponentClass=function(id,class) id=tonumber(id);return class=='ship_xs' and world.ships[id] and world.ships[id].xs or false end,
}
local realRequire=require
require=function(name)
 if name=='ffi' then return {C=api,typeof=function() error('unknown') end,cdef=function() end} end
 return realRequire(name)
end
DebugError=function(msg) logs[#logs+1]=msg end
GetCurRealTime=function() return now end
GetCurTime=function() return gt end
ConvertStringToLuaID=function(s) return tonumber(s) end
ConvertIDTo64Bit=function(v) return v end
GetComponentData=function(id,...)
 local out={}
 for i,f in ipairs({...}) do
  local sector=world.sectors[id];local ship=world.ships[id]
  if sector and f=='macro' then out[i]=sector.macro
  elseif ship and f=='owner' then out[i]=ship.owner
  elseif ship and f=='isdocked' then out[i]=ship.docked end
 end
 return unpack(out,1,select('#',...))
end
RegisterEvent=function(name,fn) events[name]=fn end
SetScript=function(name,fn) onUpdate=fn end
local function fire(name,param) events[name](nil,param) end
AddUITriggeredEvent=function(screen,control,param)
 assert(screen=='X4LAN_AttnLoad')
 mdCalls[#mdCalls+1]=control
 if mdSilent then return end
 if control=='Rank' then
  for i=1,14 do local s=world.sectors[100+i]
   local alive=0;for _,id in ipairs(s.ships) do if world.ships[id].sector==s.id then alive=alive+1 end end
   fire('X4LAN_AttnLoad.Rank',string.format('%d|%s|%d|%d|%d',param,s.macro,#s.stations,alive,s.id==playerSector and 1 or 0)) end
  fire('X4LAN_AttnLoad.RankEnd',string.format('%d|14',param))
 elseif control=='Sector' then
  local s=sectorByMacro(param.macro)
  if not s then fire('X4LAN_AttnLoad.End',string.format('%d|%s|missing',param.seq,param.macro));return end
  local ships={};for id,ship in pairs(world.ships) do if ship.sector==s.id and ship.alive then ships[#ships+1]=id end end
  table.sort(ships)
  fire('X4LAN_AttnLoad.Begin',string.format('%d|%s|%d|%d|%d',param.seq,s.macro,#ships,#s.stations,s.id==playerSector and 1 or 0))
  fire('X4LAN_AttnLoad.SectorObject',s.id)
  for _,id in ipairs(s.stations) do fire('X4LAN_AttnLoad.Station',id) end
  for i=1,math.min(#ships,param.max) do fire('X4LAN_AttnLoad.Ship',ships[i]) end
  fire('X4LAN_AttnLoad.End',string.format('%d|%s|ok',param.seq,s.macro))
 end
end
X4LAN_ENGINE_HOOKS={owner={},failed=false,refusals=0,attentionSources='',
 attentionLevel=function(id) id=tonumber(id);if world.sectors[id] then return 5 end
  local s=world.ships[id] or world.stations[id];if not s then return nil,'component unavailable' end
  return attention[s.sector] or 2 end,
 attentionCounters=function() return {sources=#(X4LAN_ATTENTION_LOAD.sources),resolved=#(X4LAN_ATTENTION_LOAD.sources),raises=7} end}
X4LAN_CONFIG={attention_load_test=true}
function run(untilT,dt) dt=dt or 0.5;while now<untilT do now=now+dt;gt=gt+dt;onUpdate() end end
function sourceKeys() local t={};for i,id in ipairs(X4LAN_ATTENTION_LOAD.sources) do t[i]=tostring(id) end;return table.concat(t,',') end
'''


@unittest.skipUnless(LuaRuntime, "LuaJIT unavailable")
class AttentionLoadDriverTests(unittest.TestCase):
    def runtime(self, setup=""):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK)
        lua.execute(setup)
        lua.execute(SOURCE.read_text(encoding="utf-8"))
        return lua

    def logs(self, lua):
        table = lua.eval("logs")
        return [table[i] for i in range(1, len(table) + 1)]

    def lines(self, lua, kind):
        return [line for line in self.logs(lua) if line.startswith("[X4LAN] ATTNLOAD " + kind)]

    def start(self, lua):
        """Run until the first step begins and return its real time."""
        lua.eval("run")(40)
        return float(self.fields(self.lines(lua, "step=")[0])["t"])

    def fields(self, line):
        return dict(part.split("=", 1) for part in line.split() if "=" in part)

    def test_inert_without_flag(self):
        lua = LuaRuntime()
        lua.execute(MOCK)
        lua.execute("X4LAN_CONFIG={attention_load_test=false};onUpdate=nil")
        lua.execute(SOURCE.read_text(encoding="utf-8"))
        self.assertIsNone(lua.eval("X4LAN_ATTENTION_LOAD"))
        self.assertIsNone(lua.eval("onUpdate"))

    def test_default_schedule_baseline_then_eight_sectors(self):
        lua = self.runtime()
        base = self.start(lua)
        self.assertEqual(lua.eval("X4LAN_ATTENTION_LOAD.phase"), "run")
        self.assertIn("armed warmup=20 steps=0,8 durations=60,180", "\n".join(self.logs(lua)))
        steps = self.lines(lua, "step=")
        self.assertEqual(len(steps), 1)
        first = self.fields(steps[0])
        self.assertEqual((first["step"], first["proxies"], first["duration"]), ("0", "-", "60"))
        self.assertEqual(lua.eval("#X4LAN_ATTENTION_LOAD.sources"), 0)
        lua.eval("run")(base + 61)
        steps = self.lines(lua, "step=")
        self.assertEqual(len(steps), 2)
        eight = self.fields(steps[1])
        self.assertEqual((eight["step"], eight["duration"], eight["available"]), ("8", "180", "8"))
        sectors = eight["sectors"].split(",")
        proxies = eight["proxies"].split(",")
        self.assertEqual(len(set(sectors)), 8)
        self.assertEqual(len(set(proxies)), 8)
        self.assertNotIn("cluster_01_sector001_macro", sectors)  # player's sector
        # Busiest non-player sectors first.
        self.assertEqual(sectors[:3], ["cluster_02_sector001_macro", "cluster_03_sector001_macro",
                                       "cluster_04_sector001_macro"])
        for proxy in proxies:
            sid = int(proxy) // 1000
            self.assertEqual(f"cluster_{sid - 100:02d}_sector001_macro", sectors[proxies.index(proxy)])
            self.assertNotEqual(int(proxy) % 1000, 1)  # player-owned
            self.assertNotEqual(int(proxy) % 1000, 2)  # XS
            self.assertEqual(int(proxy) % 1000 % 3, 0)  # docked preferred
        self.assertEqual(lua.eval("#X4LAN_ATTENTION_LOAD.sources"), 8)
        lua.eval("run")(base + 61 + 181)
        self.assertEqual(lua.eval("X4LAN_ATTENTION_LOAD.phase"), "done")
        self.assertEqual(lua.eval("#X4LAN_ATTENTION_LOAD.sources"), 0)
        self.assertEqual(len(self.lines(lua, "done")), 1)

    def test_configurable_steps_and_durations(self):
        lua = self.runtime("X4LAN_CONFIG.attention_load_steps={0,1,2,4,8,0};X4LAN_CONFIG.attention_load_step_seconds=30;"
                           "X4LAN_CONFIG.attention_load_warmup=5")
        base = self.start(lua)
        lua.eval("run")(base + 6 * 30 + 3)
        steps = [self.fields(line)["step"] for line in self.lines(lua, "step=")]
        self.assertEqual(steps, ["0", "1", "2", "4", "8", "0"])
        self.assertEqual(lua.eval("X4LAN_ATTENTION_LOAD.phase"), "done")

    def test_tick_logs_current_sector_of_every_proxy(self):
        lua = self.runtime()
        base = self.start(lua)
        lua.eval("run")(base + 61 + 3)
        tick = self.lines(lua, "tick")[-1]
        self.assertRegex(tick, r"^\[X4LAN\] ATTNLOAD tick step=8 t=[\d.]+ gt=[\d.]+ proxies=(\d:\d+@cluster_\d\d_sector001_macro,){7}"
                               r"\d:\d+@cluster_\d\d_sector001_macro attn_sources=8 attn_resolved=8 attn_raises=7$")
        # Roughly one tick per second.
        ticks = [float(self.fields(line)["t"]) for line in self.lines(lua, "tick step=8")]
        self.assertGreaterEqual(len(ticks), 3)
        self.assertTrue(all(0.9 <= b - a <= 1.1 for a, b in zip(ticks, ticks[1:])))

    def test_dead_and_departed_proxies_are_replaced_in_the_same_sector(self):
        lua = self.runtime()
        base = self.start(lua)
        lua.eval("run")(base + 61 + 2)
        before = lua.eval("sourceKeys()").split(",")
        dead, gone = int(before[0]), int(before[1])
        lua.execute(f"world.ships[{dead}].alive=false;world.ships[{gone}].sector=114")
        lua.eval("run")(base + 61 + 4)
        replaced = self.lines(lua, "replace")
        reasons = {self.fields(line)["old"]: self.fields(line) for line in replaced}
        self.assertEqual(reasons[str(dead)]["reason"], "dead")
        self.assertEqual(reasons[str(gone)]["reason"], "left")
        for old in (dead, gone):
            new = int(reasons[str(old)]["new"])
            self.assertEqual(new // 1000, old // 1000)
            self.assertNotEqual(new, old)
        after = lua.eval("sourceKeys()").split(",")
        self.assertEqual(len(set(after)), 8)
        self.assertNotIn(str(dead), after)
        self.assertNotIn(str(gone), after)

    def test_exhausted_sector_is_refreshed_then_moved_to_a_spare_sector(self):
        lua = self.runtime()
        base = self.start(lua)
        lua.eval("run")(base + 61 + 2)
        lua.execute("for id,s in pairs(world.ships) do if s.sector==102 then s.alive=false end end")
        lua.eval("run")(base + 61 + 6)
        text = "\n".join(self.logs(lua))
        self.assertIn("pending=refresh", text)
        self.assertIn("resector slot=1 from=cluster_02_sector001_macro to=cluster_10_sector001_macro", text)
        sectors = {int(p) // 1000 for p in lua.eval("sourceKeys()").split(",")}
        self.assertEqual(len(sectors), 8)
        self.assertNotIn(102, sectors)
        self.assertIn(110, sectors)

    def test_refused_registration_blacklists_and_replaces_new_proxy(self):
        lua = self.runtime()
        base = self.start(lua)
        lua.eval("run")(base + 61 + 2)
        keys = lua.eval("sourceKeys()").split(",")
        lua.execute(f"X4LAN_ENGINE_HOOKS.attentionSources='{','.join(keys[1:])}';X4LAN_ENGINE_HOOKS.refusals=1")
        lua.eval("run")(base + 61 + 4)
        refused = [self.fields(l) for l in self.lines(lua, "replace") if "reason=refused" in l]
        self.assertEqual([r["old"] for r in refused], [keys[0]])
        self.assertNotIn(keys[0], lua.eval("sourceKeys()").split(","))

    def test_census_counts_high_attention_objects(self):
        lua = self.runtime("attention[102]=9;attention[103]=9")
        base = self.start(lua)
        lua.eval("run")(base + 61 + 30)
        census = [self.fields(l) for l in self.lines(lua, "census ")]
        baseline = [c for c in census if c["role"] == "baseline" and c["round"] == "1"]
        proxy = [c for c in census if c["role"] == "proxy"]
        self.assertEqual(len(baseline), 8)
        self.assertTrue(proxy)
        by_sector = {c["sector"]: c for c in proxy}
        two = by_sector["cluster_02_sector001_macro"]
        self.assertEqual(int(two["hi9"]), int(two["read"]))
        self.assertEqual(two["levels"].count("."), 11)
        self.assertEqual(by_sector["cluster_05_sector001_macro"]["hi9"], "0")
        totals = [self.fields(l) for l in self.lines(lua, "census_total")]
        eight = [t for t in totals if t["step"] == "8"]
        self.assertEqual(eight[0]["proxy_sectors"], "8")
        self.assertEqual(int(eight[0]["hi9"]), int(two["hi9"]) + int(by_sector["cluster_03_sector001_macro"]["hi9"]))
        # Every MD request is bracketed for the report's exclusion window.
        self.assertEqual(len(self.lines(lua, "scan_begin")), len(self.lines(lua, "scan_end")))

    def test_md_timeouts_fail_cleanly(self):
        lua = self.runtime("mdSilent=true")
        lua.eval("run")(60)
        self.assertEqual(lua.eval("X4LAN_ATTENTION_LOAD.phase"), "failed")
        self.assertIn("ATTNLOAD error reason=md_helper_unavailable", self.logs(lua)[-1])

    def test_hooks_failure_stops_driver(self):
        lua = self.runtime()
        base = self.start(lua)
        lua.eval("run")(base + 61 + 2)
        lua.execute("X4LAN_ENGINE_HOOKS.failed=true")
        lua.eval("run")(base + 61 + 3)
        self.assertEqual(lua.eval("X4LAN_ATTENTION_LOAD.phase"), "failed")
        self.assertEqual(lua.eval("#X4LAN_ATTENTION_LOAD.sources"), 0)

    def test_invalid_step_is_rejected(self):
        lua = self.runtime("X4LAN_CONFIG.attention_load_steps={0,9}")
        lua.eval("run")(2)
        self.assertIn("error reason=invalid_step_9", self.logs(lua)[-1])

    def test_explicit_sector_list(self):
        lua = self.runtime("X4LAN_CONFIG.attention_load_sectors={'cluster_09_sector001_macro','cluster_01_sector001_macro','cluster_07_sector001_macro'};"
                           "X4LAN_CONFIG.attention_load_steps={0,2}")
        base = self.start(lua)
        lua.eval("run")(base + 61 + 2)
        step = self.fields(self.lines(lua, "step=")[-1])
        self.assertEqual(step["sectors"], "cluster_09_sector001_macro,cluster_07_sector001_macro")

    def test_md_helper_is_read_only(self):
        text = MD.read_text(encoding="utf-8")
        for forbidden in ("create_", "destroy_", "set_owner", "warp", "move_", "add_cargo", "remove_"):
            self.assertNotIn("<" + forbidden, text)
        self.assertEqual(len(re.findall(r"<cue ", text)), 2)


if __name__ == "__main__":
    unittest.main()
