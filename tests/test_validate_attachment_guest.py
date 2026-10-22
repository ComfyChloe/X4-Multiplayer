import copy,unittest
from dev.validate_attachment_guest import validate

SEED='a'*32;HOST='b'*32;GUEST='c'*32


def trace():
    rows=[f'{i},ship,ship_arg_s_scout_01_a_macro,argon,0,0,0,0,0,0,none,0' for i in (1,2)]
    replay=dict(seed_id=SEED,rows=rows,sector='cluster_14_sector001_macro',omitted_rows=[],
        identities={i:dict(code=f'NAT-00{i}',name='4e5043',known='1',contact='1') for i in (1,2)},
        attachments={1:dict(state='FREE',dock=0),2:dict(state='STORED',dock=7)})
    lines=[];history=[];seq=0
    def emit(v):lines.append('[X4LAN] '+v)
    emit(f'ATTACH_GUEST admitted seed={SEED} docks=50000 paused=true')
    emit('ATTACH_GUEST diagnostic-unpause seed='+SEED)
    emit(f'SEED_PLAY_READY seed={SEED} slot=p2 controlled=true ownAlive=true proxyAlive=true')
    emit(f'SEED_PLAY_NATIVE seed={SEED} slot=2 host=false tradeHost=false cargoHost=false questHost=false shuttles=2 markets=2 player=0x20 p1=0x10 p2=0x20')
    for cycle in range(1,15):
        at=2*cycle+1
        history.append(dict(event='baseline',epoch=HOST,revision=cycle,sector=replay['sector'],rows=rows))
        emit(f'ATTACH_GUEST baseline epoch={HOST} revision={cycle} count=2')
        if cycle==1:emit('ATTACH_WORLD READY epoch='+HOST)
        for ship in (1,2):
            seq+=1;goal=replay['attachments'][ship]
            packet=f"UATTACH|1|{HOST}|{HOST}|{cycle}|{seq}|{ship}|{goal['dock']}|{goal['state']}"
            history.extend([dict(event='identity',packet=f'IDENT|1|{HOST}|{HOST}|{cycle}|N|{ship}|NAT-00{ship}|4e5043|1|1'),
                            dict(event='attachment',packet=packet)])
            emit(f'ATTACH_GUEST wire at={at-0.1:.6f} result=applied value='+packet.replace('UATTACH|1|'+HOST,'UATTACH|1|'+GUEST,1))
        if cycle==1:
            emit('ATTACH_GUEST prepare operation=1 ship=2 action=ATTACH state=STORED dock=7 sequence=2 at=2.910000')
            emit('ATTACH_EXEC prepared operation=1 accepted=true')
            emit('ATTACH_GUEST dispatch operation=1 at=2.920000')
            emit('ATTACH_GUEST window operation=1 result=issued')
            emit('ATTACH_EXEC applied operation=1 outcome=applied reason=invoked')
            emit('ATTACH_EXEC action operation=1 action=ATTACH state=STORED dock=7 shipIndex=2 code=NAT-002')
            emit('ATTACH_GUEST execution-ack operation=1 outcome=applied result=accepted')
        emit(f'ATTACH_GUEST cycle index={cycle} revision={cycle} elapsed={at:.6f} at={at:.6f}')
        for ship in (1,2):
            sample=(cycle-1)*2+ship;goal=replay['attachments'][ship];state=goal['state'];dock=goal['dock']
            emit(f'ATTACH_GUEST sample sequence={sample} cycle={cycle} revision={cycle} id={ship} handle={100+ship} code=NAT-00{ship} macro=ship_arg_s_scout_01_a_macro owner=argon original=true sameHandle=true state={state} dock={dock} allowPose={str(ship==1).lower()} poseError=0.01 at={at:.6f}')
            emit(f'ATTACH_GUEST_NATIVE sample sequence={sample} cycle={cycle} revision={cycle} id={ship} saved={ship} code=NAT-00{ship} macro=ship_arg_s_scout_01_a_macro owner=argon state={state} dock={dock}')
            emit(f'ATTACH_GUEST sample-complete sequence={sample}')
        emit(f'ATTACH_GUEST cycle-end index={cycle} count=2')
    emit('ATTACH_GUEST complete slot=p2 cycles=14 samples=28 executionAcks=1 elapsed=30.000000 live_native_host=false')
    stats=dict(complete=True,native_host=False,native_guest=True,replayed_native_capture=True,send_errors=0,
               peers=dict(p1=dict(lease=HOST),p2=dict(lease=GUEST)))
    return lines,replay,history,stats


