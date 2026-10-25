"""Driver ownership tests under real LuaJIT; native engine tests remain separate."""
from pathlib import Path
import unittest

from lupa.luajit21 import LuaRuntime

SOURCE = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui/simtick.lua'


class SimtickLuaTests(unittest.TestCase):
    def runtime(self):
        lua = LuaRuntime()
        lua.execute('''
          now=0; runs=0; attempts=0; stops=0; logs={}
          function GetCurRealTime() return now end
          function DebugError(s) logs[#logs+1]=s end
          function SetScript(event,fn) assert(event=='onUpdate'); ui=fn end
          function work() runs=runs+1 end
          cfg={test_profile='45841'}
          package.config=string.char(92)
          handle={armed=true,active=function(self) return self.armed end,
            stop=function(self) self.armed=false;stops=stops+1 end}
          function package.loadlib(path,symbol)
            assert(symbol=='luaopen_x4lan_simtick')
            return function() return {start=function(fn)
              attempts=attempts+1
              if refusal then return nil,refusal end
              nativeFrame=fn;return handle
            end} end
          end
        ''')
        lua.execute(SOURCE.read_text())
        return lua

    def test_native_owner_dispatches_without_ui_and_never_doubles(self):
        self.runtime().execute('''
          X4LAN_SIMTICK.register(work,cfg)
          ui();assert(attempts==1 and runs==0)
          nativeFrame();nativeFrame();assert(runs==2)
          ui();ui();assert(runs==2 and attempts==1)
          X4LAN_SIMTICK.stop();assert(stops==1)
          ui();assert(attempts==1 and runs==2)
        ''')

    def test_linux_retains_ui_without_loading_windows_dll(self):
        self.runtime().execute('''
          package.config='/'
          package.loadlib=function() error('unexpected native load') end
          X4LAN_SIMTICK.register(work,cfg);ui();assert(runs==1)
        ''')

    def test_loading_retries_are_bounded_then_native_takes_over(self):
        self.runtime().execute('''
          refusal='loading';X4LAN_SIMTICK.register(work,cfg)
          ui();ui();assert(attempts==1 and runs==0)
          now=1;refusal=nil;ui();nativeFrame()
          assert(attempts==2 and runs==1)
        ''')

    def test_build_refusal_does_not_fall_back_to_network_on_ui(self):
        self.runtime().execute('''
          refusal='unsupported executable';X4LAN_SIMTICK.register(work,cfg)
          ui();now=20;ui();assert(attempts==1 and runs==0)
        ''')

    def test_native_disarm_is_terminal(self):
        self.runtime().execute('''
          X4LAN_SIMTICK.register(work,cfg);ui();handle.armed=false
          ui();now=20;ui();assert(stops==1 and attempts==1 and runs==0)
        ''')

    def test_rejects_profile_and_duplicate_registration(self):
        self.runtime().execute("cfg.test_profile='70000001';assert(not pcall(X4LAN_SIMTICK.register,work,cfg))")
        self.runtime().execute('''
          X4LAN_SIMTICK.register(work,cfg)
          assert(not pcall(X4LAN_SIMTICK.register,work,cfg))
        ''')
