"""PHYSPROF logging of the fixed-tick probe (physics_probe.h fields) under real LuaJIT."""
import unittest

import test_fixed_tick_probe as fixed_tick_probe  # module import: its TestCase is not collected twice


class PhysicsProbeLuaTests(unittest.TestCase):
    runtime = fixed_tick_probe.FixedTickProbeTests.runtime

    def test_physics_line_is_logged(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.phys_installed=1;s.phys_frames=60
            s.phys_updates=2;s.phys_jolt_ms=0.26;s.phys_jolt0_ms=0.2;s.phys_jolt1_ms=0.06;s.phys_jolt_max_ms=0.9
            s.phys_jobs=226;s.phys_added=1.5;s.phys_persisted=20;s.phys_removed=1;s.phys_bodies0=35;s.phys_bodies1=2
            s.phys_bodies_max=40;s.path_batches_a=30;s.path_batches_b=2;s.path_budget_ms=4;s.path_dt=1/60
            s.jtm_workers=30;s.jtm_samples=2000;s.jtm_busy=3.5;s.jtm_busy_max=30;s.jtm_cat1=3.4;s.jtm_cat2=0.1
            s.jtm_other=0;s.jtm_queue=0.4;s.jtm_queue_max=12;s.path_inflight=0.12;s.path_passes=60;s.path_pass_ms=2.25
            s.path_pass_max_ms=6.5;s.jtm_jolt_samples=40;s.jtm_busy_in_jolt=0.25;return s end
          ui();ui()
          local found
          for _,l in ipairs(logs) do
            if l:find('PHYSPROF frames=60 updates=2.00 joltMs=0.260',1,true) and l:find('pathBatchesA=30 pathBatchesB=2',1,true)
              and l:find('pathPassMs=2.250',1,true) and l:find('jtmWorkers=30',1,true) and l:find('jtmBusyInJolt=0.25',1,true) then
              found=true end
          end
          assert(found)
        ''')

    def test_physics_refusal_is_logged_once(self):
        self.runtime().execute('''
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.phys_installed=0;s.phys_reason='vtable entry not original';return s end
          ui();ui();ui()
          local n=0
          for _,l in ipairs(logs) do if l:find('FEATURE physprof DISABLED reason=vtable entry not original',1,true) then n=n+1 end end
          assert(n==1)
        ''')

    def test_joltwait_mode_is_requested_once(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='joltwait';calls={}
          local applied=0
          handle.physics_mode=function(self,m) calls[#calls+1]=m;return true end
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.phys_installed=1;s.phys_frames=60
            s.phys_mode=#calls>0 and 1 or 0;s.phys_mode_frames=#calls>0 and 60 or 0;return s end
          ui();ui();now=30;ui();now=31;ui();now=32;ui()
          assert(#calls==1 and calls[1]=='joltwait')
          local on,line=0
          for _,l in ipairs(logs) do
            if l:find('FEATURE joltwait ENABLED',1,true) then on=on+1 end
            if l:find('mode=1 modeFrames=60',1,true) then line=true end
          end
          assert(on==1 and line)
        ''')

    def test_mode_not_applied_logs_no_enabled(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='joltwait'
          handle.physics_mode=function(self,m) return true end
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.phys_installed=1;s.phys_frames=60;s.phys_mode=0;return s end
          ui();ui();now=30;ui()
          for _,l in ipairs(logs) do assert(not l:find('FEATURE joltwait ENABLED',1,true)) end
        ''')

    def test_refusal_reported_by_stats_stops_ab(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='ab';X4LAN_PHYSICS_AB_S=1;calls={}
          handle.physics_mode=function(self,m) calls[#calls+1]=m;return true end
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.phys_installed=1;s.phys_frames=60;s.phys_mode=0
            if #calls>0 then s.joltwait_reason='barrier entry not original' end;return s end
          ui();ui();now=5;ui();now=10;ui();now=15;ui()
          assert(#calls==1)
          local n=0
          for _,l in ipairs(logs) do if l:find('FEATURE joltwait DISABLED reason=barrier entry not original',1,true) then n=n+1 end end
          assert(n==1)
        ''')

    def test_ab_mode_alternates(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='ab';X4LAN_PHYSICS_AB_S=10;calls={}
          handle.physics_mode=function(self,m) calls[#calls+1]=m;return true end
          ui();ui();now=5;ui();now=10;ui();now=20;ui();now=25;ui()
          assert(#calls==3 and calls[1]=='joltwait' and calls[2]=='original' and calls[3]=='joltwait')
        ''')

    def test_joltwait_refusal_stops_requests(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='ab';X4LAN_PHYSICS_AB_S=1;calls={}
          handle.physics_mode=function(self,m) calls[#calls+1]=m;return nil,'barrier entry not original' end
          ui();ui();now=5;ui();now=10;ui()
          assert(#calls==1)
          local n=0
          for _,l in ipairs(logs) do if l:find('FEATURE joltwait DISABLED reason=barrier entry not original',1,true) then n=n+1 end end
          assert(n==1)
        ''')

    def test_joltwait_is_default_on(self):
        self.runtime().execute('''
          calls={}
          handle.physics_mode=function(self,m) calls[#calls+1]=m;return true end
          ui();ui();now=30;ui();ui()
          assert(#calls==1 and calls[1]=='joltwait')
        ''')

    def test_original_setting_requests_nothing(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='original';calls={}
          handle.physics_mode=function(self,m) calls[#calls+1]=m;return true end
          ui();ui();now=30;ui()
          assert(#calls==0)
          for _,l in ipairs(logs) do assert(not l:find('joltwait',1,true)) end
        ''')

    def test_unknown_setting_is_refused_loudly(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='fast';calls={}
          handle.physics_mode=function(self,m) calls[#calls+1]=m
            if m=='fast' then return nil,'unsupported physics mode' end;return true end
          ui();ui();now=30;ui()
          assert(#calls==1)
          local n=0
          for _,l in ipairs(logs) do if l:find('FEATURE joltwait DISABLED reason=unsupported physics mode',1,true) then n=n+1 end end
          assert(n==1)
        ''')

    def test_flight_census_requested_and_logged(self):
        self.runtime().execute('''
          X4LAN_FLIGHTPROF=true;calls={}
          handle.flight_census=function(self,m) calls[#calls+1]=m;return true end
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.phys_installed=1;s.phys_frames=60
            if #calls>0 then s.flight_installed=1;s.flight_frames=60;s.flight_calls=812.5;s.flight_calls_max=900
              s.flight_cheap=0;s.flight_ms=1.25;s.flight_owner_calls=0;s.flight_steering_calls=700;s.flight_steering_us=1.5
              s.flight_linear_calls=100.5;s.flight_linear_us=0.75 end
            return s end
          ui();ui();now=30;ui();now=31;ui()
          assert(#calls==1 and calls[1]=='on')
          local line
          for _,l in ipairs(logs) do
            if l:find('FLIGHTPROF frames=60 calls=812.5 callsMax=900 cheap=0.00 ms=1.250',1,true)
              and l:find('linear=100.5/0.75',1,true) and l:find('steering=700.0/1.50',1,true) then line=true end
          end
          assert(line)
        ''')

    def test_flight_census_refusal_is_loud_once(self):
        self.runtime().execute('''
          X4LAN_FLIGHTPROF=true;calls={}
          handle.flight_census=function(self,m) calls[#calls+1]=m;return nil,'flight vtable entry not original' end
          local base=handle.stats
          handle.stats=function(self) local s=base(self);s.phys_installed=1;s.phys_frames=60
            s.flight_reason='flight vtable entry not original';return s end
          ui();ui();now=30;ui();now=31;ui()
          assert(#calls==1)
          local n=0
          for _,l in ipairs(logs) do if l:find('FEATURE flightprof DISABLED reason=flight vtable entry not original',1,true) then n=n+1 end end
          assert(n==1)
        ''')

    def test_flight_census_not_requested_by_default(self):
        self.runtime().execute('''
          calls={}
          handle.flight_census=function(self,m) calls[#calls+1]=m;return true end
          ui();ui();now=30;ui()
          assert(#calls==0)
        ''')

    def test_old_dll_without_physics_mode_is_ignored(self):
        self.runtime().execute('''
          X4LAN_PHYSICS_MODE='joltwait'
          ui();ui();now=30;ui()
          for _,l in ipairs(logs) do assert(not l:find('joltwait',1,true)) end
        ''')

    def test_no_physics_fields_no_physics_lines(self):
        self.runtime().execute('''
          ui();ui();ui()
          for _,l in ipairs(logs) do assert(not l:find('PHYSPROF',1,true) and not l:find('physprof',1,true)) end
        ''')


if __name__ == '__main__':
    unittest.main()
