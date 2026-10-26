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
  function package.loadlib(path,symbol)
    assert(symbol=='luaopen_x4mp_combat');loaded=path
    return function() return {start=function(capacity,record,shadow)
      starts[#starts+1]={capacity=capacity,record=record,shadow=shadow}
      if refusal then return nil,refusal end
      return handle
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
          assert(find('FEATURE combatcap ENABLED record=true shadow=true capacity=1234'))
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
            self.assertIn("X4MP_COMBATCAP = {mode='both', capacity=5000}", (out / 'ui' / 'config.lua.append').read_text())
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
