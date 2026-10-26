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
              local n=select('#',...);assert(n<=3);modes[#modes+1]=(...) or 'probe';attempts=attempts+1
              args={n=n,...}
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

    def test_simulation_cost_fields_are_logged(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.sim_hooks=1;s.sim_win_frames=60
            s.sim_win_mean_ms=4.25;s.sim_win_p95_ms=6.5;s.sim_win_max_ms=9.125;return s end
          ui();ui();assert(logs[2]:find('simHooks=1 simFrames=60 simMeanMs=4.250 simP95Ms=6.50 simMaxMs=9.125',1,true))
        ''')

    def test_walk_profile_line_is_logged(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.walk_installed=1;s.walk_frames=60
            s.walk_mean_ms=9.5;s.walk_max_ms=14;s.walk_share=0.67;s.sector_calls=3;s.sector_sum_ms=8
            s.sector_max_ms=4;s.ideal_speedup=2;s.zone_calls=40;s.zone_ms=6;s.ship_calls=300;s.ship_ms=5
            s.station_calls=20;s.station_ms=1.5;s.cluster_ms=0.2;return s end
          ui();ui()
          local found
          for _,l in ipairs(logs) do if l:find('WALKPROF frames=60 walkMs=9.500',1,true) and l:find('speedup=2.00',1,true) then found=true end end
          assert(found)
        ''')

    def test_walkpar_census_line_is_logged(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.walkpar_ready=1;s.walkpar_mode=1
            s.walkpar_frames=60;s.walkpar_sector_ms=8.2;s.walkpar_sector_max_ms=2.04;s.walkpar_deliver_ms=1.1
            s.walkpar_ideal_speedup=4.02;s.walkpar_posts=120;s.walkpar_posts_imm=90;s.walkpar_posts_rootlevel=5.5
            s.walkpar_deliver_calls=110;s.walkpar_deliver_listeners=250;s.walkpar_xsector_listener=3.5;return s end
          ui();ui()
          local found
          for _,l in ipairs(logs) do if l:find('WALKCENSUS mode=census frames=60',1,true) and l:find('speedup=4.02',1,true)
            and l:find('rootlevel=5.50',1,true) then found=true end end
          assert(found)
        ''')

    def test_walkpar_census_speedup_na_without_walkprof(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.walkpar_mode=1;s.walkpar_frames=30
            s.walkpar_sector_ms=0;s.walkpar_ideal_speedup=0;s.walkpar_deliver_ms=2.5;return s end
          ui();ui()
          local found
          for _,l in ipairs(logs) do if l:find('WALKCENSUS',1,true) and l:find('speedup=n/a(walkprof)',1,true) then found=true end end
          assert(found)
        ''')

    def test_walkpar_replay_line_and_refusal(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.walkpar_ready=2;s.walkpar_mode=2
            s.walkpar_frames=1;s.walkpar_buffered=42;s.walkpar_replayed=40;s.walkpar_replay_dropped=2
            s.walkpar_degraded_frames=0;return s end
          ui();ui();ui()
          local refusals,replay=0,false
          for _,l in ipairs(logs) do
            if l:find('FEATURE walkpar DISABLED',1,true) then refusals=refusals+1 end
            if l:find('WALKREPLAY buffered=42.0 replayed=40.0 dropped=2 degraded=0',1,true) then replay=true end
          end
          assert(refusals==1 and replay)
        ''')

    def test_walk_replay_abort_stays_visible_after_mode_is_off(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.walkpar_ready=3;s.walkpar_mode=0
            s.walkpar_frames=0;return s end
          ui();ui();now=2;ui()
          local n=0
          for _,l in ipairs(logs) do
            if l:find('FEATURE walkpar DISABLED reason=degraded deferred-event replay',1,true) then n=n+1 end
          end
          assert(n==1)
        ''')

    def test_walk_ai_replay_mode_is_reported(self):
        self.runtime().execute('''
          X4LAN_FIXEDTICK_MODE='aireplay'
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.walkpar_mode=3;s.walkpar_frames=1;return s end
          ui();ui();assert(modes[1]=='aireplay')
          local census,replay=false,false
          for _,l in ipairs(logs) do
            if l:find('WALKCENSUS mode=ai-replay',1,true) then census=true end
            if l:find('WALKREPLAY',1,true) then replay=true end
          end
          assert(census and replay)
        ''')

    def test_walk_profile_refusal_is_loud(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.walk_refused=1;return s end
          ui();ui();ui()
          local n=0
          for _,l in ipairs(logs) do if l:find('FEATURE walkprof DISABLED',1,true) then n=n+1 end end
          assert(n==1)
        ''')

    def profile_runtime(self):
        lua = self.runtime()
        lua.execute('''
          X4LAN_FIXEDTICK_MODE='profile';simprof=1;reports=0;foldQueue={}
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.simprof=simprof;s.simprof_reason=simprofReason
            s.simprof_interval_us=1000;s.simprof_depth=32;return s end
          handle.profile=function(self) reports=reports+1
            return {seconds=10.02,samples=9990,rate_hz=997,depth_mean=17.25,suspend_us_mean=21.5,
              suspend_us_max=180.25,walk_us_mean=2.5,overhead_pct=2.15,truncated=12,scanned=40,lost=13,
              loading=0,failures=0,skipped=1,dropped_functions=0,dropped_stacks=0,fold_pending=3,
              phases='slot3=61.0%(w4.0) idle=20.0%(w20.0)',self='9fdef0=5.0% [ext]=4.0%',
              incl='ef4400=80.0% 122b360=61.0%'} end
          handle.folds=function(self,n) local out={}
            while #out<n and #foldQueue>0 do out[#out+1]=table.remove(foldQueue,1) end
            return out end
        ''')
        return lua

    def test_profile_mode_passes_options_and_logs_enabled(self):
        self.profile_runtime().execute('''
          X4LAN_SIMPROF_INTERVAL_MS=0.5;X4LAN_SIMPROF_DEPTH=48
          ui();assert(modes[1]=='profile' and args.n==3 and args[2]==0.5 and args[3]==48)
          assert(logs[1]:find('mode=profile',1,true))
          assert(logs[2]=='[X4LAN] FEATURE simprof ENABLED intervalUs=1000 depth=32 reportS=10',logs[2])
        ''')

    def test_profile_defaults_pass_nil_options(self):
        self.profile_runtime().execute('''
          ui();assert(args.n==3 and args[2]==nil and args[3]==nil)
        ''')

    def test_profile_reports_every_interval_and_drains_folds(self):
        self.profile_runtime().execute('''
          X4LAN_SIMPROF_FOLD_LINES=2
          ui();local before=#logs
          now=5;ui();assert(reports==0)
          now=10;foldQueue={'slot3;ef4400;122b360 5','idle;ef4400;ntdll!NtWaitForSingleObject 9','lost;[truncated];9fdef0 1'}
          ui();assert(reports==1)
          local text=table.concat(logs,'|',before+1)
          assert(text:find('SIMPROF window seconds=10.02 samples=9990 rateHz=997 depthMean=17.2 suspendUsMean=21.5 suspendUsMax=180.2 walkUsMean=2.5 overheadPct=2.15',1,true),text)
          assert(text:find('truncated=12 scanned=40 lost=13 loading=0 failures=0 skipped=1 droppedFunctions=0 droppedStacks=0 foldPending=3',1,true))
          assert(text:find('[X4LAN] SIMPROF phases slot3=61.0%(w4.0) idle=20.0%(w20.0)',1,true))
          assert(text:find('[X4LAN] SIMPROF self 9fdef0=5.0% [ext]=4.0%',1,true))
          assert(text:find('[X4LAN] SIMPROF incl ef4400=80.0% 122b360=61.0%',1,true))
          assert(text:find('[X4LAN] SIMPROF fold slot3;ef4400;122b360 5',1,true))
          assert(not text:find('[truncated]',1,true))
          now=10.1;ui();assert(logs[#logs]=='[X4LAN] SIMPROF fold lost;[truncated];9fdef0 1')
          now=19;ui();assert(reports==1);now=20;ui();assert(reports==2)
        ''')

    def test_profile_disabled_is_logged_once(self):
        self.profile_runtime().execute('''
          ui();simprof=2;simprofReason='SuspendThread failed err=5'
          now=10;ui();now=20;ui()
          local n=0
          for i=1,#logs do if logs[i]=='[X4LAN] FEATURE simprof DISABLED reason=SuspendThread failed err=5' then n=n+1 end end
          assert(n==1)
        ''')

    def test_profile_start_failure_is_logged(self):
        self.profile_runtime().execute('''
          simprof=2;simprofReason='OpenThread failed'
          ui();assert(logs[2]=='[X4LAN] FEATURE simprof DISABLED reason=OpenThread failed')
          ui();assert(logs[3]:find('FRAMEPROBE frames=2',1,true))
        ''')

    def test_probe_mode_never_profiles(self):
        self.runtime().execute('''
          handle.profile=function() error('unexpected') end;handle.folds=function() error('unexpected') end
          ui();now=30;ui();assert(args.n==0)
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
