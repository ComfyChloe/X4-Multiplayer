"""Mirror capture driver (dev/mirror_probe.lua) under real LuaJIT with a fake probe owner."""
from pathlib import Path
import unittest

from lupa.luajit21 import LuaRuntime

SOURCE = Path(__file__).resolve().parents[1] / 'dev/mirror_probe.lua'


class MirrorProbeTests(unittest.TestCase):
    def runtime(self, settings="X4LAN_MIRROR_PATH='C:/cap/run.x4cap'"):
        lua = LuaRuntime()
        lua.execute('''
          now=0;logs={};calls={}
          X4LAN_CONFIG={test_profile='45841'}
          function GetCurRealTime() return now end
          function DebugError(s) logs[#logs+1]=s end
          function SetScript(event,fn) assert(event=='onUpdate');ui=fn end
          stats={armed=1,frames_written=0,last_frame=0,probe_frame=0,frames_dropped=0,partial_frames=0,
            rows=0,roots=0,factions=0,bytes=0,walk_errors=0,mean_ms=0,max_ms=0,layout='abc123'}
          sample={5,6,7}
          valid={[5]=true,[6]=true,[7]=false}
          owner={
            mirror=function(self,...) calls[#calls+1]={...};if refusal then return nil,refusal end;return true end,
            mirror_stats=function(self) return stats end,
            mirror_sample=function(self) return sample end}
          package.loaded.ffi={typeof=function() return true end,cdef=function() end,C={
            IsValidComponent=function(id) return valid[id] end,
            GetPositionalOffset=function(id,space) assert(space==0);return {x=id+0.5,y=-id,z=1/3} end,
            GetObjectPositionInSector=function(id) return {x=id*10,y=0,z=-0.1} end}}
        ''')
        lua.execute(settings)
        lua.execute(SOURCE.read_text())
        return lua

    def test_arms_after_the_delay_with_settings(self):
        lua = self.runtime("X4LAN_MIRROR_PATH='C:/cap/a.x4cap';X4LAN_MIRROR_INTERVAL=2;X4LAN_MIRROR_FRAMES=5;"
                           "X4LAN_MIRROR_FLAGS=1;X4LAN_MIRROR_ATTENTION=4;X4LAN_MIRROR_DELAY=3")
        lua.execute('''
          ui();assert(#calls==0)                        -- no owner yet
          X4LAN_FIXEDTICK_OWNER=owner
          ui();now=2;ui();assert(#calls==0)             -- delay not over
          now=3;ui();assert(#calls==1)
          local c=calls[1];assert(c[1]=='C:/cap/a.x4cap' and c[2]==2 and c[3]==5 and c[4]==1 and c[5]==4)
          assert(logs[1]=='[X4LAN] MIRROR armed path=C:/cap/a.x4cap interval=2 frames=5 flags=1 attention=4 layout=abc123',logs[1])
        ''')

    def test_checks_only_in_the_captured_frame_and_skips_invalid(self):
        lua = self.runtime("X4LAN_MIRROR_PATH='C:/cap/b.x4cap';X4LAN_MIRROR_DELAY=0;X4LAN_MIRROR_CHECKS=8")
        lua.execute('''
          X4LAN_FIXEDTICK_OWNER=owner;ui();ui()           -- arm
          assert(#logs==1)
          stats.frames_written=1;stats.last_frame=41;stats.probe_frame=42
          ui();assert(#logs==1)                           -- captured in an earlier frame: no check
          stats.last_frame=43;stats.probe_frame=43
          ui();assert(#logs==3,#logs)                     -- uid 7 is not a valid component
          assert(logs[2]=='[X4LAN] MIRRORCHECK frame=43 uid=5 off=5.5,-5,0.333333333 sec=50,0,-0.1',logs[2])
          assert(logs[3]:find('uid=6 off=6.5,-6,',1,true))
          ui();assert(#logs==3)                           -- once per captured frame
        ''')

    def test_done_line_and_sample_limit(self):
        lua = self.runtime("X4LAN_MIRROR_PATH='C:/cap/c.x4cap';X4LAN_MIRROR_DELAY=0;X4LAN_MIRROR_CHECKS=1")
        lua.execute('''
          X4LAN_FIXEDTICK_OWNER=owner;ui();ui()
          stats.frames_written=10;stats.last_frame=9;stats.probe_frame=9;stats.armed=0
          stats.rows=1234;stats.roots=77;stats.bytes=999;stats.walk_errors=2
          ui()
          assert(#logs==3,#logs)                          -- one check (limit 1), then done
          assert(logs[3]:find('MIRROR done frames=10 dropped=0 partial=0 rows=1234 roots=77 factions=0 bytes=999 walkErrors=2',1,true),logs[3])
          assert(logs[3]:find('checks=1 regionQueries=0 cacheFull=0 visited=0 walkMs=0.000 serializeMs=0.000 reason=complete',1,true),logs[3])
          ui();assert(#logs==3)
        ''')

    def test_refusal_is_loud_and_final(self):
        lua = self.runtime("X4LAN_MIRROR_PATH='C:/cap/d.x4cap';X4LAN_MIRROR_DELAY=0")
        lua.execute('''
          refusal='mirror code signature mismatch'
          X4LAN_FIXEDTICK_OWNER=owner;ui();ui()
          assert(logs[1]=='[X4LAN] FEATURE mirror DISABLED reason=mirror code signature mismatch',logs[1])
          ui();ui();assert(#calls==1)
        ''')

    def test_inert_without_path_or_outside_the_test_profile(self):
        lua = self.runtime("X4LAN_MIRROR_PATH=nil")
        lua.execute('assert(ui==nil)')
        lua = LuaRuntime()
        lua.execute("X4LAN_CONFIG={test_profile='12345'};X4LAN_MIRROR_PATH='C:/x';function SetScript() error('registered') end")
        lua.execute(SOURCE.read_text())


if __name__ == '__main__':
    unittest.main()
