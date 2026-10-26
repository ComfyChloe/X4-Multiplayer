"""Observed trajectory and bounded loss/reorder/pause lifecycle in actual LuaJIT."""
from pathlib import Path
import unittest
from test_game_adapter import LuaRuntime


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ProjectileMotionTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute((Path(__file__).resolve().parents[1] /
                          'src/extension/x4_lan_lab/ui/projectile_motion.lua').read_text())
        self.lua.execute('''
         P=X4LAN_PROJECTILE_MOTION;B=P.new('lease/entity/epoch/gen',0);K=B.key
         function birth(id,t)
          return {id=id,time=t or 10,ttl=2,kind=1,pose={100,200,300,.1,-.2,0},velocity={-170,-400,2258}}
         end
        ''')

    def test_actual_oblique_path_and_delayed_end_use_source_clock(self):
        self.lua.execute('''
         local r=birth(1);assert(P.birth(B,K,r,10.03));assert(not P.sample(B,9.99)[1])
         local p=P.sample(B,10.5)[1].pose
         assert(p[1]==15 and p[2]==0 and p[3]==1429 and p[4]==.1 and p[5]==-.2)
         assert(P.finish(B,K,1,10.8));assert(P.sample(B,10.79)[1]);assert(not P.sample(B,10.8)[1]);assert(B.count==0)
        ''')

    def test_birth_is_immutable_and_duplicates_do_not_extend_or_respawn(self):
        self.lua.execute('''
         local r=birth(1);assert(P.birth(B,K,r,10));r.pose[1]=900;r.velocity[1]=0;r.ttl=3
         assert(P.sample(B,10)[1].pose[1]==100)
         assert(not P.birth(B,K,r,10.2));P.sample(B,12)
         assert(not P.birth(B,K,birth(1,12),12) and B.count==0)
        ''')

    def test_end_before_birth_and_changed_authority_cannot_create(self):
        self.lua.execute('''
         assert(P.finish(B,K,4,10.5));assert(not P.birth(B,K,birth(4),10.1))
         assert(not P.birth(B,'old key',birth(5),10));assert(not B.seen[5])
         assert(P.birth(B,K,birth(5),10));assert(not P.finish(B,'old key',5,10.2))
         assert(not P.finish(B,K,5,9));assert(P.finish(B,K,5,10.2));P.finish(B,K,5,11)
         assert(not P.sample(B,10.2)[5])
        ''')

    def test_pause_clears_and_blocks_history_even_after_resuming(self):
        self.lua.execute('''
         assert(P.birth(B,K,birth(1),10));P.clear(B,10.4)
         assert(not next(P.sample(B,10.4)))
         assert(not P.birth(B,K,birth(2,10.3),10.5))
         assert(not P.birth(B,K,birth(1,10.6),10.6))
         assert(P.birth(B,K,birth(3,10.6),10.6))
        ''')

    def test_expiry_missing_end_and_capacity_are_bounded_without_replay(self):
        self.lua.execute('''
         for id=1,P.capacity do assert(P.birth(B,K,birth(id),10)) end
         assert(not P.birth(B,K,birth(P.capacity+1),10))
         assert(B.count==P.capacity);assert(not next(P.sample(B,12)))
         assert(not P.birth(B,K,birth(P.capacity+1,12),12))
         assert(not P.birth(B,K,birth(999,8),12));assert(not P.birth(B,K,birth(999,12),12))
        ''')

    def test_reorder_window_is_bounded_and_eviction_never_reenables_old_ids(self):
        self.lua.execute('''
         for id=1,20000 do assert(P.finish(B,K,id,10)) end
         local n=0;for _ in pairs(B.seen) do n=n+1 end;assert(n==P.window)
         assert(not P.birth(B,K,birth(1),10));assert(not P.birth(B,K,birth(20000),10))
         assert(P.birth(B,K,birth(20002),10));assert(P.birth(B,K,birth(20001),10))
        ''')

    def test_expired_unsampled_capacity_is_reclaimed_before_admission(self):
        self.lua.execute("""
         for id=1,P.capacity do assert(P.birth(B,K,birth(id),10)) end
         assert(P.birth(B,K,birth(257,13),13));assert(B.count==1)
         P.finish(B,K,257,13.1);assert(P.birth(B,K,birth(258,13.2),13.2));assert(B.count==1)
        """)

    def test_delayed_render_tail_survives_admission_and_future_retry_is_not_burned(self):
        self.lua.execute("""
         local r=birth(1);r.ttl=.2;assert(P.birth(B,K,r,10,9.85))
         assert(P.birth(B,K,birth(2,10.25),10.25,10.1));assert(P.sample(B,10.1)[1])
         assert(not P.birth(B,K,birth(3,10.7),10.3,10.15));assert(not B.seen[3])
         assert(P.birth(B,K,birth(3,10.7),10.7,10.55))
        """)

    def test_huge_forward_ids_and_invalid_kind_never_poison_the_reorder_window(self):
        self.lua.execute("""
         assert(P.birth(B,K,birth(1),10))
         assert(not P.birth(B,K,birth(2147483647),10));assert(B.latest==1)
         assert(not P.finish(B,K,2147483000,10));assert(B.latest==1)
         for _,k in ipairs({99,'x',false}) do local r=birth(2);r.kind=k;assert(not P.birth(B,K,r,10)) end
         local r=birth(2);r.kind=nil;assert(not P.birth(B,K,r,10))
         assert(P.birth(B,K,birth(2),10))
         local fresh=P.new('new key',0);assert(P.finish(fresh,'new key',10000,10)) -- arbitrary first baseline
         assert(P.birth(fresh,'new key',birth(10001),10))
        """)

    def test_velocity_preserves_observed_motion_and_refuses_bad_samples(self):
        self.lua.execute('''
         local a={time=10,pose={100,200,300,0,0,0}}
         local b={time=10.1,pose={83,160,525.8,0,0,0}}
         local v=P.velocity(a,b);assert(math.abs(v[1]+170)<1e-8 and math.abs(v[3]-2258)<1e-8)
         b.time=10.001;assert(not P.velocity(a,b));b.time=11;assert(not P.velocity(a,b))
         b.time=10.1;b.pose[1]=1e6;assert(not P.velocity(a,b))
         b.pose[1]=0/0;assert(not P.velocity(a,b))
        ''')

    def test_invalid_data_and_out_of_world_prediction_never_render(self):
        self.lua.execute('''
         for _,change in ipairs({function(r) r.pose[4]=7 end,function(r) r.velocity[1]=math.huge end,
          function(r) r.ttl=0 end,function(r) r.time=0/0 end,function(r) r.id=1.5 end}) do
          local r=birth(1);change(r);assert(not P.birth(B,K,r,10))
         end
         local r=birth(2);r.pose[3]=50000000;assert(P.birth(B,K,r,10));assert(not P.sample(B,10.1)[2])
         assert(not P.birth(B,K,birth(3,11),10))
        ''')