class FullNativeGuestEvidenceTests(unittest.TestCase):
    def check(self,lines,replay,history,stats):return validate('\n'.join(lines),replay,50000,history,stats)

    def test_complete_native_replay_and_acknowledged_action_require_every_saved_ship(self):
        r=self.check(*trace());self.assertEqual((r['saved_ships'],r['independent_native_samples']),(2,28))
        self.assertFalse(r['whole_universe_acceptance']);self.assertFalse(r['two_native_acceptance'])
        self.assertFalse(r['attached_pose_alignment_accepted'])

    def test_controlled_native_hold_does_not_claim_production_guest_acceptance(self):
        lines,replay,history,stats=trace();replay['diagnostic_native_hold']=True
        result=self.check(lines,replay,history,stats)
        self.assertTrue(result['diagnostic_native_hold']);self.assertFalse(result['production_guest_acceptance'])

    def test_deleted_native_evidence_and_reordered_actions_fail_closed(self):
        lines,replay,history,stats=trace()
        for i in range(len(lines)):
            # Earlier missing observations are retained as convergence failures;
            # only the final three cycles require fresh complete observations.
            if 'ATTACH_GUEST wire ' in lines[i]:continue
            with self.subTest(line=i),self.assertRaises((ValueError,KeyError)):
                self.check(lines[:i]+lines[i+1:],replay,history,stats)
        actions=[v for v in lines if 'ATTACH_EXEC action ' in v]
        with self.assertRaises(ValueError):self.check([v for v in lines if v not in actions]+actions,replay,history,stats)

    def test_freshness_identity_pose_and_last_three_cycles_cannot_be_filtered(self):
        lines,replay,history,stats=trace()
        for before,after in (('saved=2','saved=1'),('sameHandle=true','sameHandle=false'),('original=true','original=false'),
            ('player=0x20','player=0x10'),('paused=true','paused=false'),('allowPose=false','allowPose=true'),
            ('poseError=0.01','poseError=1'),('at=2.920000','at=3.700000'),('code=NAT-002','code=NAT-003')):
            with self.subTest(before=before),self.assertRaises(ValueError):self.check([v.replace(before,after) for v in lines],replay,history,stats)
        early=[v.replace('poseError=0.01','poseError=1') if 'sample sequence=1 cycle=1 ' in v else v for v in lines]
        result=self.check(early,replay,history,stats);self.assertEqual(len(result['cycles'][0]['failures']),1)
        late=[v.replace('poseError=0.01','poseError=1') if 'sample sequence=23 cycle=12 ' in v else v for v in lines]
        with self.assertRaises(ValueError):self.check(late,replay,history,stats)

    def test_wire_and_baseline_must_replay_actual_qualified_host_records(self):
        lines,replay,history,stats=trace();altered=copy.deepcopy(history)
        for e in altered:
            if e['event']=='attachment' and e['packet'].endswith('|STORED'):e['packet']=e['packet'][:-6]+'EXTERNAL';break
        with self.assertRaises(ValueError):self.check(lines,replay,altered,stats)
        for values in ({'complete':False},{'send_errors':1},{'native_host':True}):
            with self.assertRaises(ValueError):self.check(lines,replay,history,{**stats,**values})

    def test_delivery_order_is_per_ship_not_a_global_sequence_barrier(self):
        lines,replay,history,stats=trace()
        first=[i for i,v in enumerate(lines) if 'ATTACH_GUEST wire ' in v][:2]
        a,b=first;lines[a],lines[b]=lines[b],lines[a]
        self.assertTrue(self.check(lines,replay,history,stats)['passed'])
        duplicate=lines[:b+1]+[lines[b]]+lines[b+1:]
        with self.assertRaises(ValueError):self.check(duplicate,replay,history,stats)

    def test_requested_geometry_requires_real_coordinates_and_matching_native_sector(self):
        lines,replay,history,stats=trace();replay['geometry_evidence']=True
        lines=[v+(' sector='+replay['sector']+' parent=400 pose=0.01,0,0 target=0,0,0' if 'ATTACH_GUEST sample ' in v
                 else ' sector='+replay['sector'] if 'ATTACH_GUEST_NATIVE sample ' in v else '') for v in lines]
        self.assertTrue(self.check(lines,replay,history,stats)['passed'])
        for before,after in (('pose=0.01,0,0','pose=500,0,0'),('parent=400','parent=UNKNOWN'),
                            ('sector='+replay['sector'],'sector=another_sector')):
            with self.subTest(before=before),self.assertRaises(ValueError):self.check([v.replace(before,after) for v in lines],replay,history,stats)


if __name__=='__main__':unittest.main()
