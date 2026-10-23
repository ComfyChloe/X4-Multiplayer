"""HEALTH relay generations and LuaJIT doubles for victim-pilot health; native acceptance remains required."""
import unittest
from pathlib import Path
from x4lan.coop import CoopSession
from test_host import TOKEN, P1, P2, FOREIGN, hello
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'
WORLD = [100, 100, 0, 64.5, 30, 0, 100, 100, 100, 100, 200, 1, 1, 0, 0, 0, 0]


class HealthRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = CoopSession(TOKEN, 'test', 'bubble')
        self.a = self.s.receive(hello(), P1, 0)[0].decode().split('|')[3]
        self.b = self.s.receive(hello('p2', '2' * 32), P2, 0)[0].decode().split('|')[3]
        self.s.receive(f'WORLD|1|{self.a}|1|{"|".join(map(str, WORLD))}'.encode(), P1, .05)
        self.crew(1, 2)
        self.s.snapshots(.12); self.g = self.s.health_gen[2]     # the generation p2 now pilots under

    def crew(self, seq, ship, now=.1):
        self.s.receive(f'CREW|1|{self.b}|{seq}|{ship}|{ship}'.encode(), P2, now)

    def health(self, seq=1, hull='80', shield='50', entity=2, gen=None, address=P2, now=.2):
        gen = self.g if gen is None else gen
        return self.s.receive(f'HEALTH|1|{self.b}|{entity}|{gen}|{seq}|{hull}|{shield}'.encode(), address, now)

    def ack(self, gen=None, entity=2, now=.15):
        gen = self.g if gen is None else gen
        self.s.receive(f'HACK|1|{self.b}|{entity}|{gen}'.encode(), P2, now)

    def rows(self, kind, now=.3, to=P1):
        return [p.decode().split('|') for a, p in self.s.snapshots(now) if a == to and p.startswith(kind + b'|')]

    def test_baseline_is_the_host_world_and_repeats_until_acknowledged(self):
        self.assertEqual(self.rows(b'HBASE', to=P2), [['HBASE', '1', self.b, '2', str(self.g), '64.500', '30.000']])
        self.assertEqual(self.rows(b'HBASE', to=P1), [])
        self.health()                                           # before the acknowledgement
        self.assertEqual(self.rows(b'HEALTH'), [])
        self.ack(gen=self.g + 1)                                # a wrong generation
        self.assertEqual(len(self.rows(b'HBASE', to=P2)), 1)
        self.ack()
        self.assertEqual(self.rows(b'HBASE', to=P2), [])
        self.health(seq=2)
        self.assertEqual(self.rows(b'HEALTH'), [['HEALTH', '1', self.a, self.b, '2', str(self.g), '2', '80', '50']])
        self.assertEqual(self.rows(b'HEALTH', to=P2), [])

    def test_frames_need_owner_pilot_live_generation_order_and_valid_values(self):
        self.ack()
        self.health(entity=1)
        self.health(address=FOREIGN)
        for hull, shield in (('nan', '0'), ('-1', '0'), ('101', '0'), ('x', '0')):
            self.health(hull=hull, shield=shield)
        self.assertEqual(self.rows(b'HEALTH'), [])
        self.health(seq=3, hull='70'); self.health(seq=3, hull='10'); self.health(seq=2, hull='10')
        self.assertEqual(self.rows(b'HEALTH')[0][7], '70')
        self.assertEqual(self.rows(b'HEALTH', now=1.2), [])     # stale after 0.75 s

    def test_leave_and_reenter_without_a_snapshot_still_closes_the_old_generation(self):
        self.ack(); self.health(seq=5)
        self.crew(2, 0, now=.30); self.crew(3, 2, now=.31)
        self.health(seq=6, now=.32)                             # generation 1 must be over
        self.assertEqual(self.rows(b'HEALTH', now=.33), [])
        self.assertEqual(self.s.health_gen[2], self.g + 2)

    def test_same_owner_leave_and_reenter_starts_a_new_generation(self):
        self.ack(); self.health(seq=5)
        self.crew(2, 0, now=.3)                                 # leaves the pilot seat
        self.assertEqual(self.rows(b'HEALTH', now=.35), [])
        self.crew(3, 2, now=.4)                                 # same owner and epoch, back aboard
        self.health(seq=6, now=.45)                             # old generation: refused
        self.assertEqual(self.rows(b'HEALTH', now=.5), [])
        self.assertEqual(self.rows(b'HBASE', now=.5, to=P2)[0][4], str(self.g + 2))
        self.ack(gen=self.g + 2, now=.55); self.health(seq=1, gen=self.g + 2, now=.6)
        self.assertEqual(self.rows(b'HEALTH', now=.65)[0][5], str(self.g + 2))


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
         function sentKind(kind) local out={};for _,p in ipairs(sent) do if p:sub(1,#kind+1)==kind..'|' then out[#out+1]=p end end;return out end
         function last(name) local r;for _,q in ipairs(requests) do if q[1]=='X4LAN_Health' and q[2]==name then r=q[3] end end;return r end
         function base(gen) client:receiveExtension({'HBASE','1',client.lease,'2',tostring(gen),'64.5','30'},now) end
        ''')
        return l

    def test_guest_adopts_the_baseline_once_then_acknowledges_and_reports(self):
        out = self.make('p2').execute('''
         events['X4LAN_Health.Own'](nil,'2|90|50');now=10.2;frames.onUpdate()
         local early=#sentKind('HEALTH')                         -- no generation yet: silent
         base(4);base(4);local b=last('Base');local bases=countRequest('Base')
         events['X4LAN_Health.Based'](nil,'2|4')
         now=10.25;frames.onUpdate();local owned=last('Authority')
         base(4);local acks=sentKind('HACK')
         events['X4LAN_Health.Own'](nil,'2|64.5|30');now=10.3;frames.onUpdate()
         events['X4LAN_Health.Own'](nil,'2|64.5|30');now=10.35;frames.onUpdate()   -- paced
         events['X4LAN_Health.Own'](nil,'2|64.5|20');now=10.36;frames.onUpdate()   -- shield drop: at once
         local h=sentKind('HEALTH')
         return early,b.gen,b.hull,bases,owned.entity,owned.gen,owned.own,#acks,acks[1],#h,h[1],h[2]''')
        early, gen, hull, bases, entity, agen, own, nacks, ack, count, first, second = out
        self.assertEqual((early, gen, hull, bases, entity, agen, own, nacks, count), (0, 4, 64.5, 1, 2, 4, True, 1, 2))
        self.assertTrue(ack.endswith('|2|4'), ack)
        self.assertTrue(first.endswith('|2|4|1|64.500|30.000'), first)
        self.assertTrue(second.endswith('|2|4|2|64.500|20.000'), second)

    def test_guest_drops_its_generation_when_it_leaves_the_pilot_seat(self):
        out = self.make('p2').execute('''
         base(4);events['X4LAN_Health.Based'](nil,'2|4');now=10.2;frames.onUpdate()
         auth(2,1,'0');now=10.3;frames.onUpdate();local owned=last('Authority')
         events['X4LAN_Health.Own'](nil,'2|50|0');now=10.5;frames.onUpdate()
         events['X4LAN_Health.Based'](nil,'2|4')                 -- a late confirmation is ignored
         return owned.own,owned.gen,#sentKind('HEALTH'),X4LAN_BUBBLE_API.health.live''')
        self.assertEqual(tuple(out), (False, 0, 0, None))

    def test_guest_never_readopts_an_older_baseline(self):
        out = self.make('p2').execute('''
         base(5);events['X4LAN_Health.Based'](nil,'2|5');now=10.2;frames.onUpdate()
         base(4)                                                  -- delayed older generation
         local h=X4LAN_BUBBLE_API.health
         local order={};for _,q in ipairs(requests) do if q[1]=='X4LAN_Health' and (q[2]=='Base' or q[2]=='Authority') then order[#order+1]=q[2]..':'..q[3].gen end end
         return h.live.gen,h.basing,table.concat(order,',')''')
        self.assertEqual(tuple(out), (5, None, 'Authority:5,Base:5,Authority:5'))
        md = (ROOT / 'extension/x4_lan_lab/md/x4lan_health.xml').read_text()
        self.assertIn('$a.$gen == $arena.$healthAuth.{$a.$entity} and @$ship.isoperational', md)  # Base fence
        self.assertIn('exact="$a.$own and $a.$gen gt 0"', md)

    def test_host_applies_in_generation_order_and_releases_on_transfer_or_staleness(self):
        out = self.make('p1').execute('''
         local src=string.rep('c',32)
         local function hp(gen,seq,hull) client:receiveExtension({'HEALTH','1',client.lease,src,'2',tostring(gen),tostring(seq),hull,'50'},now) end
         hp(3,2,'80');local a=last('Apply')
         hp(3,2,'10');hp(3,1,'10');hp(2,9,'10')                   -- duplicate, older, older generation
         client:receiveExtension({'HEALTH','1',client.lease,src,'1','3','4','10','0'},now)   -- host's own ship
         local applies=countRequest('Apply')
         hp(4,1,'1.0');local lethal=last('Apply')                  -- a newer generation resets the order
         now=10.5;frames.onUpdate();local fresh=countRequest('Release')
         now=11.6;frames.onUpdate();local stale=last('Release')
         hp(4,2,'60');auth(2,2,'p2');now=11.7;frames.onUpdate()
         return a.entity,a.gen,a.hull,applies,lethal.gen,lethal.hull,fresh,stale.gen,countRequest('Release')''')
        self.assertEqual(tuple(out), (2, 3, 80, 1, 4, 1.0, 0, 4, 2))     # a stale generation may resume

    def test_queued_lethal_of_an_old_generation_never_follows_a_newer_release(self):
        out = self.make('p1').execute('''
         local src=string.rep('c',32)
         local function hp(gen,seq,hull) client:receiveExtension({'HEALTH','1',client.lease,src,'2',tostring(gen),tostring(seq),hull,'0'},now) end
         hp(5,1,'40');auth(2,1,'0');now=10.2;frames.onUpdate()     -- pilot left: release generation 5
         auth(3,1,'p2');hp(5,2,'1.0')                               -- a late lethal frame of generation 5
         local order={};for _,q in ipairs(requests) do if q[1]=='X4LAN_Health' and (q[2]=='Apply' or q[2]=='Release') then order[#order+1]=q[2]..':'..q[3].gen end end
         return table.concat(order,','),tostring(last('Release').closed)''')
        self.assertEqual(tuple(out), ('Apply:5,Release:5', 'true'))
        md = (ROOT / 'extension/x4_lan_lab/md/x4lan_health.xml').read_text()
        self.assertIn('$a.$gen gt $arena.$healthClosed.{$e} and $a.$gen ge $arena.$healthGen.{$e}', md)
        self.assertIn('<do_if value="event.param3.$closed"><set_value name="$arena.$healthClosed.{$e}"', md)
        self.assertIn('exact="$arena.$healthValue.{$e}.{1}"', md)  # release restores the canonical value

    def test_disabled_flag_registration_and_bubble_guards(self):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        l.globals().X4LAN_CONFIG.coop = True
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'health_game.lua'):
            l.execute((UI / file).read_text())
        self.assertIsNone(l.eval('X4LAN_BUBBLE_API.health'))
        ui = (ROOT / 'extension/x4_lan_lab/ui.xml').read_text()
        self.assertLess(ui.index('ui/coop_game.lua'), ui.index('ui/health_game.lua'))
        self.assertIn('pilot_health = false', (UI / 'config.lua').read_text())
        bubble = (ROOT / 'extension/x4_lan_lab/md/x4lan_bubble.xml').read_text()
        self.assertEqual(bubble.count('@$healthLive.{'), 4)       # Publish: hull and shield of both ships
        self.assertIn('<do_if value="not @Arena.$healthOwn.{$i}"><set_object_hull object="$ship"', bubble)
        self.assertIn('le (if @Arena.$healthOwn.{$i} then 0 else 1.01)', bubble)
        guards = (ROOT / 'dev/life_guards.py').read_text()          # the generator keeps the same guards
        for text in ('@$healthLive.{%d}', "'not @Arena.$healthOwn.{$i}'", 'le (if @Arena.$healthOwn.{$i} then 0 else 1.01)'):
            self.assertIn(text, guards)


if __name__ == '__main__':
    unittest.main()
