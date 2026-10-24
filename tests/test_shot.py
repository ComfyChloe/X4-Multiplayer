"""SHOT relay, LuaJIT doubles and MD fence interpreter for shooter-confirmed hit counts (inert, dev)."""
import unittest
from pathlib import Path
from x4lan.coop import CoopSession
from test_host import TOKEN, P1, P2, FOREIGN, hello
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA
from test_health_md import run, cue as health_cue, E

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'
MD = ROOT / 'extension/x4_lan_lab/md/x4lan_shot.xml'
WORLD = [100, 100, 0, 64.5, 30, 0, 100, 100, 100, 100, 200, 1, 1, 0, 0, 0, 0]
DIGEST = 'weapon_gen_s_plasma_01_mk1_macro:40.000'


class ShotRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = CoopSession(TOKEN, 'test', 'bubble')
        self.a = self.s.receive(hello(), P1, 0)[0].decode().split('|')[3]
        self.b = self.s.receive(hello('p2', '2' * 32), P2, 0)[0].decode().split('|')[3]
        self.s.receive(f'WORLD|1|{self.a}|1|{"|".join(map(str, WORLD))}'.encode(), P1, .05)
        self.s.receive(f'CREW|1|{self.a}|1|1|1'.encode(), P1, .08)      # host pilots ship 1
        self.s.receive(f'CREW|1|{self.b}|1|2|2'.encode(), P2, .1)       # guest pilots ship 2
        self.s.snapshots(.12)
        self.g1, self.g2 = self.s.health_gen[1], self.s.health_gen[2]

    def shot(self, shooter=2, victim=1, seq=1, hits=3, sgen=None, vgen=None, address=P2, lease=None, digest=DIGEST, now=.2):
        sgen = self.s.health_gen[shooter] if sgen is None else sgen
        vgen = (self.s.health_gen[victim] if victim <= 2 else 0) if vgen is None else vgen
        lease = lease or (self.b if address == P2 else self.a)
        return self.s.receive(f'SHOT|1|{lease}|{shooter}|{sgen}|{victim}|{vgen}|{seq}|{hits}|{digest}'.encode(), address, now)

    def rows(self, now=.3, to=P1):
        return [p.decode().split('|') for a, p in self.s.snapshots(now) if a == to and p.startswith(b'SHOT|')]

    def test_guest_hits_on_the_host_ship_and_a_target_reach_only_the_host(self):
        self.shot(); self.shot(victim=3, seq=2, hits=1)
        rows = self.rows()
        self.assertEqual(sorted(r[6] for r in rows), ['1', '3'])
        self.assertEqual(rows[0][2:4], [self.a, self.b])
        self.assertEqual(self.rows(to=P2), [])

    def test_host_hits_on_the_guest_ship_go_to_its_live_pilot_and_never_back_to_the_host(self):
        self.shot(shooter=1, victim=2, address=P1)
        self.assertEqual((self.rows(to=P1), self.rows(to=P2)), ([], []))   # not live: host is authority = shooter
        self.s.receive(f'HACK|1|{self.b}|2|{self.g2}'.encode(), P2, .25)
        self.shot(shooter=1, victim=2, seq=2, hits=4, address=P1, now=.3)
        self.assertEqual(self.rows(now=.35, to=P2)[0][4:], ['1', str(self.g1), '2', str(self.g2), '2', '4', DIGEST])
        self.assertEqual(self.rows(now=.35, to=P1), [])

    def test_only_the_owning_pilot_current_generations_and_valid_fields(self):
        for bad in (dict(shooter=1), dict(address=FOREIGN), dict(sgen=99), dict(vgen=99), dict(victim=2),
                    dict(victim=5), dict(hits=-1), dict(hits=2000000), dict(digest='Bad Digest'), dict(digest='x:40')):
            self.shot(**bad)
        self.assertEqual(self.rows(), [])

    def test_order_duplicates_and_lower_counts_are_refused(self):
        self.shot(seq=3, hits=5); self.shot(seq=3, hits=9); self.shot(seq=2, hits=9); self.shot(seq=4, hits=4)
        self.assertEqual(self.rows()[0][9], '5')
        self.shot(seq=5, hits=8)
        self.assertEqual(self.rows()[0][9], '8')

    def test_a_handover_ends_the_counts_of_the_old_generation(self):
        self.shot()
        self.s.receive(f'CREW|1|{self.b}|2|0|0'.encode(), P2, .25)      # the guest leaves its ship
        self.assertEqual(self.rows(now=.3), [])
        self.assertEqual(self.s.shots, {})


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ShotAdapterTests(unittest.TestCase):
    def make(self, slot, entries=None):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        cfg = l.globals().X4LAN_CONFIG
        cfg.slot, cfg.coop, cfg.pilot_health, cfg.shot_claims = slot, True, True, True
        l.execute('''
         sectors={};ConvertIDTo64Bit=function(v) return tonumber(v) end;ConvertStringTo64Bit=function(v) return v end
         GetComponentData=function() return 'cluster_14_sector001_macro' end
         mods={};entries={weapon_gen_s_plasma_01_mk1_macro={hullshielddpshot=40},weapon_gen_s_laser_01_mk1_macro={hullshielddpshot=30,hullonlydpshot=5}}
         GetMacroData=function(m,k) return k=='infolibrary' and 'weapons' or nil end
         GetLibraryEntry=function(lib,m) return entries[m] end
         api.GetInstalledWeaponMod=function(id) return mods[id] or false end
        ''')
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'health_game.lua', 'shot_game.lua'):
            l.execute((UI / file).read_text())
        l.execute('''events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
         function auth(rev,p2) client:receiveExtension({'AUTH','1',client.lease,tostring(rev),'p1','1','p1','p2','1',p2 or 'p2'},now) end
         auth(1)
         now=10.1;frames.onUpdate();events['X4LAN_Health.Ready']();frames.onUpdate();events['X4LAN_Shot.Ready']()
         client:receiveExtension({'HGEN','1',client.lease,'1','3','4'},now)
         function clock(age) events['X4LAN_Health.Own'](nil,age..'|2026-10-24T03:00:00|0|0|0|0') end
         clock('50');frames.onUpdate()
         P='weapon_gen_s_plasma_01_mk1_macro'
         function loadout(e,list,turrets) events['X4LAN_Shot.Loadout'](nil,e..'|'..(turrets or '0')..'|'..list) end
         function shot(seq,hits,digest) client:receiveExtension({'SHOT','1',client.lease,string.rep('c',32),'2','4','1','3',tostring(seq),tostring(hits),digest or P..':40.000'},now) end
         function applies() local out={};for _,q in ipairs(requests) do if q[1]=='X4LAN_Shot' and q[2]=='Apply' then out[#out+1]=q[3] end end;return out end
         function support(e) local r;for _,q in ipairs(requests) do if q[1]=='X4LAN_Shot' and q[2]=='Support' and q[3].entity==e then r=q[3].strength end end;return r end
         function tick(t) now=t;frames.onUpdate() end
        ''')
        return l

    def test_loadout_gate_accepts_only_uniform_unmodified_hullshield_bullets(self):
        out = self.make('p1').execute('''
         loadout(2,'0x11,'..P..';0x12,'..P..';');local ok=support(2)
         loadout(1,'0x21,'..P..';0x22,weapon_gen_s_laser_01_mk1_macro;');local mixed=support(1)
         loadout(1,'0x23,weapon_gen_s_laser_01_mk1_macro;');local channel=support(1)
         mods['0x31']=true;loadout(1,'0x31,'..P..';');local modded=support(1)
         loadout(1,'0x41,'..P..';','1');local turret=support(1)
         loadout(1,'');local empty=support(1)
         return ok,mixed,channel,modded,turret,empty,X4LAN_BUBBLE_API.shot.support[2].digest''')
        self.assertEqual(tuple(out), (40, 0, 0, 0, 0, 0, DIGEST))

    def test_host_applies_increases_with_compare_and_set_and_retries(self):
        out = self.make('p1').execute('''
         loadout(2,'0x11,'..P..';')
         shot(1,3);tick(10.15);local a=applies()[1]
         tick(10.3);local held=#applies()                         -- no confirmation yet, inside RETRY
         clock('50.6');tick(10.7);local retry=applies()[2]        -- re-issued with the same base/count
         events['X4LAN_Shot.Applied'](nil,'5|4|3|3')
         shot(2,30);tick(10.75);local b=applies()[3]
         shot(2,99);shot(3,20);tick(10.8);local reordered=#applies()
         return a.base,a.count,a.strength,a.shooter,a.victim,a.sgen,a.vgen,a.stamp,held,retry.base,retry.count,b.base,b.count,reordered''')
        self.assertEqual(tuple(out), (0, 3, 40, 2, 1, 4, 3, '2026-10-24T03:00:00', 1, 0, 3, 3, 23, 3))

    def test_refusals_digest_unsupported_paused_and_failure(self):
        out = self.make('p1').execute('''
         loadout(2,'0x11,'..P..';')
         shot(1,3,'weapon_gen_s_plasma_01_mk2_macro:46.000');tick(10.15);local digest=#applies()
         X4LAN_BUBBLE_API.paused=function() return true end;shot(2,3);tick(10.2);local paused=#applies()
         X4LAN_BUBBLE_API.paused=function() return false end
         X4LAN_BUBBLE_API.shot.frame=function() error('induced') end;tick(10.25)
         X4LAN_BUBBLE_API.shot.frame=nil
         shot(3,9);tick(10.3)
         return digest,paused,#applies(),X4LAN_BUBBLE_API.shot.failed,support(2)''')
        self.assertEqual(tuple(out), (0, 0, 0, True, 0))

    def test_guest_shooter_sends_cumulative_counts_only_for_supported_current_generations(self):
        out = self.make('p2').execute('''
         local S=X4LAN_BUBBLE_API.shot
         local before=S.hit(1)                                     -- no supported loadout yet
         loadout(2,'0x11,'..P..';')
         S.hit(1);S.hit(1,2);S.hit(3);tick(10.2)
         local out={};for _,p in ipairs(sent) do if p:sub(1,5)=='SHOT|' then out[#out+1]=p end end
         auth(2,'0');tick(10.3);local after=S.hit(1)
         return before,#out,table.concat(out,';'),after''')
        before, count, frames, after = out
        self.assertEqual((before, count, after), (False, 2, False))
        self.assertTrue('|2|4|1|3|' in frames and '|3|' + DIGEST in frames and '|2|4|3|0|' in frames, frames)

    def test_disabled_without_its_flag_or_pilot_health(self):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        cfg = l.globals().X4LAN_CONFIG; cfg.coop, cfg.shot_claims = True, True
        for file in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'health_game.lua', 'shot_game.lua'):
            l.execute((UI / file).read_text())
        self.assertIsNone(l.eval('X4LAN_BUBBLE_API.shot'))
        ui = (ROOT / 'extension/x4_lan_lab/ui.xml').read_text()
        self.assertLess(ui.index('ui/health_game.lua'), ui.index('ui/shot_game.lua'))
        self.assertTrue('shot_claims = false' in (UI / 'config.lua').read_text())
        md = (ROOT / 'extension/x4_lan_lab/md/x4lan_shot.xml').read_text()
        for event in ('event_object_hull_damaged', 'event_object_shield_damaged', 'event_object_attacked'):
            self.assertTrue(event not in md, 'capture must stay unarmed: ' + event)


