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

    def test_no_physics_fields_no_physics_lines(self):
        self.runtime().execute('''
          ui();ui();ui()
          for _,l in ipairs(logs) do assert(not l:find('PHYSPROF',1,true) and not l:find('physprof',1,true)) end
        ''')


if __name__ == '__main__':
    unittest.main()
