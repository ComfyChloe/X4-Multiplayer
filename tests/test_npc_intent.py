"""npc_combat_intent: relay forwarding, host publication, guest puppet latch/timeout/aim,
hit attribution and compatibility with peers that lack the feature. Engine doubles only;
no native acceptance claim."""
import unittest
import xml.etree.ElementTree as E
from pathlib import Path

from x4lan.universe import Baseline, UniverseSession
from test_host import TOKEN, P1, P2, FOREIGN
from test_universe import SECTOR, OTHER, row
from test_game_adapter import LuaRuntime
import test_universe_adapter as ua

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'
MD = ROOT / 'extension/x4_lan_lab/md/x4lan_npc_intent.xml'


class IntentRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = UniverseSession(TOKEN, 'world1-test', 'bubble')

        def join(slot, address, nonce):
            return self.s.receive(f'HELLO|1|{TOKEN}|world1-test|{SECTOR}|bubble|{slot}|{nonce*32}'.encode(),
                                  address, 0)[0].decode().split('|')[3]
        self.a, self.b = join('p1', P1, '1'), join('p2', P2, '2')
        b = Baseline.create(self.a, 1, SECTOR, [row(1), row(2)])
        for i in range(4):
            self.s.receive(b.packet(self.a, i), P1, .05)
        self.s.receive(f'UINTEREST|1|{self.b}|1|{SECTOR}'.encode(), P2, .05)

    def intent(self, seq=1, kind='P', ref=2, mask=1, hint=1, entity=1, gen=1, address=P1, now=.1, lease=None,
               sector=SECTOR):
        lease = lease or self.a
        self.s.receive(f'NINTENT|1|{lease}|{self.a}|{entity}|{gen}|{seq}|{sector}|{kind}|{ref}|{mask}|{hint}'.encode(),
                       address, now)

    def out(self, now):
        return [p.decode().split('|') for a, p in self.s.snapshots(now) if a == P2 and p.startswith(b'NINTENT|')]

    def test_new_record_is_forwarded_once_then_refreshed_while_firing(self):
        self.intent()
        first = self.out(.15)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0][2:], [self.b, self.a, '1', '1', '1', SECTOR, 'P', '2', '1', '1'])
        self.assertEqual(self.out(.3), [])                  # no duplicate before the refresh period
        self.assertEqual(len(self.out(.66)), 1)             # refresh covers a lost datagram
        self.assertEqual(self.out(5), [])                   # stale host state is not repeated forever

    def test_idle_record_is_forwarded_once_and_not_refreshed(self):
        self.intent(kind='0', ref=0, mask=0, hint=0)
        self.assertEqual(len(self.out(.15)), 1)
        self.assertEqual(self.out(.7), [])

    def test_invalid_foreign_stale_and_unknown_records_are_dropped(self):
        for kw in (dict(address=P2, lease=None), dict(address=FOREIGN), dict(entity=99), dict(kind='P', ref=3),
                   dict(kind='N', ref=99), dict(kind='X', ref=1), dict(mask=8), dict(hint=3), dict(sector=OTHER),
                   dict(seq=0)):
            with self.subTest(kw=kw):
                if kw.get('address') == P2:
                    kw['lease'] = self.b
                self.intent(**kw)
                self.assertEqual(self.out(.15), [])
        self.intent(seq=5, mask=1); self.out(.15)
        self.intent(seq=4, mask=0); self.assertEqual(self.out(.2), [])  # reordered older record
        self.intent(seq=1, gen=2, mask=0)                   # a new tracking generation restarts seq
        self.assertEqual(self.out(.25)[0][10], '0')

    def test_npc_target_must_be_a_committed_id_and_other_sector_interest_suppresses(self):
        self.intent(kind='N', ref=2)
        self.assertEqual(self.out(.15)[0][8:10], ['N', '2'])
        self.s.receive(f'UINTEREST|1|{self.b}|2|{OTHER}'.encode(), P2, .2)
        self.intent(seq=2, mask=0)
        self.assertEqual(self.out(.3), [])

    def test_sessions_without_intents_are_unchanged(self):
        self.assertEqual(self.out(.15), [])
        self.assertFalse(self.s.npc_intent.latest)


INTENT_SETUP = 'X4LAN_CONFIG.npc_combat_intent=true'


