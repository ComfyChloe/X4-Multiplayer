"""Opt-in projectile-switch handshake and queued replay safety."""
import unittest
import xml.etree.ElementTree as E
import test_shot
from test_shot import UI, ROOT
import test_fire2
from test_game_adapter import LuaRuntime


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class VisualAdapterTests(unittest.TestCase):
    def make(self):
        l = test_shot.ShotAdapterTests().make('p1')
        l.execute('''
         X4LAN_CONFIG.fire2=true;X4LAN_CONFIG.shot_visual=true
         A=X4LAN_BUBBLE_API;H=A.health;D=A.shot;C=A.coop
         loadout(1,'0x11,'..P..';');loadout(2,'0x12,'..P..';')
         O='bullet_ter_s_plasma_01_mk1_macro';Z='x4lan_visual_plasma_bullet_macro'
         launched={[17]=O,[18]=O};writes={};slots={[11]={17,0},[42]={0,18}}
         api.GetNumUpgradeSlots=function(ship) return #slots[ship] end
         api.GetUpgradeSlotCurrentComponent=function(ship,kind,slot) return slots[ship][slot] end
         api.GetWeaponLaunchedMacro=function(id) return launched[id] end
         api.SetWeaponLaunchedMacro=function(id,macro) writes[#writes+1]={id,macro};launched[id]=macro end
         GetComponentData=function() return P end
        ''')
        l.execute((UI/'shot_visual.lua').read_text())
        l.execute('''
         V=A.shotVisual;V.ready=true;V.frame(now)
         function request(control,e)
          local out;for _,q in ipairs(requests) do
           if q[1]=='X4LAN_ShotVisual' and q[2]==control and q[3].entity==e then out=q[3] end
          end;return out
         end
         function prepared(e)
          local q=request('Prepare',e)
          V.prepared(e..'|'..q.gen..'|'..q.ticket..'|'..q.mode..'|'..C.ships[e])
         end
         function proven(e)
          local q=request('Proof',e)
          V.proven(e..'|'..q.gen..'|'..q.ticket..'|'..q.mode..'|'..q.sequence)
         end
        ''')
        return l

    def test_stop_ack_precedes_setter_and_proof_ack_precedes_replay(self):
        self.make().execute('''
         assert(#writes==0 and not V.readyReplay(2,4,now))
         prepared(2);assert(#writes==1 and launched[18]==Z)
         assert(not V.readyReplay(2,4,now));proven(2);assert(V.readyReplay(2,4,now))
         prepared(1);proven(1);assert(V.readyOriginal(1,3,now) and #writes==1)
        ''')

    def test_delayed_prepare_ack_cannot_change_a_new_owner_or_generation(self):
        for change in ('C.owners[2]="p1"', 'H.valid[2]=5', 'H.closed[2]=4', 'V.now=now+.31'):
            with self.subTest(change=change):
                self.make().execute(change+';prepared(2);assert(#writes==0)')

    def test_return_to_local_ownership_restores_the_same_guns_after_new_stop_ack(self):
        self.make().execute('''
         prepared(2);proven(2);C.owners[2]='p1';H.valid[2]=5
         V.frame(now);assert(launched[18]==Z and not V.readyOriginal(2,5,now))
         prepared(2);assert(launched[18]==O and writes[2][1]==18)
         proven(2);assert(V.readyOriginal(2,5,now))
        ''')

    def test_normal_proxy_death_revokes_only_its_proof_and_allows_new_identity(self):
        self.make().execute('''
         prepared(1);proven(1);prepared(2);proven(2)
         local old=api.IsComponentOperational
         api.IsComponentOperational=function(id) return id~=42 and old(id) end
         assert(not V.readyReplay(2,4,now));V.frame(now)
         assert(not V.failed and not D.failed and V.rows[2]==nil and V.readyOriginal(1,3,now))
         -- A replacement object must get a fresh native Stop/readback proof.
         C.ships[2]=43;slots[43]={0,18};H.valid[2]=5;V.frame(now)
         assert(not V.readyReplay(2,5,now));prepared(2);proven(2)
         assert(V.readyReplay(2,5,now) and not V.failed)
        ''')

    def test_missing_expired_or_wrong_proof_never_authorizes_fire(self):
        self.make().execute('''
         prepared(2);local q=request('Proof',2)
         V.proven('2|4|'..q.ticket..'|2|'..(q.sequence-1));assert(not V.readyReplay(2,4,now))
         proven(2);assert(V.readyReplay(2,4,now))
         assert(not V.readyReplay(2,4,now+.31) and not V.readyReplay(2,5,now))
         D.failed=true;assert(not V.readyReplay(2,4,now))
        ''')

    def test_changed_mod_slot_or_foreign_projectile_fails_before_proof(self):
        for change in ('mods[18]=true', 'slots[42]={18,0}', 'launched[18]="foreign"'):
            with self.subTest(change=change):
                self.make().execute('''prepared(2);proven(2);local seq=V.rows[2].pendingProof;''' + change + '''
                 V.now=now+.11;H.mdAt=V.now
                 local ok,err=pcall(V.frame,V.now);assert(not ok);V.fail(err)
                 assert(V.failed and D.failed and V.rows[2].pendingProof==seq)
                 assert(not V.readyReplay(2,4,now))
                ''')

    def test_fire2_waits_for_proof_drops_waiting_history_and_carries_generation(self):
        l=test_fire2.Fire2AdapterTests().make()
        l.execute('''
         X4LAN_CONFIG.shot_visual=true;A=X4LAN_BUBBLE_API
         A.health={valid={[2]=4}};allowed=false
         A.shotVisual={readyReplay=function(e,g,t) return allowed and e==2 and g==4 end}
         nav2(1,50,0);fire2(1,50,1,1,0,0);now=10.05;fire2(2,50.05,1,1,1,0)
         now=10.3;frames.onUpdate();assert(#fires('Fire2')==0 and #C.fire2[2].events==0)
         allowed=true;nav2(2,50.3,0);fire2(3,50.3,1,1,2,0)
         now=10.55;frames.onUpdate();local f=fires('Fire2');assert(#f==1)
         assert(f[1].visual and f[1].visualGen==4 and f[1].primaryShots==1)
         allowed=false;now=10.56;frames.onUpdate();f=fires('Fire2');assert(#f==2 and not f[2].primary)
        ''')


    def test_observed_projectiles_disable_native_fire2_even_with_trigger_evidence(self):
        l=test_fire2.Fire2AdapterTests().make()
        l.execute("""
         X4LAN_CONFIG.projectile_visuals=true
         nav2(1,50,0);fire2(1,50,1,1,0,0);now=10.05;fire2(2,50.05,1,1,1,0)
         now=10.3;frames.onUpdate()
         for _,f in ipairs(fires('Fire2')) do assert(not f.primary and not f.secondary) end
        """)


