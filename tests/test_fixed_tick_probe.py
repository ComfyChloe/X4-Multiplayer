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
              assert(select('#',...)<=2);modes[#modes+1]=(...) or 'probe';attempts=attempts+1
              options=select(2,...)
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

    def test_catchup_mode_logs_step_counters(self):
        self.runtime().execute('''
          X4LAN_FIXEDTICK_MODE='fixed60catchup';fixed=1
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.catchup=1;s.steps_total=120;s.zero_step_frames=200
            s.multi_step_frames=3;s.dropped_ticks=0;s.last_steps=0;s.accumulator=.004;return s end
          ui();assert(modes[1]=='fixed60catchup')
          ui();assert(logs[2]:find('catchup=1 steps=120 zeroStep=200 multiStep=3 dropped=0 lastSteps=0 acc=0.004000',1,true))
        ''')

    def test_stage3_logs_counters_and_feature_line(self):
        self.runtime().execute('''
          X4LAN_FIXEDTICK_MODE='fixed60stage3';fixed=1
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.catchup=1;s.k_cap=2;s.capped_ticks=3;s.slow_frames=4
            s.stage3=1;s.represent=1;s.represent_active=1;s.re_presents=12;s.would_represent=0;s.waits_ok=50
            s.waits_timeout=20;s.waits_other=0;s.budget_skips=6;s.max_present_gap=.0185;s.index_changed=0
            s.queue_count_changed=1;s.in_flight_changed=0;s.gate_summary='suspended:2';s.stalls_injected=1;return s end
          ui();assert(modes[1]=='fixed60stage3');assert(options==nil)
          assert(logs[2]:find('FEATURE fixedtick_stage3 ON represent=1 kCap=2',1,true))
          ui();local line=logs[3]
          assert(line:find('kCap=2 capped=3 slow=4 stalls=1',1,true))
          assert(line:find('stage3=1 represent=1 active=1 rePresents=12 wouldRepresent=0 waitsOk=50 waitsTimeout=20',1,true))
          assert(line:find('budgetSkips=6 maxPresentGap=0.018500 indexChanged=0 queueChanged=1 inFlightChanged=0 gates=suspended:2',1,true))
        ''')

    def test_stage3_options_and_represent_off_are_loud(self):
        self.runtime().execute('''
          X4LAN_FIXEDTICK_MODE='fixed60stage3';X4LAN_FIXEDTICK_OPTIONS='represent=0,stall_ms=100,stall_slot=7'
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.stage3=1;s.represent=0;return s end
          ui();assert(modes[1]=='fixed60stage3');assert(options=='represent=0,stall_ms=100,stall_slot=7')
          assert(logs[2]:find('FEATURE fixedtick_represent DISABLED reason=option represent=0',1,true))
          ui();assert(logs[3]:find('gates=none',1,true))
        ''')

    def test_options_without_mode_use_probe(self):
        self.runtime().execute('''
          X4LAN_FIXEDTICK_OPTIONS='stall_ms=50';ui();assert(modes[1]=='probe');assert(options=='stall_ms=50')
          ui();assert(not logs[2]:find('stage3=',1,true))
        ''')

    def test_stage3_refusal_and_disarm_log_feature_disabled(self):
        lua = self.runtime()
        with self.assertRaisesRegex(Exception, 'no slot page'):
            lua.execute('''
              X4LAN_FIXEDTICK_MODE='fixed60stage3';refusal='no slot page within 2 GB of the image';ui()
            ''')
        lua.execute('''assert(logs[1]=='[X4LAN] FEATURE fixedtick_stage3 DISABLED reason=no slot page within 2 GB of the image')''')
        self.runtime().execute('''
          X4LAN_FIXEDTICK_MODE='fixed60stage3';ui();armed=false;reason='stage3 call displacement changed';ui()
          assert(logs[#logs-1]:find('disarmed: stage3 call displacement changed',1,true))
          assert(logs[#logs]=='[X4LAN] FEATURE fixedtick_stage3 DISABLED reason=stage3 call displacement changed')
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
