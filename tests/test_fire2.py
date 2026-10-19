"""FIRE2 relay validation and LuaJIT playback doubles; native two-PC visual checks remain required."""
import unittest
from pathlib import Path
from x4lan.coop import CoopSession
from test_host import TOKEN, P1, P2, FOREIGN, hello
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

UI = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui'


class Fire2RelayTests(unittest.TestCase):
    def setUp(self):
        self.s = CoopSession(TOKEN, 'test', 'bubble')
        self.a = self.s.receive(hello(), P1, 0)[0].decode().split('|')[3]
        self.b = self.s.receive(hello('p2', '2' * 32), P2, 0)[0].decode().split('|')[3]

    def fire(self, seq=1, time='10', groups=('1', '1'), shots=('0', '0'), epoch=1, entity=1, address=P1, lease=None, now=.2):
        lease = lease or self.a
        packet = f'FIRE2|1|{lease}|{entity}|{epoch}|{seq}|{time}|{groups[0]}|{groups[1]}|{shots[0]}|{shots[1]}'
        return self.s.receive(packet.encode(), address, now)

    def forwarded(self, now=.3, to=P2):
        return [p.decode().split('|') for a, p in self.s.snapshots(now) if a == to and p.startswith(b'FIRE2|')]

    def test_owner_frame_is_forwarded_with_source_lease_and_fields(self):
        self.fire(shots=('3', '1'))
        rows = self.forwarded()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], self.a)
        self.assertEqual(rows[0][4:], ['1', '1', '1', '10', '1', '1', '3', '1'])
        self.assertEqual(self.forwarded(to=P1), [])

    def test_foreign_wrong_owner_epoch_and_bad_fields_are_dropped(self):
        self.fire(address=FOREIGN)
        self.fire(entity=2)
        self.fire(epoch=2)
        for bad in (dict(groups=('0', '1')), dict(groups=('1', '5')), dict(time='nan'), dict(time='-1'),
                    dict(shots=('-1', '0')), dict(shots=('x', '0'))):
            self.fire(**bad)
        self.assertFalse(self.s.fire2)

    def test_sequence_time_and_counters_never_go_backwards(self):
        self.fire(seq=2, time='10', shots=('4', '4'))
        old = dict(self.s.fire2[1])
        for bad in (dict(seq=2, time='11', shots=('5', '4')), dict(seq=3, time='10', shots=('5', '4')),
                    dict(seq=3, time='11', shots=('3', '4'))):
            self.fire(**bad)
            self.assertEqual(self.s.fire2[1], old)
        self.fire(seq=3, time='11', shots=('5', '4'))
        self.assertEqual(self.s.fire2[1]['seq'], 3)

    def test_stale_or_superseded_epoch_is_not_forwarded(self):
        self.fire()
        self.assertEqual(len(self.forwarded(.3)), 1)
        self.assertEqual(self.forwarded(1.0), [])
        self.fire(seq=2, time='11', now=1.1)
        self.s.epochs[1] = 2
        self.assertEqual(self.forwarded(1.2), [])


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class Fire2AdapterTests(unittest.TestCase):
    def make(self, groups_api=True):
        l = LuaRuntime(unpack_returned_tuples=True)
        l.execute(MOCK + EXTRA)
        l.globals().X4LAN_CONFIG.slot = 'p1'
        l.globals().X4LAN_CONFIG.coop = True
        l.execute('noGroups=%s' % ('false' if groups_api else 'true'))
        l.execute('''
         sectors={[11]=22,[42]=22};ConvertIDTo64Bit=function(v) return tonumber(v) end
         api.GetContextByClass=function(object) return sectors[object] or 22 end
         GetComponentData=function() return 'cluster_14_sector001_macro' end
         api.SetObjectSectorPos=function(object,sector,p) positions[object]=p end
         groups={[11]={1,1},[42]={1,1}};groupWrites={}
         api.GetDefensibleActiveWeaponGroup=function(ship,primary) return groups[ship][primary and 1 or 2] end
         api.SetDefensibleActiveWeaponGroup=function(ship,primary,g) groups[ship][primary and 1 or 2]=g;groupWrites[#groupWrites+1]={ship,primary,g} end
         if noGroups then api.GetDefensibleActiveWeaponGroup=nil;api.SetDefensibleActiveWeaponGroup=nil end
        ''')
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'life_game.lua'):
            l.execute((UI / file).read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         C=X4LAN_BUBBLE_API.coop
         function auth(rev,e2) client:receiveExtension({'AUTH','1',client.lease,tostring(rev),'p1','1','p1','p2',tostring(e2 or 1),'p2'},now) end
         function nav2(seq,time,fire,epoch)
          client:receiveExtension({'NAV2','1',client.lease,string.rep('b',32),'2',tostring(epoch or 1),tostring(seq),'cluster_14_sector001_macro',
           '1000','10000','0','0','0','0','0',tostring(fire or 0),tostring(time),'0','0','0','0','0','0'},now)
         end
         function fire2(seq,time,pg,sg,ps,ss,epoch)
          client:receiveExtension({'FIRE2','1',client.lease,string.rep('b',32),'2',tostring(epoch or 1),tostring(seq),tostring(time),
           tostring(pg),tostring(sg),tostring(ps),tostring(ss)},now)
         end
         function fires(name) local out={};for _,r in ipairs(requests) do if r[2]==name then out[#out+1]=r[3] end end;return out end
         auth(1)
        ''')
        return l

    def test_sender_classifies_triggers_and_never_encodes_unknown_group(self):
        l = self.make()
        l.execute('''
         events['X4LAN_Coop.Shot'](nil,'1|p');events['X4LAN_Coop.Shot'](nil,'1|s');events['X4LAN_Coop.Shot'](nil,'1|s');events['X4LAN_Coop.Shot'](nil,1)
         groups[11]={2,3};now=11;frames.onUpdate()
         local f2
         for _,p in ipairs(sent) do if p:find('^FIRE2|') then f2=p end end
         local f={};for v in f2:gmatch('[^|]+') do f[#f+1]=v end
         assert(#f==11 and f[4]=='1' and f[5]=='1' and f[8]=='2' and f[9]=='3' and f[10]=='2' and f[11]=='2')
         assert(C.shots[1]==4)
         local n=#sent;groups[11]={0,1};now=12;frames.onUpdate()
         for i=n+1,#sent do assert(not sent[i]:find('^FIRE2|'),'group 0 must not be encoded') end
        ''')

    def test_first_frame_is_baseline_and_ends_nav_fire_without_replay(self):
        l = self.make()
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);nav2(2,50.05,3);frames.onUpdate();assert(C.remote[2].firing)
         fire2(1,50.05,1,1,7,2);frames.onUpdate()
         assert(not C.remote[2].firing and #fires('Fire2')==0,'baseline must not replay counters')
         local offs=#fires('Fire')
         nav2(3,50.1,9);now=10.2;frames.onUpdate();assert(#fires('Fire')==offs and not C.remote[2].firing,'NAV counter disabled after FIRE2')
        ''')

    def test_trigger_edges_play_at_nav2_source_time_and_apply_groups(self):
        l = self.make()
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);fire2(1,50,1,1,0,0)
         now=10.05;nav2(2,50.05,0);fire2(2,50.05,2,1,1,0)
         frames.onUpdate();assert(#fires('Fire2')==0,'edge waits for the motion playback delay')
         now=10.21;frames.onUpdate()
         local f=fires('Fire2');assert(#f==1 and f[1].primary==true and f[1].secondary==false)
         assert(groups[42][1]==2 and #groupWrites==1)
         now=10.6;fire2(3,50.6,2,1,1,0);frames.onUpdate()
         f=fires('Fire2');assert(f[#f].primary==false,'trigger released after its deadline')
        ''')

    def test_stale_edges_and_silence_stop_fire_even_while_motion_freezes(self):
        l = self.make()
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);fire2(1,50,1,1,0,0);now=10.05;fire2(2,50.05,1,1,0,1);now=10.3;frames.onUpdate()
         local f=fires('Fire2');assert(#f==1 and f[1].secondary==true)
         now=10.75;frames.onUpdate();f=fires('Fire2');assert(f[#f].secondary==false,'silence stops fire')
         -- A delayed old edge outside the motion window is dropped, never fired late.
         local q=C.fire2[2];q.at=now;q.events={{time=40,trigger=1,group=1}};frames.onUpdate()
         assert(#q.events==0 and q.deadline[1]==0)
        ''')

    def test_nav_ahead_of_delayed_baseline_never_shows_a_shot_twice(self):
        l = self.make()
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);now=10.05;nav2(2,50.05,1);frames.onUpdate()
         assert(#fires('Fire')==1 and fires('Fire')[1].on==true,'NAV showed the one shot')
         fire2(1,50,1,1,0,0);fire2(2,50.05,1,1,1,0)
         now=10.21;frames.onUpdate();now=10.4;frames.onUpdate()
         assert(#fires('Fire2')==0,'the shot NAV displayed must not replay through FIRE2')
         now=10.45;nav2(3,50.45,2);fire2(3,50.45,1,1,2,0);now=10.65;frames.onUpdate()
         local f=fires('Fire2');assert(#f==1 and f[1].primary==true,'the next real shot still plays once')
        ''')

    def test_baseline_ahead_of_nav_shows_the_shot_once_through_fire2(self):
        l = self.make()
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);fire2(1,50,1,1,0,0);now=10.05;fire2(2,50.05,1,1,1,0);nav2(2,50.05,1)
         now=10.21;frames.onUpdate()
         local on=0;for _,v in ipairs(fires('Fire')) do if v.on then on=on+1 end end
         assert(on==0 and #fires('Fire2')==1 and fires('Fire2')[1].primary==true)
        ''')

    def test_pause_drops_queued_active_and_paused_history(self):
        l = self.make()
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);fire2(1,50,1,1,0,0)
         -- queued edge, then pause: cleared, never resumes
         now=10.05;fire2(2,50.05,1,1,1,0);paused=true;now=10.12;frames.onUpdate()
         assert(#C.fire2[2].events==0)
         paused=false;now=10.21;frames.onUpdate();assert(#fires('Fire2')==0,'pre-pause edge resumed')
         -- active fire, then pause: stopped
         now=10.3;fire2(3,50.3,1,1,2,0);now=10.5;frames.onUpdate();assert(fires('Fire2')[1].primary==true)
         paused=true;now=10.55;frames.onUpdate();local f=fires('Fire2');assert(f[#f].primary==false,'pause stops fire')
         -- counters received while paused are adopted, not queued
         now=10.6;fire2(4,50.6,1,1,5,0);assert(#C.fire2[2].events==0 and C.fire2[2].shown==5)
         paused=false;now=10.65;fire2(5,50.65,1,1,5,0);now=10.9;frames.onUpdate()
         assert(#fires('Fire2')==#f,'no fire from paused history')
         now=10.95;fire2(6,50.95,1,1,6,0);now=11.15;frames.onUpdate()
         f=fires('Fire2');assert(f[#f].primary==true,'fresh post-resume shot plays')
        ''')

    def test_missing_group_api_ignores_fire2_and_keeps_nav_fallback(self):
        l = self.make(groups_api=False)
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);fire2(1,50,1,1,0,0);assert(next(C.fire2)==nil,'no epoch latched')
         now=10.05;nav2(2,50.05,1);frames.onUpdate();assert(C.remote[2].firing,'NAV fallback active')
         local n=#sent;now=11;frames.onUpdate()
         for i=n+1,#sent do assert(not sent[i]:find('^FIRE2|')) end
        ''')

    def test_epoch_change_clears_fire2_and_restores_nav_fallback(self):
        l = self.make()
        l.execute('''
         C.sectors.cluster_14_sector001_macro=22
         nav2(1,50,0);fire2(1,50,1,1,0,0);now=10.05;fire2(2,50.05,1,1,1,0);now=10.3;frames.onUpdate()
         assert(#fires('Fire2')==1)
         auth(2,2);assert(C.fire2[2]==nil)
         local f=fires('Fire2');assert(f[#f].primary==false,'epoch change stops FIRE2 fire')
         nav2(1,60,0,2);nav2(2,60.05,1,2);frames.onUpdate();assert(C.remote[2].firing,'NAV fallback again for the new epoch')
        ''')


if __name__ == '__main__':
    unittest.main()
