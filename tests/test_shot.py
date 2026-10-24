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
DIGEST = 'weapon_ter_s_plasma_01_mk1_macro:40.000'


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
        self.s.receive(f'HACK|1|{self.b}|2|{self.g2}'.encode(), P2, .13)

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
        self.s.health_live[2] = 0  # fixture: baseline still unacknowledged
        self.shot(shooter=1, victim=2, address=P1)
        self.assertEqual((self.rows(to=P1), self.rows(to=P2)), ([], []))   # not live: host is authority = shooter
        self.s.receive(f'HACK|1|{self.b}|2|{self.g2}'.encode(), P2, .25)
        self.assertEqual(self.rows(now=.26, to=P2), [])  # native hits never become new claims
        self.shot(shooter=1, victim=2, seq=2, hits=1, address=P1, now=.3)
        self.assertEqual(self.rows(now=.35, to=P2)[0][4:], ['1', str(self.g1), '2', str(self.g2), '2', '1', DIGEST])
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

    def test_relay_rejects_changed_digest_within_a_counter_generation(self):
        self.shot(seq=1,hits=3)
        self.shot(seq=2,hits=4,digest='weapon_gen_s_laser_01_mk2_macro:46.000')
        self.assertEqual(self.rows()[0][9:], ['3', DIGEST])

    def test_guest_claims_need_an_acknowledged_health_baseline(self):
        self.s.health_live[2]=0
        self.shot();self.assertEqual(self.rows(),[])
        self.s.receive(f'HACK|1|{self.b}|2|{self.g2}'.encode(),P2,.25)
        self.shot(seq=2,hits=1,now=.26)
        self.assertEqual(self.rows()[0][9],'1')


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ShotAdapterTests(unittest.TestCase):
    def make(self, slot, entries=None):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA)
        cfg = l.globals().X4LAN_CONFIG
        cfg.slot, cfg.coop, cfg.pilot_health, cfg.shot_claims = slot, True, True, True
        l.execute('''
         sectors={};ConvertIDTo64Bit=function(v) return tonumber(v) end;ConvertStringTo64Bit=function(v) return v end
         GetComponentData=function() return 'cluster_14_sector001_macro' end
         mods={};entries={weapon_ter_s_plasma_01_mk1_macro={hullshielddpshot=40},weapon_gen_s_laser_01_mk1_macro={hullshielddpshot=30,hullonlydpshot=5}}
         for _,entry in pairs(entries) do
          entry.isbeamweapon=0
          for _,k in ipairs({'shieldonlydpshot','hullnoshielddpshot','hullshieldareadpshot','shieldonlyareadpshot','hullonlyareadpshot','hullnoshieldareadpshot'}) do entry[k]=0 end
          entry.hullonlydpshot=entry.hullonlydpshot or 0
         end
         GetMacroData=function(m,k) return k=='infolibrary' and 'weapons' or nil end
         GetLibraryEntry=function(lib,m) return entries[m] end
         local operational=api.IsComponentOperational
         api.IsComponentOperational=function(id) return id and id>=0x11 and id<=0x41 or operational(id) end
         modQueries={};api.GetInstalledWeaponMod=function(id) assert(type(id)=='number' and id>0);modQueries[#modQueries+1]=id;return mods[id] or false end
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
         events['X4LAN_Shot.StateReady'](nil,tostring(X4LAN_BUBBLE_API.shot.stateRevision))
         if X4LAN_CONFIG.slot=='p2' then
          client:receiveExtension({'HBASE','1',client.lease,'2','4','100','100'},now)
          events['X4LAN_Health.Based'](nil,'2|4')
         end
         P='weapon_ter_s_plasma_01_mk1_macro'
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
         mods[0x31]=true;loadout(1,'0x31,'..P..';');local modded=support(1)
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
         events['X4LAN_Shot.Applied'](nil,'5|4|3|3|'..X4LAN_BUBBLE_API.shot.stateRevision)
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

    def test_pending_hits_cannot_be_reinterpreted_after_loadout_change(self):
        out=self.make('p1').execute("""
         loadout(2,'0x11,'..P..';');shot(1,3)
         entries[P].hullshielddpshot=80;loadout(2,'0x11,'..P..';');tick(10.15)
         entries[P].hullshielddpshot=40;loadout(2,'0x11,'..P..';');shot(2,9);tick(10.2)
         local stopped=0;for _,q in ipairs(requests) do if q[1]=='X4LAN_Shot' and q[2]=='Stop' then stopped=stopped+1 end end
         return X4LAN_BUBBLE_API.shot.failed,#applies(),support(2),stopped""")
        self.assertEqual(tuple(out),(True,0,0,1))

    def test_stock_gate_refuses_beams_missing_channels_and_unknown_macros(self):
        for mutation in ("entries[P].isbeamweapon=1", "entries[P].isbeamweapon=nil", "entries[P].isbeamweapon=false", "entries[P].hullonlyareadpshot=nil",
                         "entries.unknown_weapon_macro=entries[P];P='unknown_weapon_macro'"):
            with self.subTest(mutation=mutation):
                l=self.make('p1');l.execute(mutation)
                self.assertEqual(l.execute("loadout(2,'0x11,'..P..';');return support(2)"),0)

    def test_native_hex_ids_use_id_conversion_and_invalid_objects_refuse(self):
        out=self.make('p1').execute("""
         ConvertStringTo64Bit=function() error('decimal conversion cannot read MD hexadecimal IDs') end
         loadout(2,'0x11,'..P..';');return support(2),modQueries[1]
        """)
        self.assertEqual(tuple(out),(40,17))
        for change in ("ConvertIDTo64Bit=function() return 0 end",
                       "api.IsComponentOperational=function() return false end",
                       "api.GetInstalledWeaponMod=function() error('unavailable') end"):
            with self.subTest(change=change):
                lua=self.make('p1');lua.execute(change)
                self.assertEqual(lua.execute("loadout(2,'0x11,'..P..';');return support(2),#modQueries"),(0,0))

    def test_apply_waits_for_matching_native_state_ack(self):
        out=self.make('p1').execute("""
         local S=X4LAN_BUBBLE_API.shot;S.stateAck=nil
         loadout(2,'0x11,'..P..';');shot(1,3);tick(10.15);local before=#applies()
         events['X4LAN_Shot.StateReady'](nil,tostring(S.stateRevision-1));tick(10.2);local old=#applies()
         events['X4LAN_Shot.StateReady'](nil,tostring(S.stateRevision));tick(10.25)
         return before,old,#applies(),applies()[1].state==S.stateRevision""")
        self.assertEqual(tuple(out),(0,0,1,True))

    def test_applied_ack_requires_current_state_and_known_count(self):
        out=self.make('p1').execute("""
         local S=X4LAN_BUBBLE_API.shot;loadout(2,'0x11,'..P..';');shot(1,3);tick(10.15)
         local key='2|4|1|3'
         events['X4LAN_Shot.Applied'](nil,'5|4|3|3|'..(S.stateRevision-1));local stale=S.confirmed[key] or 0
         events['X4LAN_Shot.Applied'](nil,'5|4|3|99|'..S.stateRevision);local extra=S.confirmed[key] or 0
         events['X4LAN_Shot.Applied'](nil,'5|4|3|3|'..S.stateRevision)
         return stale,extra,S.confirmed[key],S.inflight[key]==nil""")
        self.assertEqual(tuple(out),(0,0,3,True))

    def test_native_authority_hits_never_enter_a_later_claim_counter(self):
        out=self.make('p1').execute("""
         local S,H=X4LAN_BUBBLE_API.shot,X4LAN_BUBBLE_API.health
         loadout(1,'0x11,'..P..';');local native=S.hit(2,3)
         H.remote[2]={gen=4,at=now,hull=100,shield=100}
         local remote=S.hit(2,1)
         return native,remote,S.totals['1|3|2|4']""")
        self.assertEqual(tuple(out),(False,True,1))

    def test_locally_closed_health_generation_drops_claims_before_next_hgen(self):
        out=self.make('p1').execute("""
         local S,H=X4LAN_BUBBLE_API.shot,X4LAN_BUBBLE_API.health
         loadout(2,'0x11,'..P..';');shot(1,3);H.close(2,4);tick(10.15)
         shot(2,9);tick(10.2)
         return #applies(),next(S.pending)==nil,H.valid[2],H.closed[2]""")
        self.assertEqual(tuple(out),(0,True,4,4))


def shot_cue(name):
    found = [c for c in E.parse(MD).iter('cue') if c.get('name') == name]
    assert len(found) == 1, name
    return found[0].find('actions')


class ShotFenceTests(unittest.TestCase):
    P = dict(timewarp=1, wall='2026-10-24T03:00:00', age=50.2, operational=True)
    W = dict(state=1,stamp='2026-10-24T03:00:00', deadline=51.0, strength=40, shooter=2, victim=1, **{'from': 50.0})

    def state(self):
        return dict(shotSGen={k: 0 for k in range(1, 9)}, shotVGen={k: 0 for k in range(1, 9)},
                    shotCount={k: 0 for k in range(1, 9)}, shotStrength={1: 40, 2: 40}, dead={1: False, 2: False},
                    shotApplying=False, shotFailed=False, shotState=1, shotStateAt=50.1, shotCurrentGen={1:3,2:4}, shotAuthority={1:True,2:True},
                    Closed={1:0,2:0}, Live={1:False,2:False}, Own={1:False,2:False}, Auth={1:0,2:0})

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
        self.assertFalse(S['shotApplying'])

    def test_generations_only_move_forward_and_restart_from_zero(self):
        S = self.state(); self.apply(S, sgen=4, vgen=3, base=0, count=5)
        self.assertEqual(self.apply(S, sgen=3, vgen=3, base=0, count=2), 0)      # older shooter generation
        S['shotCurrentGen'][2] = 5
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

    def test_closed_generation_and_current_native_state_revoke_queued_apply(self):
        for change in (lambda S:S['Closed'].__setitem__(2,4),lambda S:S['Closed'].__setitem__(1,3),
                       lambda S:S['shotCurrentGen'].__setitem__(2,5),lambda S:S.__setitem__('shotState',2),
                       lambda S:S.__setitem__('shotStateAt',49)):
            S=self.state();change(S)
            self.assertEqual(self.apply(S,sgen=4,vgen=3,base=0,count=3),0)
            self.assertEqual(S['shotCount'][5],0)

    def test_native_health_authority_is_rechecked_at_execution(self):
        S=self.state();S['Live'][1]=True
        self.assertEqual(self.apply(S,sgen=4,vgen=3,base=0,count=3),0)
        S=self.state();P=dict(self.P,host=False)
        self.assertEqual(self.apply(S,P,sgen=4,vgen=3,base=0,count=3),0)
        S['Own'][1]=True;S['Auth'][1]=2
        self.assertEqual(self.apply(S,P,sgen=4,vgen=3,base=0,count=3),0)
        S['Auth'][1]=3
        self.assertEqual(self.apply(S,P,sgen=4,vgen=3,base=0,count=3),3)

    def test_lost_applied_callback_recovers_from_duplicate_without_more_damage(self):
        S=self.state();self.assertEqual(self.apply(S,sgen=4,vgen=3,base=0,count=3),3)
        effects=run(shot_cue('Apply'),S,dict(self.W,sgen=4,vgen=3,base=0,count=3),dict(self.P),None)
        self.assertEqual(sum(t=='apply_attackstrength' for t,_ in effects),0)
        self.assertEqual(sum(t=='raise_lua_event' for t,_ in effects),1)
        self.assertEqual(S['shotCount'][5],3)

    def test_terminal_stop_cannot_be_rearmed_by_old_support_or_state(self):
        S=self.state();run(shot_cue('Stop'),S,{},dict(self.P),None)
        run(shot_cue('Support'),S,dict(entity=2,strength=40),dict(self.P),None)
        state=dict(self.W,revision=2,g1=3,g2=4,a1=True,a2=True)
        effects=run(shot_cue('State'),S,state,dict(self.P),None)
        self.assertFalse(any(t=='raise_lua_event' for t,_ in effects))
        self.assertTrue(S['shotFailed']);self.assertEqual(S['shotState'],1)
        self.assertEqual(self.apply(S,sgen=4,vgen=3,base=0,count=3),0)

    def test_new_state_rejects_old_queued_request_and_stale_state(self):
        S=self.state();new=dict(self.W,revision=2,g1=3,g2=5,a1=True,a2=True)
        run(shot_cue('State'),S,new,dict(self.P),None)
        run(shot_cue('State'),S,dict(new,revision=1,g2=4),dict(self.P),None)
        self.assertEqual((S['shotState'],S['shotCurrentGen'][2]),(2,5))
        self.assertEqual(self.apply(S,sgen=4,vgen=3,base=0,count=3),0)
        self.assertEqual(self.apply(S,state=2,sgen=5,vgen=3,base=0,count=2),2)


if __name__ == '__main__':
    unittest.main()
