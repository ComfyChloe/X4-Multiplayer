"""Combat recorder lane script (dev/combat_scenario/combat_capture.lua) under real LuaJIT with a
fake native module, and the overlay builder. Never launches X4; the native module itself is
tested by `cargo test -p x4mp-combat` and native/engine/combat_check_lua.py."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dev' / 'combat_scenario' / 'combat_capture.lua'


def load_overlay():
    spec = importlib.util.spec_from_file_location('combat_capture_overlay', ROOT / 'dev' / 'combat_scenario' / 'capture_overlay.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


overlay = load_overlay()

STUBS = '''
  now=0;logs={};events={};starts={};writes={};stops=0
  function GetCurRealTime() return now end
  function DebugError(s) logs[#logs+1]=s end
  function SetScript(event,fn) assert(event=='onUpdate');ui=fn end
  function RegisterEvent(name,fn) events[name]=fn end
  stats={calls=10,recorded=8,dropped=0,foreign=1,shadowed=10,mismatched=0,skipped_mode=0,
    uninitialised_tls=0,mxcsr=0x1f80,mxcsr_changes=0,mode=0}
  handle={stats=function(self) return stats end,
    write=function(self,path) writes[#writes+1]=path;return stats.recorded end,
    stop=function(self) stops=stops+1;return true end}
  cstats={frames=3,weapon_calls=30,bullet_calls=12,shots=4,missile_shots=1,bullet_checks=5,bullet_hits=2,
    rng_weapon=4,rng_bullet=3,rng_frame=50,rng_frame_over_cap=0,rng_unmatched=0,foreign=0,uninitialised_tls=0,
    dropped_frames=0,ticks_per_second=3e9}
  censuses={}
  chandle={stats=function(self) return cstats end,summary=function(self) return {'shots mean=1.3','frame_ms mean=20'} end,
    write=function(self,path) writes[#writes+1]=path;return cstats.frames end,
    stop=function(self) stops=stops+1;return true end}
  function package.loadlib(path,symbol)
    assert(symbol=='luaopen_x4mp_combat');loaded=path
    return function() return {start=function(capacity,record,shadow,switch,verify)
      starts[#starts+1]={capacity=capacity,record=record,shadow=shadow,switch=switch,verify=verify}
      if refusal then return nil,refusal end
      return handle
    end,census=function(frames)
      censuses[#censuses+1]=frames
      if refusal then return nil,refusal end
      return chandle
    end} end
  end
  function find(text) for i=1,#logs do if logs[i]:find(text,1,true) then return i end end end
'''


class CaptureScript(unittest.TestCase):
    def runtime(self, config):
        lua = LuaRuntime()
        lua.execute(STUBS)
        lua.execute(config)
        lua.execute(SCRIPT.read_text())
        return lua

    def test_inert_without_configuration(self):
        lua = self.runtime('')
        lua.execute('assert(loaded==nil and ui==nil and next(events)==nil)')

    def test_arms_only_inside_the_window_and_writes_at_its_end(self):
        lua = self.runtime("X4MP_COMBATCAP={mode='both',capacity=1234,path='C:/p/x4mp-combatcap.txt'}")
        lua.execute('''
          ui();assert(#starts==0,'no arm before the window')
          events['X4MP.CombatScn'](nil,'begin')
          ui();assert(#starts==1);assert(starts[1].capacity==1234 and starts[1].record and starts[1].shadow)
          assert(find('FEATURE combatcap ENABLED record=true shadow=true switch=false verify=1 capacity=1234'))
          now=11;ui();assert(find('COMBATCAP stats calls=10 recorded=8 dropped=0 foreign=1 shadowed=10 mismatched=0'))
          events['X4MP.CombatScn'](nil,'end')
          assert(writes[1]=='C:/p/x4mp-combatcap.txt' and stops==1)
          assert(find('COMBATCAP written records=8 path=C:/p/x4mp-combatcap.txt'))
          assert(find('COMBATCAP stopped reason=window end'))
          events['X4MP.CombatScn'](nil,'begin');ui();assert(#starts==1,'one session per run')
        ''')

    def test_shadow_mode_does_not_record_or_write(self):
        lua = self.runtime("X4MP_COMBATCAP={mode='shadow'}")
        lua.execute('''
          events['X4MP.CombatScn'](nil,'begin');ui()
          assert(starts[1].record==false and starts[1].shadow==true and starts[1].capacity==200000)
          stats.mismatched=2;stats.first_mismatch='original=... rust=...'
          events['X4MP.CombatScn'](nil,'end')
          assert(#writes==0 and stops==1)
          assert(find('COMBATCAP final') and find('COMBATCAP first_mismatch original='))
        ''')

    def test_switch_mode_starts_authoritative_and_reports_a_switch_off(self):
        lua = self.runtime("X4MP_COMBATCAP={mode='switch',verify=7}")
        lua.execute('''
          events['X4MP.CombatScn'](nil,'begin');ui()
          assert(starts[1].record==false and starts[1].shadow==false)
          assert(find('FEATURE combatcap ENABLED record=false shadow=false switch=true verify=7'))
          stats.served=100;stats.verified=15;stats.switch_mismatched=1;stats.switch_off='verification mismatch'
          now=11;ui()
          assert(find('served=100 verified=15 switchMismatched=1 foreignClass=0 switchOff=verification mismatch'))
          assert(find('FEATURE ikjoint DISABLED reason=verification mismatch'))
          events['X4MP.CombatScn'](nil,'end');assert(#writes==0 and stops==1)
        ''')

    def test_census_mode_logs_stats_summary_and_writes_its_table(self):
        lua = self.runtime("X4MP_COMBATCAP={mode='census',capacity=500,path='C:/p/x4mp-combatcensus.txt'}")
        lua.execute('''
          ui();assert(#censuses==0,'no arm before the window')
          events['X4MP.CombatScn'](nil,'begin');ui()
          assert(#censuses==1 and censuses[1]==500 and #starts==0)
          assert(find('FEATURE combatcensus ENABLED frames=500'))
          now=11;ui()
          assert(find('COMBATCENSUS stats frames=3 weaponCalls=30 bulletCalls=12 shots=4 missileShots=1 checks=5 hits=2 rngWeapon=4 rngBullet=3 rngFrame=50'))
          events['X4MP.CombatScn'](nil,'end')
          assert(find('COMBATCENSUS final frames=3'))
          assert(find('COMBATCENSUS summary shots mean=1.3') and find('COMBATCENSUS summary frame_ms mean=20'))
          assert(writes[1]=='C:/p/x4mp-combatcensus.txt' and find('COMBATCENSUS written frames=3 path=C:/p/x4mp-combatcensus.txt'))
          assert(stops==1 and find('COMBATCENSUS stopped reason=window end'))
        ''')
        lua = self.runtime("X4MP_COMBATCAP={mode='census'};refusal='vtable entry differs from the pinned build'")
        lua.execute('''
          events['X4MP.CombatScn'](nil,'begin');ui()
          assert(censuses[1]==20000)
          assert(find('FEATURE combatcensus DISABLED reason=vtable entry differs from the pinned build'))
        ''')

    def test_refusal_and_failure_are_loud(self):
        lua = self.runtime("X4MP_COMBATCAP={mode='capture',path='x'};refusal='unsupported executable (SHA-256)'")
        lua.execute('''
          events['X4MP.CombatScn'](nil,'begin');ui()
          assert(find('FEATURE combatcap DISABLED reason=unsupported executable (SHA-256)'))
          ui();assert(#starts==1)
        ''')
        lua = self.runtime("X4MP_COMBATCAP={mode='capture',path='x'}")
        lua.execute('''
          events['X4MP.CombatScn'](nil,'begin');ui()
          stats.failed='patch bytes changed';now=20;ui()
          assert(find('failed=patch bytes changed') and stops==1 and find('stopped reason=failed'))
        ''')


class Overlay(unittest.TestCase):
    def test_builds_and_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dll = base / 'x4mp_combat.dll'
            dll.write_bytes(b'MZ fake')
            out = overlay.build(base / 'ov', dll, 'both', 5000)
            self.assertEqual((out / 'native' / 'x4mp_combat_64.dll').read_bytes(), b'MZ fake')
            self.assertEqual((out / 'ui.xml.files').read_text().split(), ['ui/combat_capture.lua'])
            self.assertIn("X4MP_COMBATCAP = {mode='both', capacity=5000, verify=1}", (out / 'ui' / 'config.lua.append').read_text())
            switched = overlay.build(base / 'sw', dll, 'switch', 10, 7)
            self.assertIn("mode='switch', capacity=10, verify=7", (switched / 'ui' / 'config.lua.append').read_text())
            with self.assertRaises(ValueError):
                overlay.build(base / 'sw2', dll, 'switch', 10, -1)
            census = overlay.build(base / 'cs', dll, 'census', 30000)
            self.assertIn("mode='census', capacity=30000", (census / 'ui' / 'config.lua.append').read_text())
            self.assertIn('data-only vtable hooks', (census / 'x4lane-overlay.txt').read_text())
            with self.assertRaises(ValueError):
                overlay.build(base / 'cs2', dll, 'census', 1_000_001)
            self.assertEqual((out / 'ui' / 'combat_capture.lua').read_text(), SCRIPT.read_text())
            overlay.build(base / 'ov', dll, 'shadow', 10)  # rebuild over its own output
            with self.assertRaises(ValueError):
                overlay.build(base / 'ov2', dll, 'nope', 10)
            with self.assertRaises(ValueError):
                overlay.build(base / 'ov2', dll, 'both', 0)
            foreign = base / 'foreign'
            foreign.mkdir()
            with self.assertRaises(FileExistsError):
                overlay.build(foreign, dll, 'both', 10)
            with self.assertRaises(FileNotFoundError):
                overlay.build(base / 'ov3', base / 'missing.dll', 'both', 10)


if __name__ == '__main__':
    unittest.main()
