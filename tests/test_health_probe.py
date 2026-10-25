"""LuaJIT doubles for the dev-only health function probe; it observes, the native run is A:primary's."""
import unittest
from pathlib import Path
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class HealthProbeTests(unittest.TestCase):
    def make(self, probe=True):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        g = l.globals(); c = g.X4LAN_CONFIG
        c.slot, c.coop, c.pilot_health, c.health_probe, c.test_profile = 'p1', True, True, probe, '45841'
        l.execute("sectors={};ConvertIDTo64Bit=function(v) return tonumber(v) end;GetComponentData=function() return 'cluster_14_sector001_macro' end")
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'health_game.lua'):
            l.execute((UI / file).read_text())
        l.execute((ROOT / 'dev/health_function_probe.lua').read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         client:receiveExtension({'AUTH','1',client.lease,'1','p1','1','p1','p2','1','p2'},now)
         now=10.1;frames.onUpdate();events['X4LAN_Health.Ready']()
         events['X4LAN_Health.Own'](nil,'50|2026-10-24T00:30:00|0|0|0|0');frames.onUpdate()
         function hp(seq,hull) client:receiveExtension({'HEALTH','1',client.lease,string.rep('c',32),'2','3',tostring(seq),hull,'50'},now) end
         function probe() local out={};for _,m in ipairs(logs) do if m:find('HEALTH_PROBE',1,true) then out[#out+1]=m end end;return out end
        ''')
        return l

    def test_logs_frames_and_op_sequence_and_drops_only_in_phase_3(self):
        out = self.make().execute('''
         client:receiveExtension({'HGEN','1',client.lease,'1','1','3'},now)
         client:receiveExtension({'HGEN','1',client.lease,'1','1','3'},now)   -- unchanged: logged once
         hp(2010,'90');hp(2010,'90');hp(3010,'90');hp(3011,'90')
         local drops=0;for _,q in ipairs(requests) do if q[1]=='X4LAN_HealthProbe' and q[2]=='Drop' then drops=drops+1 end end
         return table.concat(probe(),'\\n'),drops''')
        text, drops = out
        self.assertEqual(text.count('frame HGEN'), 1)
        self.assertTrue('op=2 Apply entity=2 gen=3 hull=90' in text, text[:400])
        self.assertTrue('seq=2010 hull=90 shield=50 accepted=false phase=2' in text)   # the duplicate
        self.assertTrue('op=1 Setup' in text and 'op=3 Apply' in text and 'op=4 Apply' in text and 'op=5' not in text)
        self.assertEqual(drops, 1)                                  # 3010 only (every tenth, phase 3)

    def test_inert_without_its_flag_and_md_schema(self):
        out = self.make(probe=False).execute('return #probe()')
        self.assertEqual(out, 0)
        md = (ROOT / 'dev/health_function_probe.xml').read_text()
        self.assertTrue("control=\"'Drop'\"" in md and 'checkinterval="0.05s"' in md and '$arena.$healthProbe' in md)
        prod = (ROOT / 'extension/x4_lan_lab/md/x4lan_health.xml').read_text()
        for op in ('Base', 'Apply', 'Release'):                    # post-write readbacks, probe-gated
            self.assertTrue('HEALTH_PROBE written %s' % op in prod, op)
        self.assertEqual(prod.count('<do_if value="@$arena.$healthProbe">'), 3)


if __name__ == '__main__':
    unittest.main()
