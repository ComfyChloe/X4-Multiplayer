import copy,unittest
from dev.validate_attachment_world import validate

SEED='a'*32;HOST='b'*32;GUEST='c'*32


def trace():
    lines=[];history=[]
    def emit(v):lines.append('[X4LAN] '+v)
    emit(f'ATTACH_FULL admitted seed={SEED} docks=50000 paused=true')
    emit('ATTACH_FULL diagnostic-unpause seed='+SEED)
    emit(f'SEED_PLAY_READY seed={SEED} slot=p1 controlled=true ownAlive=true proxyAlive=true')
    emit(f'SEED_PLAY_NATIVE seed={SEED} slot=1 host=true tradeHost=true cargoHost=true questHost=true shuttles=2 markets=2 player=0x10 p1=0x10 p2=0x20')
    for rev in range(1,4):
        packet=f'UATTACH|1|{HOST}|{HOST}|{rev}|{rev}|1|0|FREE'
        emit(f'ATTACH_FULL publish at={rev}.000000 handle=100 code=NAT-001 macro=ship_arg_s_scout_01_a_macro owner=argon value={packet}')
        emit(f'ATTACH_FULL sample-request sequence={rev} revision={rev} id=1')
        emit(f'ATTACH_FULL_NATIVE sample sequence={rev} revision={rev} id=1 saved=1 code=NAT-001 macro=ship_arg_s_scout_01_a_macro owner=argon state=FREE dock=0')
        emit(f'ATTACH_FULL sample-complete sequence={rev}')
        history.extend([
            dict(event='baseline',epoch=HOST,revision=rev,sector='cluster_14_sector001_macro',
                 rows=['1,ship,ship_arg_s_scout_01_a_macro,argon,0,0,0,0,0,0,none,0']),
            dict(event='identity',packet=f'IDENT|1|{GUEST}|{HOST}|{rev}|N|1|NAT-001|4e5043|1|1'),
            dict(event='attachment',packet=packet.replace('UATTACH|1|'+HOST,'UATTACH|1|'+GUEST,1)),
        ])
    emit('ATTACH_FULL complete slot=p1 revisions=3 published=3 samples=3 native_guest=false')
    stats=dict(complete=True,native_host=True,native_guest=False,send_errors=0,
               peers=dict(p1=dict(lease=HOST),p2=dict(lease=GUEST)))
    return lines,history,stats


class FullNativeHostEvidenceTests(unittest.TestCase):
    def check(self,lines,history,stats):return validate('\n'.join(lines),SEED,50000,history,stats)

    def test_native_publication_requires_full_adapter_fixtures_readbacks_and_real_delivery(self):
        result=self.check(*trace())
        self.assertEqual((result['baselines'],result['saved_ships'],result['independent_native_samples']),(3,1,3))
        self.assertFalse(result['whole_universe_acceptance'])
        self.assertFalse(result['two_native_acceptance'])

    def test_removed_reordered_or_wrong_native_evidence_never_passes(self):
        lines,history,stats=trace()
        variants=[lines[:i]+lines[i+1:] for i in range(len(lines))]
        variants.append([v for v in lines if 'sample-request ' not in v]+[v for v in lines if 'sample-request ' in v])
        for before,after in (('saved=1','saved=2'),('state=FREE dock=0','state=STORED dock=7'),
                             ('code=NAT-001 macro','code=NAT-002 macro'),('player=0x10','player=0x20'),
                             ('paused=true','paused=false'),('handle=100','handle=0')):
            variants.append([v.replace(before,after) for v in lines])
        for altered in variants:
            with self.subTest(index=variants.index(altered)),self.assertRaises(ValueError):self.check(altered,history,stats)

    def test_peer_provenance_and_full_saved_baseline_coverage_are_required(self):
        lines,history,stats=trace()
        for i in range(len(history)):
            # Each revision needs baseline, native IDENT and a received attachment.
            if history[i]['event']=='identity':continue  # Repeated identical IDENT is not independently required.
            with self.assertRaises(ValueError):self.check(lines,history[:i]+history[i+1:],stats)
        for altered in ({**stats,'complete':False},{**stats,'native_host':False},{**stats,'native_guest':True},
                        {**stats,'send_errors':1}):
            with self.assertRaises(ValueError):self.check(lines,history,altered)
        missing=copy.deepcopy(history)
        for event in missing:
            if event['event']=='baseline':event['rows'].append('2,ship,ship_arg_s_scout_01_a_macro,argon,0,0,0,0,0,0,none,0')
        with self.assertRaises(ValueError):self.check(lines,missing,stats)
        with self.assertRaises(ValueError):self.check(lines,[v for v in history if v['event']!='identity'],stats)


if __name__=='__main__':unittest.main()
