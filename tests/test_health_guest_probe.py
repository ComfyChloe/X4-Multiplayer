"""LuaJIT doubles for the dev-only guest health probe; the native guest run is A:primary's."""
import unittest
from pathlib import Path
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class HealthGuestProbeTests(unittest.TestCase):
    def make(self):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        c = l.globals().X4LAN_CONFIG
        c.slot, c.coop, c.pilot_health, c.health_probe, c.test_profile = 'p2', True, True, True, '45841'
        l.execute("sectors={};ConvertIDTo64Bit=function(v) return tonumber(v) end;GetComponentData=function() return 'cluster_14_sector001_macro' end")
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'health_game.lua'):
            l.execute((UI / file).read_text())
        # Native transports are userdata: any field write must fail, so the probe may not try.
        l.execute('''local real=X4LAN_TRANSPORT.open
         X4LAN_TRANSPORT.open=function(...) local t=real(...)
          return setmetatable({},{__index=function(_,k) return t[k] end,__newindex=function() error('attempt to index userdata') end}) end''')
        l.execute((ROOT / 'dev/health_guest_probe.lua').read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         client:receiveExtension({'AUTH','1',client.lease,'1','p1','1','p1','p2','1','p2'},now)
         now=10.1;frames.onUpdate();events['X4LAN_Health.Ready']()
         S=X4LAN_BUBBLE_API.state;H=X4LAN_BUBBLE_API.health
         function own(age,gen,hull) events['X4LAN_Health.Own'](nil,age..'|2026-10-24T01:30:00|2|'..gen..'|'..hull..'|30') end
         function tick(t) now=t;frames.onUpdate() end
         function probe() local out={};for _,m in ipairs(logs) do if m:find('HEALTH_GUEST_PROBE',1,true) then out[#out+1]=m end end;return table.concat(out,'\\n') end
         function healths() local n=0;for _,p in ipairs(sent) do if p:sub(1,7)=='HEALTH|' then n=n+1 end end;return n end
         function count(name) local n=0;for _,q in ipairs(requests) do if q[2]==name then n=n+1 end end;return n end
         own('50',0,64.5);tick(10.15)
         client:receiveExtension({'HGEN','1',client.lease,'1','1','4'},now)
         client:receiveExtension({'HBASE','1',client.lease,'2','4','64.5','30'},now)
         events['X4LAN_Health.Based'](nil,'2|4')
         client:receiveExtension({'HBASE','1',client.lease,'2','4','64.5','30'},now)
         own('50.1',4,64.5);tick(10.2)
        ''')
        return l

    def test_handover_order_is_logged(self):
        text = self.make().execute('return probe()')
        order = [m for m in ('Authority entity=2 gen=4 own=false', 'Base entity=2 gen=4', 'Based 2|4',
                             'Authority entity=2 gen=4 own=true', 'sent HACK gen=4', 'sent HEALTH gen=4 seq=1 hull=64.500') ]
        positions = [text.find(m) for m in order]
        self.assertTrue(all(p >= 0 for p in positions), list(zip(order, positions)))
        self.assertEqual(positions, sorted(positions))

    def test_phase_actions_damage_suspend_inject_and_old_base(self):
        out = self.make().execute('''
         S.remoteRevision=3001;tick(10.3);tick(10.4);local damage=count('LocalDamage')
         S.remoteRevision=5001;tick(10.5);local before=healths()
         own('50.6',4,50);tick(11.0);own('50.7',4,50);tick(12.0);local during=healths()-before   -- only health dispatch is suspended
         local b0=count('Base');client:receiveExtension({'HBASE','1',client.lease,'2','5','40','10'},now)
         client:receiveExtension({'HGEN','1',client.lease,'1','1','5'},now)
         local lost=count('Base')-b0+(H.valid[2]==5 and 1 or 0)               -- controlled loss: nothing adopted
         own('52.6',4,50);tick(13.6)
         local injected=nil;for _,q in ipairs(requests) do if q[2]=='Authority' and q[3].own and q[3].gen==4 and q[3].stamp then injected=q[3] end end
         S.remoteRevision=6001;local bases=count('Base');tick(13.7)
         return damage,during,injected and injected.deadline,count('Base')-bases,probe():find('inject old HBASE gen=4',1,true)~=nil,lost,probe():find('dropping HGEN/HBASE during suspension',1,true)~=nil''')
        damage, during, deadline, rebased, logged, lost, dropped = out
        self.assertEqual((damage, during, rebased, logged, lost, dropped), (1, 0, 0, True, 0, True))
        self.assertAlmostEqual(deadline, 53.6)                             # fresh window from the latest readback


if __name__ == '__main__':
    unittest.main()
