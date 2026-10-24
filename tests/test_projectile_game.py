"""Engine doubles for observed-shot authority and delayed native acknowledgements."""
from pathlib import Path
import unittest
import xml.etree.ElementTree as E
from test_game_adapter import LuaRuntime
ROOT=Path(__file__).resolve().parents[1]
UI=ROOT/'extension/x4_lan_lab/ui'


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ProjectileAdapterTests(unittest.TestCase):
    def make(self):
        l=LuaRuntime(unpack_returned_tuples=True)
        l.execute('''
         now=10;paused=false;proof=true;controlled=11;events={};requests={};sent={};writes={}
         classes={[11]='ship',[42]='ship',[101]='bullet',[200]='object',[201]='object'}
         sectors={[11]=22,[42]=22,[101]=22,[200]=22,[201]=22}
         api={IsComponentOperational=function(id) return classes[id]~=nil end,
          IsComponentClass=function(id,k) return classes[id]==k end,
          GetContextByClass=function(id) return sectors[id] or 0 end,
          GetPlayerControlledShipID=function() return controlled end,
          GetPlayerContainerID=function() return controlled end,
          GetObjectPositionInSector=function(id) for i=#writes,1,-1 do if writes[i].id==id then return writes[i].p end end;error('unwritten object') end,
          GetPositionalOffset=function() return {x=100+170*(now-10),y=200-300*(now-10),z=300+2200*(now-10),yaw=.1,pitch=-.2,roll=0} end,
          SetObjectSectorPos=function(id,sector,p) writes[#writes+1]={id=id,sector=sector,p=p} end}
         local req=require;require=function(name) if name=='ffi' then return {C=api,cdef=function() end,new=function(n,t) return t end} else return req(name) end end
         GetComponentData=function() return 'sector_macro' end;ConvertIDTo64Bit=tonumber
         DebugError=function() end;RegisterEvent=function(n,f) events[n]=f end
         AddUITriggeredEvent=function(s,c,a) requests[#requests+1]={screen=s,control=c,a=a} end
         X4LAN_CONFIG={slot='p1',projectile_visuals=true,shot_visual=true,shot_capture=true,shot_claims=true,pilot_health=true,fire2=true}
         C={owners={'p1','p2'},pilots={'p1','p2'},epochs={1,1},ships={11,42},remote={[2]={at=10,sector='sector_macro',sourceLease=string.rep('c',32),track={offset=-40}}},frame=function() end,receive=function() return false end}
         H={valid={3,4},closed={0,0},mdAt=10,mdAge=100,mdStamp='2026-10-24T06:20:00'}
         D={ready=true,stateRevision=1,support={{macro='weapon_ter_s_plasma_01_mk1_macro'},{macro='weapon_ter_s_plasma_01_mk1_macro'}}}
         V={ready=true,readyOriginal=function() return proof end,readyReplay=function() return proof end}
         S={client={lease=string.rep('a',32)},transport={send=function(self,p) sent[#sent+1]=p end}}
         X4LAN_BUBBLE_API={coop=C,health=H,shot=D,shotVisual=V,state=S,id=tostring,paused=function() return paused end}
        ''')
        for f in ['projectile_motion.lua','projectile_protocol.lua','projectile_game.lua']:
            l.execute((UI/f).read_text())
        l.execute('''
         X=X4LAN_BUBBLE_API.projectiles;W=X4LAN_PROJECTILE_PROTOCOL;X.ready=true;X.now=now
         X.contextsFrame(now);for e,q in pairs(X.contexts) do X.state(e..'|'..q.ticket) end
         function clock(t) now=t;X.now=t;H.mdAt=t;C.remote[2].at=t end
         function last(kind) local out;for _,r in ipairs(requests) do if r.control==kind then out=r.a end end;return out end
         function count(kind) local n=0;for _,r in ipairs(requests) do if r.control==kind then n=n+1 end end;return n end
         function packet(id,time)
          return {type='BOLT',direction='forwarded',lease=S.client.lease,source_lease=string.rep('c',32),entity=2,epoch=1,gen=4,id=id or 1,time=time or 50,
           sector='sector_macro',kind=1,x=100,y=200,z=300,vx=170,vy=-300,vz=2200,yaw=.1,pitch=-.2,roll=0,ttl=1.7}
         end
         function receive(m)
          local f={};for v in (assert(W.serialize(m))..'|'):gmatch('([^|]*)|') do f[#f+1]=v end
          return X.receive(f,now)
         end
         function ackcreate(a,id) X.created('2|'..a.ticket..'|'..a.slot..'|'..a.operation..'|'..(id or 200)..'|22') end
        ''')
        return l

    def test_engine_luaid_userdata_is_not_concatenated_or_used_as_context_identity(self):
        self.make().execute('''
         local before=X.contexts[2];local ticket=before.ticket
         X4LAN_BUBBLE_API.id=function() return newproxy(true) end
         clock(10.05);C.frame(now);clock(10.1);C.frame(now)
         assert(not X.failed and X.contexts[2]==before and before.ticket==ticket)
         receive(packet());assert(before.buffer.count==1)
        ''')

    def test_zone_reparent_is_corrected_once_and_persistent_displacement_removes_owned_carrier(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.make().execute('''
                 receive(packet());clock(10.2);X.renderFrame(now);local a=last('Create');ackcreate(a)
                 local read=api.GetObjectPositionInSector
                 api.GetObjectPositionInSector=function(id)
                  if #writes==1 or '''+str(persistent).lower()+''' then return {x=-50000,y=200,z=-5897} end
                  return read(id)
                 end
                 X.renderFrame(now);assert(#writes==2)
                 assert((X.slots[a.slot]==nil)=='''+str(persistent).lower()+''')
                 for _,w in ipairs(writes) do assert(w.id==200) end
                ''')

    def test_far_sector_float_rounding_is_tolerated_but_zone_sized_displacement_is_not(self):
        for delta, accepted in ((.03125, True), (50742, False)):
            with self.subTest(delta=delta):
                self.make().execute('''
                 local m=packet();m.x=250000;receive(m);clock(10.2);X.renderFrame(now)
                 local a=last('Create');ackcreate(a);local read=api.GetObjectPositionInSector
                 api.GetObjectPositionInSector=function(id)
                  local p=read(id);return {x=p.x+'''+str(delta)+''',y=p.y,z=p.z}
                 end
                 X.renderFrame(now);assert((X.slots[a.slot]~=nil)=='''+str(accepted).lower()+''')
                 assert(#writes=='''+('1' if accepted else '2')+''')
                ''')

    def test_native_creation_ack_precedes_any_move_and_matches_delayed_actual_path(self):
        self.make().execute('''
         assert(receive(packet()));X.renderFrame(now);assert(count('Create')==0)
         clock(10.2);X.renderFrame(now);local a=last('Create');assert(a and #writes==0)
         ackcreate(a);X.renderFrame(now);assert(#writes==1 and writes[1].id==200)
         assert(math.abs(writes[1].p.x-108.5)<1e-8 and math.abs(writes[1].p.y-185)<1e-8)
         assert(math.abs(writes[1].p.yaw-.1*180/math.pi)<1e-8)
        ''')

    def test_wrong_lease_generation_epoch_sector_kind_and_lost_pilot_refuse(self):
        for change in ("m.lease=string.rep('b',32)","m.source_lease=string.rep('d',32)","m.gen=3",
                       "m.epoch=2","m.sector='different_macro'","m.kind=2","C.pilots[2]='0'",'proof=false'):
            with self.subTest(change=change):
                self.make().execute("local m=packet();"+change+";receive(m);assert(X.contexts[2].buffer.count==0)")

    def test_late_native_ack_after_pause_or_authority_change_never_moves(self):
        for change in ('paused=true', "C.owners[2]='p1'",'H.valid[2]=5', 'H.closed[2]=4', 'sectors[42]=23'):
            with self.subTest(change=change):
                self.make().execute('''receive(packet());clock(10.2);X.renderFrame(now);local a=last('Create');'''+change+'''
                 X.contextsFrame(now);ackcreate(a);X.renderFrame(now);assert(#writes==0 and count('Remove')>=1)
                ''')

    def test_expiry_and_missing_native_ack_are_bounded_and_late_remove_cannot_target_new_slot(self):
        self.make().execute('''
         receive(packet());clock(10.2);X.renderFrame(now);local old=last('Create')
         clock(10.6);X.contextsFrame(now);X.renderFrame(now);assert(not X.slots[old.slot])
         local q=X.contexts[2];X.state('2|'..q.ticket);receive(packet(2,50.7));clock(10.86);X.renderFrame(now)
         local new=last('Create');assert(new.operation>old.operation);ackcreate(old)
         assert(X.slots[new.slot].operation==new.operation);ackcreate(new,201);X.renderFrame(now)
         assert(#writes==1 and writes[1].id==201)
        ''')

    def test_native_rejection_retries_live_observation_without_reviving_ended_or_other_slots(self):
        self.make().execute('''
         receive(packet());clock(10.2);X.renderFrame(now);local old=last('Create')
         local reject='2|'..old.ticket..'|'..old.slot..'|'..old.operation
         X.rejected(reject);clock(10.24);X.renderFrame(now);assert(count('Create')==1)
         clock(10.26);X.renderFrame(now);assert(not X.slots[old.slot])
         clock(10.27);X.renderFrame(now);local new=last('Create');assert(new.operation>old.operation)
         X.rejected(reject);assert(not X.slots[new.slot].retryAt)
         ackcreate(new);X.renderFrame(now);assert(#writes==1)
         X.rejected('2|'..new.ticket..'|'..new.slot..'|'..new.operation)
         assert(not X.slots[new.slot].retryAt)
        ''')
        self.make().execute('''
         receive(packet());clock(10.2);X.renderFrame(now);local old=last('Create')
         X.rejected('2|'..old.ticket..'|'..old.slot..'|'..old.operation)
         receive({type='BEND',direction='forwarded',lease=S.client.lease,source_lease=string.rep('c',32),entity=2,epoch=1,gen=4,id=1,time=50.05})
         clock(10.3);X.renderFrame(now);assert(not next(X.slots) and count('Create')==1)
        ''')

    def test_pause_cutoff_and_duplicate_packets_never_respawn(self):
        self.make().execute('''
         receive(packet());clock(10.2);X.renderFrame(now);local a=last('Create');ackcreate(a)
         paused=true;clock(10.3);X.contextsFrame(now);assert(X.contexts[2].buffer.count==0)
         paused=false;clock(10.4);X.contextsFrame(now);X.state('2|'..X.contexts[2].ticket)
         receive(packet());receive(packet(2,50.2));assert(X.contexts[2].buffer.count==0)
         receive(packet(3,50.4));assert(X.contexts[2].buffer.count==1)
        ''')

    def test_actual_native_velocity_and_first_pose_send_immediately_without_selected_target(self):
        self.make().execute('''
         X.born('1|3|'..X.contexts[1].ticket..'|101|22|1|170|-300|2200');X.captureFrame(now);assert(#sent==1)
         local m=assert(W.parse(sent[1],'client'));assert(m.entity==1 and m.gen==3)
         assert(math.abs(m.vx-170)<1e-8 and math.abs(m.vy+300)<1e-8 and math.abs(m.vz-2200)<1e-8)
         assert(m.time==10 and m.yaw==.1 and m.ttl==1.7)
         classes[101]=nil;clock(10.04);X.captureFrame(now);m=assert(W.parse(sent[2],'client'));assert(m.type=='BEND' and m.time==10.04)
         clock(10.10);X.captureFrame(now);clock(10.16);X.captureFrame(now);assert(#sent==4 and not next(X.captures))
        ''')

    def test_native_velocity_invalid_payloads_are_rejected_and_far_pose_cannot_change_velocity(self):
        for values in ('', '|nan|0|1', '|inf|0|1', '|100001|0|1', '|1||2', '|1|2|3|4'):
            with self.subTest(values=values):
                self.make().execute("X.born('1|3|'..X.contexts[1].ticket..'|101|22|1"+values+"');assert(not next(X.captures))")
        self.make().execute('''
         api.GetPositionalOffset=function() return {x=249000.015625,y=10000,z=40,yaw=.1,pitch=-.2,roll=0} end
         X.born('1|3|'..X.contexts[1].ticket..'|101|22|1|855.05|-840.615|4849.231');X.captureFrame(now)
         local m=assert(W.parse(sent[1],'client'))
         assert(m.x==249000.015625 and m.vx==855.05 and m.vy==-840.615 and m.vz==4849.231)
        ''')

    def test_capture_rejects_wrong_native_context_and_stops_on_handover(self):
        self.make().execute('''
         X.born('1|4|'..X.contexts[1].ticket..'|101|22|1|170|-300|2200');assert(not next(X.captures))
         X.born('1|3|'..X.contexts[1].ticket..'|101|23|1|170|-300|2200');assert(not next(X.captures))
         X.born('1|3|'..X.contexts[1].ticket..'|101|22|1|170|-300|2200');assert(next(X.captures))
         controlled=42;clock(10.03);X.captureFrame(now);assert(not next(X.captures) and #sent==0)
        ''')

    def test_failure_stops_only_visuals_and_never_activates_real_guns(self):
        self.make().execute('''
         X.fail('fixture');assert(X.failed and not D.failed and count('Stop')==1)
         receive(packet());X.contextsFrame(now);X.renderFrame(now);assert(#writes==0)
        ''')


class NativeProjectileGateTests(unittest.TestCase):
    def test_setup_stops_only_remote_scripted_fire_and_preserves_player_input(self):
        tree=E.parse(ROOT/'extension/x4_lan_lab/md/x4lan_projectile.xml')
        stop=tree.find('cues/cue/actions/do_all/do_if')
        self.assertEqual(stop.get('value'),'@$arena.$ships.{$e}.isoperational and player.controlled != $arena.$ships.{$e}')
        self.assertIsNotNone(stop.find('stop_shooting'))

    def test_native_slot_cancel_fences_queued_create_and_cannot_remove_newer_object(self):
        tree=E.parse(ROOT/'extension/x4_lan_lab/md/x4lan_projectile.xml')
        cues={x.get('name'):x for x in tree.iter('cue')}
        create=cues['Create'].find('actions/do_if').get('value')
        remove=cues['Remove'].find('actions/do_if').get('value')
        self.assertIn('$a.$operation gt $operations.{$i}',create)
        self.assertIn('$a.$operation ge $operations.{$i}',remove)
        self.assertEqual(cues['Remove'].find('actions/do_if/set_value').get('name'),'$operations.{$i}')
        # Execute the actual inequality fragments, including cancel-before-create.
        def permits(expr, op, saved):
            return eval(expr.replace('$a.$operation','op').replace('$operations.{$i}','saved').replace(' gt ',' > ').replace(' ge ',' >= '),{},dict(op=op,saved=saved))
        c='$a.$operation gt $operations.{$i}';r='$a.$operation ge $operations.{$i}'
        self.assertTrue(permits(r,9,8));self.assertFalse(permits(c,9,9))
        self.assertTrue(permits(c,10,9));self.assertFalse(permits(r,9,10))

    def test_full_native_execution_gate_rechecks_identity_generation_pause_and_deadline(self):
        tree=E.parse(ROOT/'extension/x4_lan_lab/md/x4lan_projectile.xml')
        window=next(x for x in tree.iter('library') if x.get('name')=='Window').findall('actions/set_value')[-1].get('exact')
        create=next(x for x in tree.iter('cue') if x.get('name')=='Create').find('actions/do_if').get('value')
        mapping={
            "player.systemtime.{'%Y-%m-%dT%H:%M:%S'}":'wall', 'player.timewarp.factor':'unpaused',
            'player.age - $arena.$shotStateAt':'state_age','player.age - $arena.$shotVisualAt.{$e}':'proof_age',
            'player.age - $at.{$e}':'permission_age', 'player.age / 1s':'age',
            '@$arena.$ships.{$e}.isoperational':'operational', '@$arena.$shotFailed':'shot_failed',
            '$arena.$shotCurrentGen.{$e}':'current_gen','$arena.$healthClosed.{$e}':'closed',
            '$arena.$shotState':'revision','$arena.$shotVisualMode.{$e}':'proof_mode',
            '$arena.$shotVisualGen.{$e}':'proof_gen','$arena.$ships.{$e}':'current_ship',
            '$ships.{$e}.sector':'ship_sector','$ships.{$e}':'ship','$sectors.{$e}':'sector',
            '$operations.{$i}':'operation','$tickets.{$e}':'ticket','$gens.{$e}':'gen',
            '$modes.{$e}':'mode','$kinds.{$e}':'kind','player.controlled':'controlled','player.sector':'player_sector',
            '$failed':'failed','$valid':'valid','$e':'entity','$i':'slot'}
        fields=['entity','gen','revision','stamp','from','deadline','slot','operation','ticket','kind','ttl']
        for k in fields: mapping['$a.$'+k]='request_'+k
        def translate(s):
            for a,b in sorted(mapping.items(),key=lambda kv:-len(kv[0])):s=s.replace(a,b)
            for a,b in [(' gt ',' > '),(' ge ',' >= '),(' le ',' <= '),(' lt ',' < '),('0.3s','.3'),('0s','0')]:s=s.replace(a,b)
            self.assertNotIn('$',s);return s
        window,create=translate(window),translate(create)
        base=dict(failed=False,shot_failed=False,entity=2,request_gen=4,current_gen=4,closed=3,
                  request_revision=7,revision=7,state_age=.1,unpaused=1,wall='stamp',request_stamp='stamp',
                  age=10,request_from=9.9,request_deadline=10.2,operational=True,slot=1,
                  request_operation=8,operation=7,request_ticket=5,ticket=5,gen=4,mode=2,controlled=11,
                  ship=42,current_ship=42,ship_sector=22,sector=22,player_sector=22,request_kind=1,kind=1,
                  proof_mode=2,proof_gen=4,permission_age=.1,proof_age=.1,request_ttl=1)
        def accepted(changes):
            v=base|changes;v['valid']=eval(window,{},v);return eval(create,{},v)
        self.assertTrue(accepted({}))
        for change in [dict(closed=4),dict(current_gen=5),dict(unpaused=0),dict(wall='new second'),
                       dict(age=10.3),dict(state_age=.31),dict(proof_age=.31),dict(permission_age=.31),
                       dict(operational=False),dict(shot_failed=True),dict(current_ship=43),dict(controlled=42),
                       dict(ship_sector=23),dict(player_sector=23),dict(proof_mode=1),dict(mode=0),
                       dict(request_gen=3),dict(request_ticket=4),dict(request_operation=7),dict(request_ttl=0),
                       dict(request_ttl=3.1),dict(slot=257),dict(request_kind=2)]:
            with self.subTest(change=change):self.assertFalse(accepted(change))

    def test_carrier_is_meshless_effects_finite_and_no_damage_or_physical_bullets_are_created(self):
        root=ROOT/'extension/x4_lan_lab';base=root/'assets/x4lan/projectiles'
        carrier=E.parse(base/'x4lan_projectile_carrier.xml').find('component')
        self.assertEqual(carrier.get('class'),'object');self.assertIsNone(carrier.find('source'));self.assertFalse(carrier.findall('.//part'))
        effects=E.parse(root/'libraries/effects.xml').findall('add/effect')
        self.assertEqual(len(effects),3)
        for fx in effects:
            element=fx.find('elements/element');self.assertEqual(element.get('duration'),'3')
            macro=E.parse(base/(element.get('ref')+'.xml')).find('macro');self.assertEqual(macro.get('class'),'effectobject')
            self.assertIsNone(macro.find('properties/damage'))
        text=(root/'md/x4lan_projectile.xml').read_text()
        for action in ('shoot','apply_damage','set_object_hull','set_object_shield'):
            self.assertNotIn('<'+action+' ',text)
