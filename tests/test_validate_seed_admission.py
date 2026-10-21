import unittest
from dev.validate_seed_admission import admission_order

SEED='a'*32


def trace():
    return ['[X4LAN] '+line for line in (
        'SEED_ADMISSION request control=Inspect paused=true ready=false',
        'SEED_NATIVE loaded count=1 marked=1',
        'SEED_ADMISSION request control=Inspect paused=true ready=false',
        'SEED_ADMISSION request control=Read paused=true ready=false',
        f'DOCK_IMPORT event mode=load kind=End value={SEED}|0|1|2|2|1',
        f'SEED_DOCK_GATE READY seed={SEED}',
        f'SEED_GATE READY seed={SEED} slot=p2',
        f'SEED_ADMISSION ready seed={SEED} paused=false pauseCalls=2 resumeCalls=1',
        'SEED_ADMISSION request control=Read paused=true ready=true',
        'SEED_ADMISSION request control=Inspect paused=true ready=true',
        f'SEED_ADMISSION complete seed={SEED} count=2 pristine=1 loads=1')]


class AdmissionEvidenceTests(unittest.TestCase):
    def test_native_readiness_follows_complete_import_then_pause_release_and_final_marker(self):
        result=admission_order('\n'.join(trace()),SEED,2)
        self.assertEqual((result['pause_calls'],result['resume_calls']),(2,1))
        held=[line.replace('paused=false pauseCalls=2 resumeCalls=1','paused=true pauseCalls=0 resumeCalls=0') for line in trace()]
        self.assertTrue(admission_order('\n'.join(held),SEED,2)['preserved_existing_pause'])

    def test_missing_early_unpaused_or_false_pristine_evidence_never_passes(self):
        original=trace();variants=[]
        for i in (1,3,4,5,6,7,8,9,10):variants.append(original[:i]+original[i+1:])
        variants.append(original[:4]+original[5:]+[original[4]])
        variants.append(original+[original[8]])
        variants.append([line+' unavailable' if 'SEED_GATE READY' in line else line for line in original])
        for old,new in (('paused=true ready=false','paused=false ready=false'),('resumeCalls=1','resumeCalls=0'),
                        ('pauseCalls=2','pauseCalls=0'),('pristine=1','pristine=0'),('count=2','count=3')):
            variants.append([s.replace(old,new) for s in original])
        for lines in variants:
            with self.subTest(lines=lines),self.assertRaises(ValueError):admission_order('\n'.join(lines),SEED,2)


if __name__=='__main__':unittest.main()