class NativeReplayGateTests(unittest.TestCase):
    def test_queued_positive_grant_is_refused_after_mode_gen_closure_or_expiry_change(self):
        tree=E.parse(ROOT/'extension/x4_lan_lab/md/x4lan_coop.xml')
        cue=next(c for c in tree.iter('cue') if c.get('name')=='Fire2')
        guard=cue.find('actions/do_if/do_if')
        expression=guard.get('value')
        replacements={
            '@$arena.$projectileVisualEnabled':'observed', '@$arena.$shotVisualEnabled':'enabled', '@$v.$visual':'requested',
            '$v.$primary':'primary', '$v.$secondary':'secondary', '@$arena.$shotFailed':'failed',
            '@$arena.$shotVisualMode.{$v.$ship}':'mode', '@$v.$visualGen':'gen',
            '$v.$visualGen':'gen', '$arena.$shotVisualGen.{$v.$ship}':'proofgen',
            '$arena.$shotCurrentGen.{$v.$ship}':'currentgen', '$arena.$healthClosed.{$v.$ship}':'closed',
            'player.age - $arena.$shotVisualAt.{$v.$ship}':'proofage',
            'player.age - $arena.$shotStateAt':'stateage', 'player.controlled != $ship':'not piloted',
            ' gt ':' > ', ' le ':' <= ', '0.3s':'.3'}
        for a,b in sorted(replacements.items(), key=lambda item: -len(item[0])): expression=expression.replace(a,b)
        self.assertNotIn('$',expression)
        base=dict(enabled=True,observed=False,requested=True,primary=True,secondary=False,failed=False,
                  mode=2,gen=4,proofgen=4,currentgen=4,closed=3,proofage=.1,stateage=.1,piloted=False)
        self.assertTrue(eval(expression,{},base))
        for change in (dict(observed=True),dict(mode=0),dict(mode=1),dict(gen=3),dict(closed=4),dict(proofage=.31),
                       dict(stateage=.31),dict(piloted=True),dict(failed=True),dict(gen=0,requested=False)):
            with self.subTest(change=change): self.assertFalse(eval(expression,{},base|change))
        self.assertTrue(eval(expression,{},base|dict(primary=False,mode=0))) # Stop always allowed.
        self.assertTrue(eval(expression,{},base|dict(enabled=False,requested=False,mode=0)))
        self.assertFalse(eval(expression,{},base|dict(enabled=False,requested=False,observed=True)))
        refused=cue.find('actions/do_if/do_else')
        self.assertIsNotNone(refused.find('stop_shooting'))
        self.assertEqual([n.get('exact') for n in refused.findall('set_value')],['[0,0]','false'])

    def test_assets_are_separately_indexed_zero_damage_stock_references(self):
        root=ROOT/'extension/x4_lan_lab'
        entries=[e for e in E.parse(root/'index/macros.xml').findall('add/entry') if e.get('name').startswith('x4lan_visual_')]
        self.assertEqual(len(entries),3)
        for entry in entries:
            path=root/entry.get('value').removeprefix('extensions/x4_lan_lab/')
            macro=E.parse(str(path)+'.xml').find('macro')
            self.assertEqual(macro.get('name'),entry.get('name'))
            self.assertEqual(macro.find('properties/damage').attrib,{'value':'0','repair':'0'})
            self.assertEqual(macro.find('properties/bullet').get('maxhits'),'1')
