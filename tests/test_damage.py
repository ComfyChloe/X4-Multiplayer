"""HIT2 relay validation and LuaJIT doubles for shooter-confirmed hits; native two-PC acceptance remains required."""
import unittest
from pathlib import Path
from x4lan.coop import CoopSession
from test_host import TOKEN, P1, P2, FOREIGN, hello
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'


class Hit2RelayTests(unittest.TestCase):
    def setUp(self):
        self.s = CoopSession(TOKEN, 'test', 'bubble')
        self.a = self.s.receive(hello(), P1, 0)[0].decode().split('|')[3]
        self.b = self.s.receive(hello('p2', '2' * 32), P2, 0)[0].decode().split('|')[3]

    def hit(self, seq=1, totals=('5', '2', '0', '0', '0', '0', '0', '0'), entity=2, epoch=1, address=P2, lease=None, now=.2):
        packet = f'HIT2|1|{lease or self.b}|{entity}|{epoch}|{seq}|' + '|'.join(totals)
        return self.s.receive(packet.encode(), address, now)

    def forwarded(self, now=.3, to=P1):
        return [p.decode().split('|') for a, p in self.s.snapshots(now) if a == to and p.startswith(b'HIT2|')]

    def test_guest_totals_reach_only_the_host_with_source_lease(self):
        self.hit()
        rows = self.forwarded()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2:5], [self.a, self.b, '2'])
        self.assertEqual(rows[0][5:], ['1', '1', '5', '2', '0', '0', '0', '0', '0', '0'])
        self.assertEqual(self.forwarded(to=P2), [])

    def test_host_foreign_wrong_owner_epoch_and_bad_totals_are_dropped(self):
        self.hit(address=P1, lease=self.a, entity=1)      # host hits are native, never claimed
        self.hit(address=FOREIGN)
        self.hit(entity=1)
        self.hit(epoch=2)
        for bad in (('nan',) + ('0',) * 7, ('-1',) + ('0',) * 7, ('2e9',) + ('0',) * 7, ('x',) + ('0',) * 7):
            self.hit(totals=bad)
        self.hit(totals=('0',) * 7)
        self.assertEqual(self.forwarded(), [])

    def test_totals_are_monotonic_and_stale_frames_expire(self):
        self.hit(seq=2, totals=('5', '2') + ('0',) * 6)
        self.hit(seq=2, totals=('9', '2') + ('0',) * 6)    # same sequence
        self.hit(seq=3, totals=('4', '2') + ('0',) * 6)    # a total went down
        self.assertEqual(self.forwarded()[0][7:9], ['5', '2'])
        self.hit(seq=4, totals=('6', '3') + ('0',) * 6, now=1)
        self.assertEqual(self.forwarded(now=1.1)[0][7:9], ['6', '3'])
        self.assertEqual(self.forwarded(now=3.5), [])

    def test_authority_transfer_stops_forwarding_old_epoch(self):
        self.hit()
        self.s.epochs[2] += 1
        self.assertEqual(self.forwarded(), [])


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class DamageAdapterTests(unittest.TestCase):
    def make(self, slot):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        g = l.globals(); g.X4LAN_CONFIG.slot = slot; g.X4LAN_CONFIG.coop = True; g.X4LAN_CONFIG.damage_claims = True
        l.execute('''
         sectors={[11]=22,[42]=22};ConvertIDTo64Bit=function(v) return tonumber(v) end
         api.GetContextByClass=function(object) return sectors[object] or 22 end
         GetComponentData=function() return 'cluster_14_sector001_macro' end
        ''')
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'damage_game.lua'):
            l.execute((UI / file).read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         client:receiveExtension({'AUTH','1',client.lease,'1','p1','1','p1','p2','1','p2'},now)
         now=10.1;frames.onUpdate();assert(countRequest('Setup')>=1);events['X4LAN_Damage.Ready']()
         D=X4LAN_BUBBLE_API.damage
         function hits() local out={};for _,p in ipairs(sent) do if p:sub(1,5)=='HIT2|' then out[#out+1]=p end end;return out end
         function last(name) local r;for _,q in ipairs(requests) do if q[1]=='X4LAN_Damage' and q[2]==name then r=q[3] end end;return r end
        ''')
        return l

    def test_guest_reports_only_its_own_same_time_hits_as_cumulative_totals(self):
        l = self.make('p2')
        out = l.execute('''
         events['X4LAN_Damage.Attacked'](nil,'1|2|1|50.0')         -- own ship hits the host ship
         events['X4LAN_Damage.Damaged'](nil,'1|s|30|50.0|0')
         events['X4LAN_Damage.Damaged'](nil,'1|h|12.5|50.0|0')
         events['X4LAN_Damage.Damaged'](nil,'1|h|9|50.05|0')        -- later damage, no attack event: dropped
         events['X4LAN_Damage.Damaged'](nil,'3|h|99|50.0|0')        -- no attacker: not claimed
         events['X4LAN_Damage.Attacked'](nil,'3|0|0|50.1')          -- an NPC hit a target
         events['X4LAN_Damage.Damaged'](nil,'3|h|40|50.1|0')
         events['X4LAN_Damage.Attacked'](nil,'2|1|0|50.2')          -- replayed host hits the guest: native host authority
         events['X4LAN_Damage.Damaged'](nil,'2|h|70|50.2|0')
         events['X4LAN_Damage.Attacked'](nil,'4|2|1|50.2')          -- own hit in a WORLD Apply frame: dropped
         events['X4LAN_Damage.Damaged'](nil,'4|h|5|50.2|1')
         now=10.2;frames.onUpdate();local early=#hits();now=10.21;frames.onUpdate()
         local first=hits()[1]
         events['X4LAN_Damage.Damaged'](nil,'4|s|8|50.3|0')         -- damage before its attack event
         events['X4LAN_Damage.Attacked'](nil,'4|2|1|50.3')
         now=10.25;frames.onUpdate();now=10.26;frames.onUpdate()
         local h=hits();return early,#h,first,h[#h]''')
        early, count, first, latest = out
        self.assertEqual((early, count), (1, 2))
        self.assertTrue(first.endswith('|2|1|1|30.000|12.500|0.000|0.000|0.000|0.000|0.000|0.000'), first)
        self.assertTrue(latest.endswith('|2|1|2|30.000|12.500|0.000|0.000|0.000|0.000|8.000|0.000'), latest)

    def test_guest_resends_unchanged_totals_and_drops_them_on_authority_change(self):
        l = self.make('p2')
        out = l.execute('''
         events['X4LAN_Damage.Attacked'](nil,'1|2|1|50.0');events['X4LAN_Damage.Damaged'](nil,'1|h|5|50.0|0')
         now=10.19;frames.onUpdate();now=10.2;frames.onUpdate();now=10.3;frames.onUpdate();local quiet=#hits()
         now=10.5;frames.onUpdate();local resent=#hits()
         client:receiveExtension({'AUTH','1',client.lease,'2','p1','1','p1','p2','2','p2'},now)
         now=11;frames.onUpdate();local after=#hits()
         events['X4LAN_Damage.Attacked'](nil,'1|2|1|60.0');events['X4LAN_Damage.Damaged'](nil,'1|h|1|60.0|0')
         now=11.1;frames.onUpdate();now=11.11;frames.onUpdate();local h=hits()
         return quiet,resent,after,h[#h]''')
        quiet, resent, after, latest = out
        self.assertEqual((quiet, resent, after), (1, 2, 2))
        self.assertTrue(latest.endswith('|2|2|3|0.000|1.000|0.000|0.000|0.000|0.000|0.000|0.000'), latest)

    def test_host_applies_increases_once(self):
        l = self.make('p1')
        out = l.execute('''
         local src=string.rep('c',32)
         local function hit2(seq,epoch,...) client:receiveExtension({'HIT2','1',client.lease,src,'2',tostring(epoch),tostring(seq),...},now) end
         hit2(1,1,'30','12.5','0','0','0','0','0','0')
         local a=last('Apply');local first={a.entity,a.values[1],a.values[2]}
         hit2(1,1,'90','90','0','0','0','0','0','0')                -- duplicate sequence
         hit2(2,1,'30','12.5','0','0','0','0','0','0')              -- unchanged: nothing to apply
         hit2(3,1,'20','12.5','0','0','0','0','0','0')              -- decreasing: rejected
         hit2(4,1,'35','12.5','0','0','0','0','0','0')
         local b=last('Apply');local applies=countRequest('Apply')
         hit2(5,2,'1','1','0','0','0','0','0','0')                  -- old epoch on the host: ignored
         return first[1],first[2],first[3],b.values[1],applies,countRequest('Apply')''')
        self.assertEqual(tuple(out), (2, 30, 12.5, 5, 2, 2))

    def test_host_undoes_only_same_time_replayed_hits_and_floors_own_target_hits(self):
        l = self.make('p1')
        out = l.execute('''
         -- A:primary's reproduction: remote 30 at 50.0, then the host's own 40 at 50.1 whose
         -- damage event precedes its attack event. Only the remote 30 may be given back.
         events['X4LAN_Damage.Attacked'](nil,'3|2|0|50.0');events['X4LAN_Damage.Damaged'](nil,'3|s|30|50.0|0')
         events['X4LAN_Damage.Damaged'](nil,'3|s|40|50.1|0');events['X4LAN_Damage.Attacked'](nil,'3|1|1|50.1')
         events['X4LAN_Damage.Damaged'](nil,'3|h|6|50.1|0')
         events['X4LAN_Damage.Attacked'](nil,'4|2|0|50.2');events['X4LAN_Damage.Attacked'](nil,'4|1|1|50.2')
         events['X4LAN_Damage.Damaged'](nil,'4|h|9|50.2|0')         -- two attackers at once: dropped
         events['X4LAN_Damage.Attacked'](nil,'1|2|0|50.3');events['X4LAN_Damage.Damaged'](nil,'1|h|4|50.3|1')  -- setter frame
         now=10.5;frames.onUpdate();local early=countRequest('Undo');now=10.51;frames.onUpdate()
         local u=last('Undo')
         return early,countRequest('Undo'),u.victim,u.shield,u.hull,countRequest('Floor'),last('Floor').victim''')
        self.assertEqual(tuple(out), (1, 1, 3, 30, 0, 1, 3))

    def test_disabled_flag_and_ui_registration(self):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        l.globals().X4LAN_CONFIG.coop = True
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'damage_game.lua'):
            l.execute((UI / file).read_text())
        self.assertIsNone(l.eval('X4LAN_BUBBLE_API.damage'))
        ui = (ROOT / 'extension/x4_lan_lab/ui.xml').read_text()
        self.assertLess(ui.index('ui/coop_game.lua'), ui.index('ui/damage_game.lua'))
        self.assertIn('damage_claims = false', (UI / 'config.lua').read_text())


if __name__ == '__main__':
    unittest.main()
