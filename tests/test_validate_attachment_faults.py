import unittest
from dev.validate_attachment_faults import validate
from test_validate_attachment_execution import trace as base_trace,query,shift,SEED


def trace():
    original=base_trace();start=next(i for i,s in enumerate(original) if 'ATTACH_CONTROLLER prepare ' in s)
    out=original[:start]
    for op,kind in ((1,'stale'),(2,'paused')):
        prepared=shift(original[start:start+4],op-1)
        out+=prepared[:3]
        out.append(f'[X4LAN] ATTACH_FAULT armed operation={op} kind={kind} shipIndex=1 state=FREE dock=0')
        out.append(prepared[3])
        elapsed='2.500000000' if kind=='stale' else '0.100000000'
        paused='true' if kind=='paused' else 'false';reason='expired' if kind=='stale' else 'paused'
        out += [f'[X4LAN] ATTACH_FAULT sent operation={op} kind={kind} elapsed={elapsed} paused={paused}',
                f'[X4LAN] ATTACH_EXEC applied operation={op} outcome=rejected reason={reason}',
                f'[X4LAN] ATTACH_CONTROLLER execution-ack operation={op} outcome=rejected',
                f'[X4LAN] ATTACH_FAULT lua operation={op} kind={kind} shipIndex=1 state=FREE dock=0',
                f'[X4LAN] ATTACH_FAULT native operation={op} kind={kind} shipIndex=1 code=ABC-123 stored=0 docked=0 dock=0 paused={paused}',
                f'[X4LAN] ATTACH_FAULT verified operation={op} kind={kind}']
        out+=query(3+op)
    return out+shift(original[start:],2)


class NativeFaultValidatorTests(unittest.TestCase):
    def test_two_unchanged_fault_readbacks_and_complete_recovery_are_required(self):
        result=validate('\n'.join(trace()),SEED)
        self.assertTrue(result['execution_negative_timing_acceptance'])
        self.assertEqual((result['native_prepare_attempts'],result['native_rejected_consumptions'],result['operations']),(11,2,9))
        self.assertEqual([f['reason'] for f in result['native_faults']],['expired','paused'])

    def test_missing_late_wrong_actor_wrong_pause_or_mutating_fault_never_passes(self):
        original=trace();variants=[]
        for i,line in enumerate(original):
            if 'ATTACH_FAULT ' in line:variants.append(original[:i]+original[i+1:])
        for before,after in (('elapsed=2.500000000','elapsed=0.100000000'),('reason=paused','reason=expired'),
                             ('code=ABC-123 stored=0','code=XYZ-999 stored=0'),('stored=0 docked=0','stored=1 docked=1'),
                             ('paused=true','paused=false'),('kind=paused','kind=stale')):
            variants.append([s.replace(before,after) for s in original])
        for token in ('ATTACH_FAULT native','ATTACH_FAULT verified','ATTACH_FAULT armed'):
            variants.append([s for s in original if token not in s]+[s for s in original if token in s])
        variants.append([s for s in original if 'ATTACH_FAULT ' not in s])
        for lines in variants:
            with self.subTest(lines=lines),self.assertRaises(ValueError):validate('\n'.join(lines),SEED)


if __name__=='__main__':unittest.main()
