import copy
import unittest
from dev.validate_native_seed_objects import validate


class NativeSeedObjectsValidatorTests(unittest.TestCase):
    def fixture(self):
        capture={'rows':['1,ship,ship_test_macro,argon,100,200,300,0,0,0,none,0'],
                 'identities':{'1':{'code':'ABC-123'}}}
        replay={'history':[{'phase':p,'revision':r} for p,r in [('initial',1),('absent',3),('return',4)]]}
        spec='SEED_OBJECT_SPEC slot={slot} revision={rev} id=1 code=ABC-123 macro=ship_test_macro owner=argon maxhull=1000 maxshield=200 weapons=2 shields=1 engines=1 pilot=true\n'
        host=spec.format(slot='p1',rev=1)
        guest=''
        for rev in (1,4):
            guest+=f'SEED_OBJECT_PROBE object slot=p2 revision={rev} id=1 handle=2002 code=ABC-123 macro=ship_test_macro owner=argon original=true sameHandle=true pose=100,200,300\n'
            guest+=f'SEED_OBJECT_PROBE summary slot=p2 revision={rev} count=1 live=1 matching=1 retained=0 maxError=0.000000 failed=nil\n'
            guest+=spec.format(slot='p2',rev=rev)
        guest+='SEED_OBJECT_PROBE summary slot=p2 revision=3 count=0 live=0 matching=0 retained=1 maxError=0.000000 failed=nil\n'
        return host,guest,capture,replay

    def test_complete_original_objects_equipment_and_retention(self):
        result=validate(*self.fixture())
        self.assertEqual(result['original_ships'],1)
        self.assertFalse(result['two_native_acceptance'])

    def test_rejects_visual_clone_changed_handle_identity_and_equipment(self):
        host,guest,capture,replay=self.fixture()
        for old,new in [('original=true','original=false'),('sameHandle=true','sameHandle=false'),
                        ('code=ABC-123','code=ABC-124'),('engines=1','engines=0'),
                        ('pilot=true','pilot=unavailable'),('maxhull=1000','maxhull=nan')]:
            with self.subTest(new=new),self.assertRaises(ValueError):
                validate(host,guest.replace(old,new),capture,replay)
        with self.assertRaises(ValueError):
            validate(host,guest.replace('revision=4 id=1 handle=2002','revision=4 id=1 handle=3003'),capture,replay)

    def test_rejects_missing_views_retention_equipment_and_identity_capture(self):
        host,guest,capture,replay=self.fixture()
        for content in ('\n'.join(line for line in guest.splitlines() if 'revision=4' not in line),
                        '\n'.join(line for line in guest.splitlines() if 'SPEC' not in line),
                        '\n'.join(line for line in guest.splitlines() if not ('SPEC' in line and 'revision=4' in line)),
                        guest.replace('pose=100,200,300','pose=900,200,300'),
                        guest.replace('retained=1','retained=0'),guest.replace('maxError=0.000000','maxError=5.000000')):
            with self.assertRaises(ValueError): validate(host,content,capture,replay)
        bad=copy.deepcopy(capture);bad['identities']={}
        with self.assertRaises(ValueError): validate(host,guest,bad,replay)
        with self.assertRaises(ValueError): validate(host,guest,capture,{'history':[]})

    def test_rejects_native_failure_and_invalid_position(self):
        host,guest,capture,replay=self.fixture()
        for content in (guest+' STOPPED binding failed',guest.replace('failed=nil','failed=true'),
                        guest+'\n[=ERROR=] 0.00 extensions\\x4_lan_lab\\md\\probe.xml(18): Required attribute is missing',
                        guest.replace('pose=100,200,300','pose=nan,200,300')):
            with self.assertRaises(ValueError): validate(host,content,capture,replay)

    def test_seed_only_scope_requires_exact_explicit_post_snapshot_omissions(self):
        host,guest,capture,replay=self.fixture()
        capture['rows'].append('250001,ship,new_ship_macro,argon,0,0,0,0,0,0,none,0')
        capture['identities']['250001']={'code':'NEW-001'}
        with self.assertRaises(ValueError): validate(host,guest,capture,replay)
        replay.update(seed_only=True,omitted_host_created_ids=[250001])
        result=validate(host,guest,capture,replay)
        self.assertEqual(result['explicitly_omitted_host_created_ids'],[250001])
        self.assertTrue(result['seed_only'])
        replay['omitted_host_created_ids']=[1,250001]
        with self.assertRaises(ValueError): validate(host,guest,capture,replay)


if __name__=='__main__': unittest.main()
