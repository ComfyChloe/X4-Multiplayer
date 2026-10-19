"""Source-clock movement under jitter/loss; uses the actual LuaJIT renderer."""
from pathlib import Path
import unittest
from test_game_adapter import LuaRuntime


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class MotionTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute((Path(__file__).resolve().parents[1] /
                          'extension/x4_lan_lab/ui/motion.lua').read_text())

    def test_regular_motion_continues_between_sparse_packets_then_freezes(self):
        self.lua.execute('''
          local M=X4LAN_MOTION;local t={}
          for i=0,3 do
           assert(M.push(t,i*.2,{i*20,0,0,0,0,0},{100,0,0,0,0,0},100+i*.2))
          end
          local previous=0
          for i=0,59 do
           local now=100.6+i/60;local p=M.render(t,now)
           assert(p[1]>=previous and p[1]<=95.0001)
           previous=p[1]
          end
          assert(math.abs(M.render(t,101)[1]-85)<.0001)
          assert(math.abs(M.render(t,1000)[1]-95)<.0001)
        ''')

    def test_source_time_removes_variable_arrival_spacing(self):
        self.lua.execute('''
          local M=X4LAN_MOTION;local t={}
          local jitter={0,.04,.01,.08,0,.03}
          for i=1,6 do
           local time=(i-1)*.2
           assert(M.push(t,time,{time*120,0,0,0,0,0},{120,0,0,0,0,0},50+time+jitter[i]))
          end
          for i=0,10 do
           local now=50.6+i*.04
           assert(math.abs(M.render(t,now)[1]-(now-50-.15)*120)<.00001)
          end
          assert(not M.push(t,.8,{999,0,0,0,0,0},{0,0,0,0,0,0},52))
        ''')

    def test_rotation_uses_short_arc_and_prediction_wraps(self):
        self.lua.execute('''
          local M=X4LAN_MOTION;local t={};local d=math.pi/180
          M.push(t,1,{0,0,0,179*d,0,0},{0,0,0,20*d,0,0},1)
          M.push(t,1.1,{0,0,0,-179*d,0,0},{0,0,0,20*d,0,0},1.1)
          assert(math.abs(math.abs(M.render(t,1.2)[4])-math.pi)<.00001)
          assert(M.render(t,1.4)[4]<0)
        ''')

    def test_pauses_teleports_and_invalid_numbers_do_not_invent_velocity(self):
        self.lua.execute('''
          local M=X4LAN_MOTION;local t={};local z={0,0,0,0,0,0}
          M.push(t,1,z,z,1)
          M.push(t,1.1,{20000,0,0,0,0,0},z,1.1)
          assert(M.render(t,1.1)[1]==20000 and #t.samples==1)
          M.push(t,5,{20100,0,0,0,0,0},z,5)
          assert(M.render(t,5)[1]==20100 and #t.samples==1)
          assert(not M.push(t,6,{0/0,0,0,0,0,0},z,6))
          assert(not M.push(t,6,z,{math.huge,0,0,0,0,0},6))
          local v=M.velocity({time=1,pose=z},{1,0,0,0,0,0},2)
          assert(v[1]==0)
        ''')
