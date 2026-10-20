"""Unshipped damage probe: LuaJIT doubles prove it observes without changing behaviour."""
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'


class DamageProbeStaticTests(unittest.TestCase):
    def test_probe_is_never_shipped(self):
        self.assertNotIn('damage_probe', (ROOT / 'extension/x4_lan_lab/ui.xml').read_text())
        self.assertFalse(list((ROOT / 'extension').rglob('*damage_probe*')))
        for builder in ('distribution/build_packages.py', 'distribution/public/build.py'):
            self.assertNotIn('damage_probe', (ROOT / builder).read_text())

    def test_md_companion_only_reads_and_reports(self):
        root = ET.parse(ROOT / 'dev/damage_probe.xml').getroot()
        actions = {e.tag for e in root.iter() if e.tag not in ('mdscript', 'cues', 'cue', 'conditions', 'check_value', 'actions')}
        self.assertEqual(actions, {'raise_lua_event', 'debug_text'})


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class DamageProbeAdapterTests(unittest.TestCase):
    def make(self, probe=True):
        l = LuaRuntime(unpack_returned_tuples=True)
        l.execute(MOCK + EXTRA)
        l.globals().X4LAN_CONFIG.slot = 'p1'
        l.globals().X4LAN_CONFIG.coop = True
        l.execute('''
         sectors={[11]=22,[42]=22};ConvertIDTo64Bit=function(v) return tonumber(v) end
         api.GetContextByClass=function(object) return sectors[object] or 22 end
         api.SetObjectSectorPos=function(object,sector,p) positions[object]=p end
         hull={}
         GetComponentData=function(object,a,b)
          if a=='macro' then return 'cluster_14_sector001_macro' end
          local h=hull[tostring(object)];if not h then return nil end;return h[1],h[2]
         end
        ''')
        files = [n for n in ('bubble_game.lua', 'motion.lua', 'coop_game.lua') if (UI / n).is_file()]
        for name in files:
            l.execute((UI / name).read_text())
        if probe:
            l.execute((ROOT / 'dev/damage_probe.lua').read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         C=X4LAN_BUBBLE_API.coop
         client:receiveExtension({'AUTH','1',client.lease,'1','p1','1','p1','p2','1','p2'},now)
         function nav(seq,fire) client:receiveExtension({'NAV','1',client.lease,string.rep('b',32),'2','1',tostring(seq),'cluster_14_sector001_macro','1000','10000','0','0','0','0','0',tostring(fire)},now) end
         function probes(reason) local n=0;for _,v in ipairs(logs) do if v:find('DAMAGE_PROBE',1,true) and v:find('reason='..reason..' ',1,true) then n=n+1 end end;return n end
         function count(name) local n=0;for _,r in ipairs(requests) do if r[2]==name then n=n+1 end end;return n end
        ''')
        return l

    def test_logs_ticks_shots_edges_and_targets_without_changing_fire(self):
        l = self.make()
        l.execute('''
         hull['11']={100,80};hull['42']={90,70};hull['t1']={55,0}
         events['X4LAN_DamageProbe.Targets'](nil,{'t1','t2'})
         now=10.3;frames.onUpdate();assert(probes('tick')>=1)
         events['X4LAN_Coop.Shot'](nil,1);assert(C.shots[1]==1,'production Shot handler still registered')
         now=10.35;frames.onUpdate();assert(probes('shot')==1,'shot detected from counters')
         C.sectors.cluster_14_sector001_macro=22
         nav(1,0);now=10.4;nav(2,1);frames.onUpdate()
         assert(count('Fire')==1 and probes('edge-Fire')==1,'fire edge forwarded once and logged')
         local line;for _,v in ipairs(logs) do if v:find('reason=shot ',1,true) then line=v end end
         assert(line:find('ship1=100.00/80.00',1,true) and line:find('ship2=90.00/70.00',1,true))
         assert(line:find('target1=55.00/0.00',1,true) and line:find('target2=dead',1,true))
         assert(line:find('paused=false',1,true) and line:find('mode=legacy',1,true))
        ''')

    def test_production_requests_identical_with_and_without_probe(self):
        script = '''
         C.sectors.cluster_14_sector001_macro=22
         nav(1,0);now=10.4;nav(2,1);frames.onUpdate();now=11;frames.onUpdate()
         out={};for _,r in ipairs(requests) do out[#out+1]=r[2] end;result=table.concat(out,',')
        '''
        results = []
        for probe in (False, True):
            l = self.make(probe)
            l.execute(script)
            results.append(l.globals().result)
        self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()
