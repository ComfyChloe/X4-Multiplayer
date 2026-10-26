"""Opt-in fixed-tick probe lifecycle tests under real LuaJIT."""
from pathlib import Path
import unittest

from lupa.luajit21 import LuaRuntime

SOURCE = Path(__file__).resolve().parents[1] / 'dev/fixed_tick_probe.lua'


class FixedTickProbeTests(unittest.TestCase):
    def runtime(self):
        lua = LuaRuntime()
        lua.execute('''
          now=0;attempts=0;logs={};armed=true
          X4LAN_CONFIG={test_profile='45841'}
          function GetCurRealTime() return now end
          function DebugError(s) logs[#logs+1]=s end
          function SetScript(event,fn) assert(event=='onUpdate');ui=fn end
          stops=0;modes={}
          handle={active=function(self) return armed end,stop=function(self) stops=stops+1 end,
            stats=function(self)
            return {frames=2,wall_dt=.02,max_wall_dt=.03,game_dt=1/60,real_dt=.02,
              game_time=10,real_time=20,speed=1,paused_frames=0,loading_skips=0,
              game_time_regressions=0,fixed=fixed,fixed_frames=fixed and 1 or nil,
              wall_elapsed=fixed and .5 or nil,game_elapsed=fixed and .25 or nil,reason=reason}
          end}
          function package.loadlib(path,symbol)
            assert(symbol=='luaopen_x4lan_fixedtick')
            return function() return {start=function(...)
              assert(select('#',...)<=1);modes[#modes+1]=(...) or 'probe';attempts=attempts+1
              if refusal then return nil,refusal end
              return handle
            end} end
          end
        ''')
        lua.execute(SOURCE.read_text())
        return lua

    def test_registration_and_cadence_logging(self):
        self.runtime().execute('''
          ui();assert(attempts==1);assert(logs[1]:find('slot 5',1,true))
          ui();assert(logs[2]:find('FRAMEPROBE frames=2',1,true))
          assert(logs[2]:find('gameDt=0.016666667',1,true))
        ''')

    def test_probe_mode_passes_no_argument(self):
        self.runtime().execute('''
          ui();assert(modes[1]=='probe');assert(logs[1]:find('mode=probe',1,true))
          ui();assert(logs[2]:find('fixed=0 fixedFrames=0',1,true))
        ''')

    def test_fixed60_mode_is_requested_and_logged(self):
        self.runtime().execute('''
          X4LAN_FIXEDTICK_MODE='fixed60';fixed=1
          ui();assert(modes[1]=='fixed60');assert(logs[1]:find('mode=fixed60',1,true))
          ui();assert(logs[2]:find('fixed=1 fixedFrames=1 wallElapsed=0.500000 gameElapsed=0.250000',1,true))
        ''')

    def test_fixed60_refusal_is_an_error(self):
        with self.assertRaisesRegex(Exception, 'frame-rate limit is not 60'):
            self.runtime().execute('''
              X4LAN_FIXEDTICK_MODE='fixed60';refusal='frame-rate limit is not 60';ui()
            ''')

    def test_loading_retries_at_most_once_per_second(self):
        self.runtime().execute('''
          refusal='loading';ui();ui();assert(attempts==1)
          now=1;refusal=nil;ui();assert(attempts==2)
        ''')

    def test_disarm_is_reported_and_can_retry(self):
        self.runtime().execute('''
          ui();armed=false;reason='movie capture active';ui()
          assert(logs[2]:find('disarmed: movie capture active',1,true));assert(stops==1)
          armed=true;now=1;ui();assert(attempts==2)
        ''')

    def test_wrong_profile_never_registers(self):
        lua = LuaRuntime()
        lua.execute("X4LAN_CONFIG={test_profile='70000001'};function SetScript() error('unexpected') end")
        lua.execute(SOURCE.read_text())


if __name__ == '__main__':
    unittest.main()