def shot_cue(name):
    found = [c for c in E.parse(MD).iter('cue') if c.get('name') == name]
    assert len(found) == 1, name
    return found[0].find('actions')


class ShotFenceTests(unittest.TestCase):
    P = dict(timewarp=1, wall='2026-10-24T03:00:00', age=50.2, operational=True)
    W = dict(stamp='2026-10-24T03:00:00', deadline=51.0, strength=40, shooter=2, victim=1, **{'from': 50.0})

    def state(self):
        return dict(shotSGen={k: 0 for k in range(1, 9)}, shotVGen={k: 0 for k in range(1, 9)},
                    shotCount={k: 0 for k in range(1, 9)}, shotStrength={1: 40, 2: 40}, dead={1: False, 2: False})

    def apply(self, S, P=None, **a):
        effects = run(shot_cue('Apply'), S, dict(self.W, **a), dict(P or self.P), None)
        return sum(1 for tag, _ in effects if tag == 'apply_attackstrength')

    def test_compare_and_set_applies_each_hit_exactly_once(self):
        S = self.state()
        self.assertEqual(self.apply(S, sgen=4, vgen=3, base=0, count=3), 3)
        self.assertEqual(self.apply(S, sgen=4, vgen=3, base=0, count=3), 0)      # duplicate / retry after success
        self.assertEqual(self.apply(S, sgen=4, vgen=3, base=2, count=5), 0)      # wrong base
        self.assertEqual(self.apply(S, sgen=4, vgen=3, base=3, count=5), 2)
        self.assertEqual(S['shotCount'][5], 5)

    def test_generations_only_move_forward_and_restart_from_zero(self):
        S = self.state(); self.apply(S, sgen=4, vgen=3, base=0, count=5)
        self.assertEqual(self.apply(S, sgen=3, vgen=3, base=0, count=2), 0)      # older shooter generation
        self.assertEqual(self.apply(S, sgen=5, vgen=3, base=1, count=2), 0)      # newer, but not from 0
        self.assertEqual(self.apply(S, sgen=5, vgen=3, base=0, count=2), 2)
        self.assertEqual((S['shotSGen'][5], S['shotCount'][5]), (5, 2))
        self.assertEqual(self.apply(S, sgen=4, vgen=3, base=0, count=9), 0)      # the old pair never returns

    def test_bounds_strength_window_pause_and_life(self):
        S = self.state()
        refused = [self.apply(S, sgen=4, vgen=3, base=0, count=21),               # more than 20 at once
                   self.apply(S, sgen=4, vgen=3, base=0, count=3, strength=46),   # other loadout strength
                   self.apply(S, sgen=4, vgen=3, base=0, count=3, stamp='2026-10-24T02:59:59'),
                   self.apply(S, sgen=4, vgen=3, base=0, count=3, deadline=50.1),
                   self.apply(S, dict(self.P, timewarp=0), sgen=4, vgen=3, base=0, count=3),
                   self.apply(S, dict(self.P, operational=False), sgen=4, vgen=3, base=0, count=3)]
        S['dead'][1] = True; refused.append(self.apply(S, sgen=4, vgen=3, base=0, count=3)); S['dead'][1] = False
        S['shotStrength'][2] = 0; refused.append(self.apply(S, sgen=4, vgen=3, base=0, count=3))
        self.assertEqual(refused, [0] * 8)
        self.assertEqual(S['shotCount'][5], 0)


if __name__ == '__main__':
    unittest.main()
