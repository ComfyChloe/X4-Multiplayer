"""Inert-by-default onUpdate/game-time diagnostic (frame_probe.lua); LuaJIT logic only."""
from pathlib import Path
import unittest

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'
PROBE = (UI / 'frame_probe.lua').read_text()
ENGINE = '''
logs, scripts, realTime, gameTime = {}, {}, 0, 0
function DebugError(v) logs[#logs+1]=v end
function SetScript(kind, f) assert(kind=='onUpdate'); scripts[#scripts+1]=f end
function GetCurRealTime() return realTime end
function GetCurTime() return gameTime end
local realRequire=require
require=function(name)
 if name=='ffi' then return {cdef=function() end, C={IsGamePaused=function() return paused==true end}} end
 return realRequire(name)
end
'''


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class FrameProbeTests(unittest.TestCase):
    def lua(self, config=None):
        l = LuaRuntime(unpack_returned_tuples=True)
        l.execute(ENGINE)
        if config is not None:
            l.execute('X4LAN_CONFIG=' + config)
        l.execute(PROBE)
        return l

    def test_inert_without_explicit_opt_in(self):
        for config in (None, '{}', '{frame_probe=false}', '{frame_probe="true"}'):
            l = self.lua(config)
            self.assertEqual(l.eval('#scripts'), 0, config)
            self.assertEqual(l.eval('#logs'), 0, config)
            self.assertIsNone(l.eval('X4LAN_FRAME_PROBE.active'))
        self.assertIn('frame_probe = false', (UI / 'config.lua').read_text())
        ui = (ROOT / 'extension/x4_lan_lab/ui.xml').read_text()
        self.assertEqual(ui.count('name="ui/frame_probe.lua"'), 1)
        # Wraps the transport opener, so it must load after transport.lua and before any adapter.
        self.assertLess(ui.index('ui/transport.lua'), ui.index('ui/frame_probe.lua'))
        self.assertLess(ui.index('ui/frame_probe.lua'), ui.index('ui/bubble_game.lua'))
        for name in ('SetObjectSectorPos', 'AddUITriggeredEvent', 'SpawnObject', 'RemoveComponent', 'Pause('):
            self.assertNotIn(name, PROBE)

    def test_window_summary_reports_rate_and_game_ratio(self):
        l = self.lua()
        l.execute('''
          p=X4LAN_FRAME_PROBE.new({window=5})
          out={}
          for i=0,300 do
           for _,line in ipairs(p.sample(100+i/60, 50+i/60*2, false)) do out[#out+1]=line end
          end
        ''')
        self.assertEqual(l.eval('#out'), 1)
        line = l.eval('out[1]')
        self.assertIn('FRAME_PROBE window real=5.000 game=10.000 calls=300 hz=60.00 ratio=2.000', line)
        self.assertIn('slow=0 paused=0 zeroDt=0', line)

    def test_throttled_and_stalled_frames_are_reported(self):
        l = self.lua()
        l.execute('''
          p=X4LAN_FRAME_PROBE.new({window=5})
          out={}
          local function s(r,g,pz) for _,line in ipairs(p.sample(r,g,pz)) do out[#out+1]=line end end
          s(0,0,false)
          -- Unfocused CPU throttle: about 12 frames per second.
          for i=1,12 do s(i*.085,i*.085,false) end
          -- Minimized: no frames for 30 s; game time advances by the 1 s clamp only.
          s(31.02,2.02,false)
          -- Paused frames: real time runs, game time does not.
          s(31.04,2.02,true)
        ''')
        self.assertIn('FRAME_PROBE stall realGap=30.000 gameGap=1.000 paused=false', l.eval('out[1]'))
        summary = l.eval('out[2]')
        self.assertIn('calls=13', summary)
        self.assertIn('maxGapMs=30000.0', summary)
        self.assertIn('slow=1', summary)  # the stall itself is also a slow frame
        self.assertEqual(l.eval('#out'), 2)
        l.execute('out={};for i=1,30 do for _,x in ipairs(p.sample(31.04+i*.2,2.02,true)) do out[#out+1]=x end end')
        self.assertEqual(l.eval('#out'), 1)
        self.assertIn('calls=26 hz=5.18 ratio=0.000', l.eval('out[1]'))
        self.assertIn('slow=25 paused=26 zeroDt=26', l.eval('out[1]'))

    def test_clock_reset_and_bad_numbers_do_not_crash(self):
        l = self.lua()
        l.execute('''
          p=X4LAN_FRAME_PROBE.new({})
          assert(#p.sample(10,10,false)==0)
          assert(#p.sample(0/0,1,false)==0)
          assert(#p.sample(nil,1,false)==0)
          assert(#p.sample(1,1,false)==0) -- real time went backwards: new window
          assert(#p.sample(1.016,1.016,false)==0)
        ''')

    def test_opt_in_registers_one_onupdate_and_counts_datagrams_unchanged(self):
        l = self.lua()
        l.execute('''
          sent={}
          X4LAN_TRANSPORT={open=function(host,port)
            return {send=function(_,v) sent[#sent+1]=v;return true end,
                    receive=function() return table.remove(queue or {},1) end,
                    close=function() closed=true end}
          end}
          X4LAN_CONFIG={frame_probe=true,frame_probe_window=1}
        ''')
        l.execute(PROBE)
        self.assertEqual(l.eval('#scripts'), 1)
        l.execute('''
          realTime=0;gameTime=0;scripts[1]() -- opens the first window; counters are per window
          t=X4LAN_TRANSPORT.open('127.0.0.1',1)
          queue={'A|1','B|1'}
          assert(t:send('HELLO|1|x')==true)
          assert(t:receive()=='A|1' and t:receive()=='B|1' and t:receive()==nil)
          t:close()
          assert(sent[1]=='HELLO|1|x' and closed)
          for i=1,61 do realTime=i/60;gameTime=i/60;scripts[1]() end
        ''')
        self.assertIn('FRAME_PROBE armed', l.eval('logs[1]'))
        window = [line for line in l.eval('logs').values() if 'FRAME_PROBE window' in line]
        self.assertEqual(len(window), 1)
        self.assertIn('sent=1 received=2', window[0])


if __name__ == '__main__':
    unittest.main()
