"""HEALTH relay validation and LuaJIT doubles for victim-pilot health; native acceptance remains required."""
import unittest
from pathlib import Path
from x4lan.coop import CoopSession
from test_host import TOKEN, P1, P2, FOREIGN, hello
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'


class HealthRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = CoopSession(TOKEN, 'test', 'bubble')
        self.a = self.s.receive(hello(), P1, 0)[0].decode().split('|')[3]
        self.b = self.s.receive(hello('p2', '2' * 32), P2, 0)[0].decode().split('|')[3]
        self.s.receive(f'CREW|1|{self.b}|1|2|2'.encode(), P2, .1)            # p2 pilots ship 2

    def health(self, seq=1, hull='80', shield='50', entity=2, epoch=1, address=P2, lease=None, now=.2):
        packet = f'HEALTH|1|{lease or self.b}|{entity}|{epoch}|{seq}|{hull}|{shield}'
        return self.s.receive(packet.encode(), address, now)

    def forwarded(self, now=.3, to=P1):
        return [p.decode().split('|') for a, p in self.s.snapshots(now) if a == to and p.startswith(b'HEALTH|')]

    def test_pilot_reading_reaches_the_other_peer_with_source_lease(self):
        self.health()
        rows = self.forwarded()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2:], [self.a, self.b, '2', '1', '1', '80', '50'])
        self.assertEqual(self.forwarded(to=P2), [])

    def test_only_owner_pilot_current_epoch_and_valid_values(self):
        self.health(entity=1)                                   # p2 does not own ship 1
        self.health(address=FOREIGN)
        self.health(epoch=2)
        for hull, shield in (('nan', '0'), ('-1', '0'), ('101', '0'), ('x', '0')):
            self.health(hull=hull, shield=shield)
        self.assertEqual(self.forwarded(), [])
        self.s.receive(f'CREW|1|{self.b}|2|0|0'.encode(), P2, .2)  # left the pilot seat
        self.health(seq=2)
        self.assertEqual(self.forwarded(), [])

    def test_order_expiry_and_transfer(self):
        self.health(seq=3, hull='70')
        self.health(seq=3, hull='10')                           # same sequence
        self.health(seq=2, hull='10')                           # older
        self.assertEqual(self.forwarded()[0][7], '70')
        self.assertEqual(self.forwarded(now=1.2), [])
        self.health(seq=4, hull='60', now=1.3)
        self.s.epochs[2] += 1
        self.assertEqual(self.forwarded(now=1.4), [])


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class HealthAdapterTests(unittest.TestCase):
    def make(self, slot):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        g = l.globals(); g.X4LAN_CONFIG.slot = slot; g.X4LAN_CONFIG.coop = True; g.X4LAN_CONFIG.pilot_health = True
        l.execute('''
         sectors={[11]=22,[42]=22};ConvertIDTo64Bit=function(v) return tonumber(v) end
         api.GetContextByClass=function(object) return sectors[object] or 22 end
         GetComponentData=function() return 'cluster_14_sector001_macro' end
        ''')
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'health_game.lua'):
            l.execute((UI / file).read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         function auth(rev,e2,p2) client:receiveExtension({'AUTH','1',client.lease,tostring(rev),'p1','1','p1','p2',tostring(e2),p2},now) end
         auth(1,1,'p2')
         now=10.1;frames.onUpdate();assert(countRequest('Setup')>=1);events['X4LAN_Health.Ready']()
         function healths() local out={};for _,p in ipairs(sent) do if p:sub(1,7)=='HEALTH|' then out[#out+1]=p end end;return out end
         function last(name) local r;for _,q in ipairs(requests) do if q[1]=='X4LAN_Health' and q[2]==name then r=q[3] end end;return r end
        ''')
        return l

    def test_guest_reports_its_piloted_ship_paced_and_at_once_on_damage(self):
        out = self.make('p2').execute('''
         now=10.2;frames.onUpdate();local owned=last('Owned')
         events['X4LAN_Health.Own'](nil,'2|90|50');now=10.25;frames.onUpdate()
         events['X4LAN_Health.Own'](nil,'2|90|50');now=10.3;frames.onUpdate()       -- paced: nothing new
         local paced=#healths()
         events['X4LAN_Health.Own'](nil,'2|90|40');now=10.31;frames.onUpdate()      -- shield drop: at once
         events['X4LAN_Health.Own'](nil,'1|70|0');now=10.5;frames.onUpdate()        -- not its ship
         local h=healths();return owned.entity,owned.own,paced,#h,h[1],h[2]''')
        entity, own, paced, count, first, second = out
        self.assertEqual((entity, own, paced, count), (2, True, 1, 2))
        self.assertTrue(first.endswith('|2|1|1|90.000|50.000'), first)
        self.assertTrue(second.endswith('|2|1|2|90.000|40.000'), second)

    def test_guest_stops_and_clears_ownership_when_it_leaves_the_pilot_seat(self):
        out = self.make('p2').execute('''
         now=10.2;frames.onUpdate();events['X4LAN_Health.Own'](nil,'2|90|50');now=10.25;frames.onUpdate()
         auth(2,1,'0');now=10.5;frames.onUpdate();events['X4LAN_Health.Own'](nil,'2|80|50');now=10.7;frames.onUpdate()
         return #healths(),last('Owned').own''')
        self.assertEqual(tuple(out), (1, False))

    def test_host_applies_in_order_and_releases_on_transfer(self):
        out = self.make('p1').execute('''
         local src=string.rep('c',32)
         local function hp(seq,epoch,hull,shield) client:receiveExtension({'HEALTH','1',client.lease,src,'2',tostring(epoch),tostring(seq),hull,shield},now) end
         hp(2,1,'80','50');local a=last('Apply')
         hp(2,1,'10','0');hp(1,1,'10','0')                         -- duplicate and older
         hp(3,2,'10','0')                                           -- not the current epoch
         client:receiveExtension({'HEALTH','1',client.lease,src,'1','1','4','10','0'},now)   -- the host's own ship
         local applies=countRequest('Apply')
         hp(5,1,'1.0','0');local lethal=last('Apply')
         now=10.3;frames.onUpdate();local before=countRequest('Release')
         auth(2,2,'p2');now=10.4;frames.onUpdate()
         return a.entity,a.hull,a.shield,applies,lethal.hull,before,countRequest('Release'),last('Release').entity,last('Owned')''')
        self.assertEqual(tuple(out), (2, 80, 50, 1, 1.0, 0, 1, 2, None))

    def test_disabled_flag_and_ui_registration(self):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        l.globals().X4LAN_CONFIG.coop = True
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'health_game.lua'):
            l.execute((UI / file).read_text())
        self.assertIsNone(l.eval('X4LAN_BUBBLE_API.health'))
        ui = (ROOT / 'extension/x4_lan_lab/ui.xml').read_text()
        self.assertLess(ui.index('ui/coop_game.lua'), ui.index('ui/health_game.lua'))
        self.assertIn('pilot_health = false', (UI / 'config.lua').read_text())


if __name__ == '__main__':
    unittest.main()
