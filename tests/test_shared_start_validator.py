import unittest
from dev.validate_shared_start import validate_role

SEED='a'*32
HOST='\n'.join(['SEED_NATIVE loaded count=1 marked=1','SEED_GATE READY seed='+SEED+' slot=p1',
    'SEED_PLAY_STATE value='+SEED+'|1|1 paused=false',
    'SEED_PLAY_READY seed='+SEED+' slot=p1 controlled=true ownAlive=true proxyAlive=true',
    'SEED_PLAY_NATIVE seed='+SEED+' slot=1 host=1 tradeHost=1 cargoHost=1 questHost=1 shuttles=2 markets=2 player=0x123 p1=0x123 p2=0x456'])

class SharedStartValidator(unittest.TestCase):
    def test_expected_host_and_stale_opposite_role(self):
        self.assertTrue(validate_role(HOST,SEED,'p1')['native_role_matches'])
        with self.assertRaises(ValueError):validate_role(HOST,SEED,'p2')
    def test_paused_wrong_object_duplicate_or_early_completion_rejected(self):
        lines=HOST.splitlines()
        for bad in (HOST.replace('paused=false','paused=true'),HOST.replace('player=0x123','player=0x456'),
                    HOST+'\n'+lines[-1],'\n'.join([lines[-1]]+lines[:-1]),HOST+'\nSEED_GATE STOPPED invalid'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_role(bad,SEED,'p1')

if __name__=='__main__':unittest.main()