def nintent(seq, kind='P', ref=2, mask=1, hint=1, entity=1, gen=1):
    return (f"{{'NINTENT','1',client.lease,string.rep('a',32),'{entity}','{gen}','{seq}',"
            f"'cluster_14_sector001_macro','{kind}','{ref}','{mask}','{hint}'}}")


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class IntentAdapterTests(unittest.TestCase):
    make = ua.UniverseAdapterTests.make

    def adapter(self, slot, setup=INTENT_SETUP):
        l = self.make(slot, setup=setup)
        l.execute((UI / 'npc_intent_game.lua').read_text())
        l.execute('''
          N=X4LAN_BUBBLE_API.npcIntent
          function fires() local out={};for _,r in ipairs(requests) do if r[1]=='X4LAN_NpcIntent' and r[2]=='Fire' then out[#out+1]=r[3] end end;return out end
          function intentsSent() local out={};for _,p in ipairs(sent) do if p:find('^NINTENT') then out[#out+1]=p end end;return out end
          function has(text) for _,v in ipairs(logs) do if v:find(text,1,true) then return true end end;return false end
        ''')
        if setup == INTENT_SETUP:
            l.execute("tick(1);events['X4LAN_NpcIntent.Ready']()")
        return l

    def host(self):
        l = self.adapter('p1')
        l.execute("U.receive({'UINTEREST','1',client.lease,'1','cluster_14_sector001_macro'},now);tick(30)")
        return l

    def guest(self):
        l = self.adapter('p2')
        l.execute("baseline(1,{testRow},true);tick(3);assert(U.replicas[1].object==200)")
        return l

    def test_host_tracks_relevant_npcs_and_publishes_on_change_plus_refresh(self):
        l = self.host()
        l.execute('''
          local tracks=0;for _,r in ipairs(requests) do if r[2]=='Track' then tracks=tracks+1 end end
          assert(tracks==2 and N.tracked[1].object==101)
          events['X4LAN_NpcIntent.Sample'](nil,'1:P:2:1:1;2:0:0:0:0;')
          local s=intentsSent();assert(#s==2,#s)
          assert(s[1]:find('|1|1|1|cluster_14_sector001_macro|P|2|1|1$'),s[1])
          events['X4LAN_NpcIntent.Sample'](nil,'1:P:2:1:1;2:0:0:0:0;');assert(#intentsSent()==2)
          now=now+.6;events['X4LAN_NpcIntent.Sample'](nil,'1:P:2:1:1;2:0:0:0:0;');assert(#intentsSent()==3)
          events['X4LAN_NpcIntent.Sample'](nil,'1:P:2:0:1;');assert(#intentsSent()==4)
          events['X4LAN_NpcIntent.Sample'](nil,'9:P:2:1:1;1:P:2:9:1;');assert(#intentsSent()==4)
        ''')

    def test_host_untracks_npcs_that_leave_weapons_range(self):
        l = self.host()
        l.execute('''
          positions[101].x=50000;positions[102].x=50000;tick(60)
          local untracks=0;for _,r in ipairs(requests) do if r[2]=='Untrack' then untracks=untracks+1 end end
          assert(untracks==2 and next(N.tracked)==nil)
        ''')

    def test_host_keeps_tracking_an_npc_that_targets_a_player_beyond_range(self):
        l = self.host()
        l.execute('''
          events['X4LAN_NpcIntent.Sample'](nil,'1:P:2:1:1;')
          positions[101].x=50000;tick(60);assert(N.tracked[1])
        ''')

    def test_guest_fires_replica_and_latch_survives_lost_refresh(self):
        l = self.guest()
        l.execute(f'''
          U.receive({nintent(1)},now);tick(1)
          local f=fires();assert(#f==1 and f[1].guns==true and f[1].turrets==false and tostring(f[1].object)=='200')
          tick(20)  -- 1 s without any refresh: still firing, no stop issued
          f=fires();assert(#f==1 and N.latched==1)
          U.receive({nintent(2)},now);tick(8);assert(#fires()==1)
        ''')

    def test_guest_latch_times_out_and_explicit_stop_wins(self):
        l = self.guest()
        l.execute(f'''
          U.receive({nintent(1)},now);tick(1);tick(32)
          local f=fires();assert(#f==2 and f[2].guns==false,#f)
          assert(N.stats.timeouts==1 or has('timeouts=1'))
          U.receive({nintent(2)},now);tick(1);assert(#fires()==3 and fires()[3].guns==true)
          U.receive({nintent(3, mask=0)},now);tick(1);f=fires();assert(#f==4 and f[4].guns==false)
          U.receive({nintent(2)},now);tick(1);assert(#fires()==4)  -- reordered older record ignored
        ''')

    def test_guest_only_fires_at_players_and_stops_on_pause(self):
        l = self.guest()
        l.execute(f'''
          U.receive({nintent(1, kind='X', ref=0)},now);tick(2);assert(#fires()==0)
          U.receive({nintent(2, kind='P', ref=2, mask=5)},now);tick(1)
          local f=fires();assert(#f==1 and f[1].guns and f[1].turrets)
          paused=true;tick(1);f=fires();assert(#f==2 and not f[2].guns and not f[2].turrets)
        ''')

    def test_guest_replica_faces_its_replicated_player_target(self):
        l = self.guest()
        l.execute(f'''
          tick(5);assert(math.abs(positions[200].yaw)<1e-6)
          U.receive({nintent(1)},now);tick(5)
          -- replica at x=1500, own ship (slot 2 on the guest = object 11) at x=100: face -x.
          assert(math.abs(positions[200].yaw+90)<1e-3,positions[200].yaw)
          assert(math.abs(positions[200].pitch)<1e-3 and positions[200].x==1500)
          tick(40);assert(math.abs(positions[200].yaw)<1e-6)  -- latch expired: host pose again
        ''')

    def test_pitch_convention_is_calibrated_from_host_motion(self):
        l = self.guest()
        l.execute('''
          for i=1,25 do
           U.receive({'UMOVE','1',client.lease,string.rep('a',32),'1',tostring(i+1),'cluster_14_sector001_macro',
            string.format('%.6f',now),'1500','200','300','0','-0.5','0','0','100','100','0','0','0'},now)
          end
          assert(N.pitchSign==-1 and has('PITCH_SIGN -1'))
        ''')

    def test_hits_are_classified_by_authority_and_never_written(self):
        l = self.guest()
        l.execute('''
          events['X4LAN_NpcIntent.Hit'](nil,'2|1|victim|90|10')
          events['X4LAN_NpcIntent.Hit'](nil,'1|0|overwritten|100|50')
          assert(N.stats.hits_victim==1 and N.stats.hits_overwritten==1 and N.stats.hits_tracked==1)
          now=now+2;tick(1);assert(has('hits_victim=1'))
        ''')
        tags = {e.tag for e in E.parse(MD).iter()}
        self.assertFalse(tags & {'set_object_hull', 'set_object_shield', 'set_object_min_hull', 'apply_attackstrength',
                                 'destroy_object', 'create_order', 'set_owner', 'set_faction_relation'})

    def test_flag_off_and_missing_world_sync_are_loud_and_inert(self):
        l = self.adapter('p2', setup='')
        l.execute("assert(N==nil and has('FEATURE npc_combat_intent DISABLED reason=flag_off'))")
        l.execute("baseline(1,{testRow},true);tick(3)")
        l.execute(f"assert(U.receive({nintent(1)},now)==false)")  # an old guest ignores the record

    def test_guest_reports_host_without_feature(self):
        l = self.guest()
        l.execute('''
          for i=1,65 do now=now+1;frames.onUpdate() end
          assert(has('FEATURE npc_combat_intent DISABLED reason=no_host_intents'))
        ''')
        l.execute(f"U.receive({nintent(1)},now);tick(1);assert(has('FEATURE_RESUMED npc_combat_intent'))")

    def test_client_without_feature_drops_nintent_packets(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute((UI / 'client.lua').read_text())
        lua.execute('''
          local lease=string.rep('b',32)
          c=X4LAN_CLIENT.new({token=string.rep('a',32),slot='p2',build='world1-test',sector='s',bubble='b'},
            {send=function() end},string.rep('c',32))
          c:receive('WELCOME|1|'..string.rep('c',32)..'|'..lease..'|20',1)
          c.receiveExtension=function() return false end  -- extension chain without npc_intent
          local before=c.tick
          c:receive('NINTENT|1|'..lease..'|'..string.rep('a',32)..'|1|1|1|cluster_14_sector001_macro|P|2|1|1',2)
          assert(c.tick==before and c.remote==nil and #c.samples==0)
        ''')


if __name__ == '__main__':
    unittest.main()
