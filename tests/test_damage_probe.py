"""Unshipped damage probe: LuaJIT doubles prove it observes without changing behaviour.

These are engine doubles and a log validator, not native measurements.
"""
import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'
spec = importlib.util.spec_from_file_location('validate_damage_probe', ROOT / 'dev/validate_damage_probe.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class DamageProbeStaticTests(unittest.TestCase):
    def test_probe_is_never_shipped(self):
        self.assertNotIn('damage_probe', (ROOT / 'extension/x4_lan_lab/ui.xml').read_text())
        self.assertFalse(list((ROOT / 'extension').rglob('*damage_probe*')))
        for builder in ('distribution/build_packages.py', 'distribution/public/build.py'):
            self.assertNotIn('damage_probe', (ROOT / builder).read_text())

    def test_md_companion_only_reads_and_reports(self):
        root = ET.parse(ROOT / 'dev/damage_probe.xml').getroot()
        allowed = {'mdscript', 'cues', 'cue', 'conditions', 'check_value', 'event_ui_triggered', 'actions',
                   'delay', 'set_value', 'raise_lua_event', 'debug_text'}
        self.assertEqual({e.tag for e in root.iter()} - allowed, set())
        # set_value only writes the probe's own cue variables.
        self.assertEqual({e.get('name') for e in root.iter('set_value')},
                         {'$requested', '$ships', '$mark', 'md.X4LAN_DamageProbe.Targets.$applyMark'})

    def test_no_session_credentials_are_logged(self):
        source = (ROOT / 'dev/damage_probe.lua').read_text()
        for secret in ('lease', 'token', 'worldOwner'):
            self.assertNotIn(secret, source)


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class DamageProbeAdapterTests(unittest.TestCase):
    def make(self, probe=True, slot='p1', fire2=False):
        l = LuaRuntime(unpack_returned_tuples=True)
        l.execute(MOCK + EXTRA)
        l.globals().X4LAN_CONFIG.slot = slot
        l.globals().X4LAN_CONFIG.coop = True
        if fire2:
            l.execute('X4LAN_CONFIG.fire2=true')
        l.execute('''
         sectors={[11]=22,[42]=22};ConvertIDTo64Bit=function(v) return tonumber(v) end
         api.GetContextByClass=function(object) return sectors[object] or 22 end
         api.SetObjectSectorPos=function(object,sector,p) positions[object]=p end
         groups={[11]={1,1},[42]={1,1}}
         api.GetDefensibleActiveWeaponGroup=function(ship,primary) return groups[ship][primary and 1 or 2] end
         api.SetDefensibleActiveWeaponGroup=function(ship,primary,g) groups[ship][primary and 1 or 2]=g end
         hull={}
         GetComponentData=function(object,a,b)
          if a=='macro' then return 'cluster_14_sector001_macro' end
          local h=hull[tostring(object)];if not h then return nil end;return h[1],h[2]
         end
        ''')
        for name in ('bubble_game.lua', 'motion.lua', 'coop_game.lua'):
            if (UI / name).is_file():
                l.execute((UI / name).read_text())
        if probe:
            l.execute((ROOT / 'dev/damage_probe.lua').read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         C=X4LAN_BUBBLE_API.coop;C.sectors.cluster_14_sector001_macro=22
         client:receiveExtension({'AUTH','1',client.lease,'1','p1','1','p1','p2','1','p2'},now)
         local remote=X4LAN_CONFIG.slot=='p1' and '2' or '1'
         function nav(seq,fire) client:receiveExtension({'NAV','1',client.lease,string.rep('b',32),remote,'1',tostring(seq),'cluster_14_sector001_macro','1000','10000','0','0','0','0','0',tostring(fire)},now) end
         function nav2(seq,time,fire)
          client:receiveExtension({'NAV2','1',client.lease,string.rep('b',32),remote,'1',tostring(seq),'cluster_14_sector001_macro',
           '1000','10000','0','0','0','0','0',tostring(fire or 0),tostring(time),'0','0','0','0','0','0'},now)
         end
         function fire2(seq,time,pg,sg,ps,ss)
          client:receiveExtension({'FIRE2','1',client.lease,string.rep('b',32),remote,'1',tostring(seq),tostring(time),
           tostring(pg),tostring(sg),tostring(ps),tostring(ss)},now)
         end
         worldRev=0
         function world(h1,s1,h2,s2)
          worldRev=worldRev+1
          local f={'WORLD','1',client.lease,string.rep('c',32),tostring(worldRev)}
          local v={h1,s1,0,h2,s2,0,100,100,100,100,0,1,1,0,0,0,0}
          for _,x in ipairs(v) do f[#f+1]=tostring(x) end
          client:receiveExtension(f,now)
         end
         function lines(reason) local out={};for _,v in ipairs(logs) do if v:find('DAMAGE_PROBE',1,true) and v:find('reason='..reason..' ',1,true) then out[#out+1]=v end end;return out end
         function count(name) local n=0;for _,r in ipairs(requests) do if r[2]==name then n=n+1 end end;return n end
         local function ser(v)
          if type(v)~='table' then return tostring(v) end
          local keys={};for k in pairs(v) do keys[#keys+1]=tostring(k) end;table.sort(keys)
          local out={};for _,k in ipairs(keys) do out[#out+1]=k..'='..ser(v[k] ~= nil and v[k] or v[tonumber(k)]) end
          return '{'..table.concat(out,',')..'}'
         end
         function payloads() local out={};for _,r in ipairs(requests) do if r[1]~='X4LAN_DamageProbe' then out[#out+1]=tostring(r[1])..':'..tostring(r[2])..':'..ser(r[3]) end end;return table.concat(out,'\\n') end
         function sequence() local out={};for _,r in ipairs(requests) do out[#out+1]=r[1]..':'..r[2]..(r[2]=='Mark' and ('#'..r[3].id) or '') end;return table.concat(out,',') end
        ''')
        return l

    def test_lines_carry_clocks_run_authority_and_world_revision(self):
        l = self.make()
        l.execute('''
         hull['11']={100,80};hull['42']={90,70};hull['t1']={55,0}
         events['X4LAN_DamageProbe.Targets'](nil,{'t1','t2'})
         events['X4LAN_Coop.Shot'](nil,1);assert(C.shots[1]==1,'production Shot handler still registered')
         now=10.35;frames.onUpdate()
         local line=lines('shot')[1];assert(line,'shot detected from counters')
         for _,part in ipairs({'run=','rt=10.350','gt=10.350','auth=1:p1/1,p2/1','wrev=','ship1=100.00/80.00','ship2=90.00/70.00',
           'target1=55.00/0.00','target2=dead','paused=false','mode=legacy'}) do
          assert(line:find(part,1,true),'missing '..part..' in '..line)
         end
        ''')

    def test_delayed_apply_is_requested_then_read_back_or_reported_missing(self):
        l = self.make(slot='p2')
        l.execute('''
         hull['11']={70,70};hull['42']={70,70}
         world(20,20,100,100);now=10.1;world(90,90,100,100)
         local req=lines('apply_requested');assert(#req==2,'two Apply dispatches')
         assert(req[1]:find('wrev=1',1,true) and req[1]:find('requested[ship1=20.00/20.00',1,true))
         assert(req[2]:find('wrev=2',1,true) and req[2]:find('requested[ship1=90.00/90.00',1,true))
         assert(req[1]:find('ship1=70.00/70.00',1,true) and req[2]:find('ship1=70.00/70.00',1,true),'observed is still pre-application')
         assert(req[1]~=req[2],'different authoritative inputs must not produce identical lines')
         -- Native readback for the second request only (the first one never completes).
         events['X4LAN_DamageProbe.Readback'](nil,'2|90|90|100|100|90|90|100|100')
         local rb=lines('apply_readback');assert(#rb==1 and rb[1]:find('apply=2 wrev=2',1,true))
         now=12;frames.onUpdate()
         local missing=lines('apply_missing');assert(#missing==1 and missing[1]:find('apply=1 wrev=1',1,true))
         events['X4LAN_DamageProbe.Readback'](nil,'9|1|2|3|4|5|6|7|8');assert(#lines('apply_readback_unmatched')==1)
         events['X4LAN_DamageProbe.Readback'](nil,'bad');assert(#lines('apply_readback_invalid')==1)
        ''')

    def test_identical_repeated_requests_pair_by_number(self):
        l = self.make(slot='p2')
        l.execute('''
         hull['11']={70,70};hull['42']={70,70}
         world(50,50,100,100);now=11.2;world(50,50,100,100)
         local req=lines('apply_requested');assert(#req==2,'unchanged health is re-sent after 1 s')
         assert(sequence():find('X4LAN_DamageProbe:Mark#1,X4LAN_Bubble:Apply,X4LAN_DamageProbe:Mark#2,X4LAN_Bubble:Apply',1,true),sequence())
         events['X4LAN_DamageProbe.Readback'](nil,'2|50|50|100|100|50|50|100|100')
         local rb=lines('apply_readback');assert(#rb==1 and rb[1]:find('apply=2 ',1,true),'the second identical request, not the first')
         now=13;frames.onUpdate();local m=lines('apply_missing');assert(#m==1 and m[1]:find('apply=1 ',1,true))
        ''')

    def test_readback_echo_must_match_its_request(self):
        l = self.make(slot='p2')
        l.execute('''
         world(20,20,100,100)
         events['X4LAN_DamageProbe.Readback'](nil,'1|90|90|100|100|90|90|100|100')
         assert(#lines('apply_readback_echo_mismatch')==1 and #lines('apply_readback')==0)
        ''')

    def test_fire2_lines_carry_source_metadata(self):
        l = self.make(fire2=True)
        l.execute('''
         nav2(1,50,0);fire2(1,50,2,3,4,5);now=10.3;frames.onUpdate()
         local line=lines('tick')[#lines('tick')]
         assert(line:find('fire2_2=[1:1@50.000000 groups=2/3 shots=4,5 on=ps]',1,true),line)
         assert(line:find('mode=fire2',1,true))
        ''')

    def full_payloads(self, probe, **options):
        l = self.make(probe, **options)
        l.execute(self.SCENARIO)
        return l.globals().payloads()

    SCENARIO = '''
     hull['11']={70,70};hull['42']={70,70}
     nav(1,0);now=10.4;nav(2,1);frames.onUpdate();now=11;frames.onUpdate()
     nav2(3,50,1);fire2(1,50,1,1,0,0);now=11.05;nav2(4,50.05,1);fire2(2,50.05,2,1,1,1);now=11.3;frames.onUpdate()
     now=11.4;fire2(3,50.4,2,1,1,1);now=11.9;frames.onUpdate()
     world(20,20,100,100);now=12;world(90,90,100,100);frames.onUpdate()
    '''

    def test_full_production_payloads_identical_with_and_without_probe(self):
        for options in ({}, {'fire2': True}, {'slot': 'p2'}, {'slot': 'p2', 'fire2': True}):
            with self.subTest(**options):
                self.assertEqual(self.full_payloads(False, **options), self.full_payloads(True, **options))

    def test_scenarios_exercise_fire_fire2_and_apply(self):
        self.assertIn(':Fire:', self.full_payloads(False))
        self.assertIn(':Fire2:', self.full_payloads(False, fire2=True))
        self.assertIn(':Apply:', self.full_payloads(False, slot='p2'))


class DamageProbeValidatorTests(unittest.TestCase):
    def line(self, reason, detail='-', run='aaaa0001', ship1='70.00/70.00', ship2='100.00/100.00', rt='10.000'):
        return (f'[=ERROR=] 1.0 [X4LAN] DAMAGE_PROBE run={run} seq=1 rt={rt} gt={rt} slot=p2 reason={reason} '
                f'detail={detail} paused=false auth=1:p1/1,p2/1 wrev=1 ship1={ship1} ship2={ship2} target1=none '
                f'target2=none shots=0,0 trigger1=0,0 trigger2=0,0 nav=-,- fire2_1=[-] fire2_2=[-] mode=legacy')

    def test_pairs_requests_and_flags_missing_late_mismatched_and_unmatched(self):
        rows = validator.parse([
            self.line('apply_requested', 'apply=1 wrev=1 requested[ship1=20.00/20.00 ship2=100.00/100.00 target1=- target2=-]'),
            self.line('apply_requested', 'apply=2 wrev=2 requested[ship1=90.00/90.00 ship2=100.00/100.00 target1=- target2=-]'),
            self.line('apply_readback', 'apply=2 wrev=2 delay=0.800 requested[ship1=90.00/90.00 ship2=100.00/100.00] observed[ship1=80.00/90.00 ship2=100.00/100.00]'),
            self.line('apply_missing', 'apply=1 wrev=1 age=1.600'),
            self.line('apply_requested', 'apply=3 wrev=3 requested[ship1=50.00/50.00 ship2=100.00/100.00 target1=- target2=-]'),
            self.line('apply_readback_unmatched', 'apply=9 requested[ship1=1.00/2.00 ship2=3.00/4.00] observed[ship1=5.00/6.00 ship2=7.00/8.00]'),
            self.line('apply_requested', 'apply=4 wrev=4 requested[ship1=60.00/60.00 ship2=100.00/100.00 target1=- target2=-]'),
            self.line('apply_readback_echo_mismatch', 'apply=4 wrev=4 requested[ship1=1.00/1.00 ship2=100.00/100.00] observed[ship1=60.00/60.00 ship2=100.00/100.00]'),
            'unrelated line',
        ])
        result = validator.summarise(rows)['aaaa0001']
        self.assertEqual(result['confirmed'], 1)
        self.assertEqual(result['missing'], [1, 3], 'reported missing and never completed')
        self.assertEqual(result['late'], [2])
        self.assertEqual(result['mismatched'], [2])
        self.assertEqual(result['unmatched'], 1)
        self.assertEqual(result['echo_mismatch'], [4])

    def test_drift_after_confirmed_readback_is_only_a_candidate(self):
        rows = validator.parse([
            self.line('apply_requested', 'apply=1 wrev=1 requested[ship1=90.00/90.00 ship2=100.00/100.00 target1=- target2=-]'),
            self.line('apply_readback', 'apply=1 wrev=1 delay=0.020 requested[ship1=90.00/90.00 ship2=100.00/100.00] observed[ship1=90.00/90.00 ship2=100.00/100.00]'),
            self.line('tick', ship1='90.00/90.00', rt='10.2'),
            self.line('tick', ship1='75.00/60.00', rt='10.4'),
            self.line('apply_requested', 'apply=2 wrev=2 requested[ship1=75.00/60.00 ship2=100.00/100.00 target1=- target2=-]'),
            self.line('tick', ship1='10.00/10.00', rt='10.6'),
        ])
        result = validator.summarise(rows)['aaaa0001']
        self.assertEqual(len(result['candidate_drift']), 1, 'only between a confirmed readback and the next request')
        self.assertEqual(result['candidate_drift'][0]['observed'], (75.0, 60.0))
        self.assertEqual(result['missing'], [2])

    def test_separate_runs_are_not_mixed(self):
        rows = validator.parse([
            self.line('apply_requested', 'apply=1 wrev=1 requested[ship1=90.00/90.00 ship2=100.00/100.00 target1=- target2=-]', run='r1'),
            self.line('apply_readback', 'apply=1 wrev=1 delay=0.010 requested[ship1=90.00/90.00 ship2=100.00/100.00] observed[ship1=90.00/90.00 ship2=100.00/100.00]', run='r2'),
        ])
        result = validator.summarise(rows)
        self.assertEqual(result['r1']['missing'], [1])
        self.assertEqual(result['r2']['confirmed'], 1)


if __name__ == '__main__':
    unittest.main()
