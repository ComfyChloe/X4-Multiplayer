import unittest
from dev.validate_loadout_roundtrip import validate
from x4lan.entity_descriptor import encode


def trace():
    rows=[]
    for index in range(1,9):
        for kind in ('expected','observed'):
            rows.append(f'LOADOUT_ROUNDTRIP {kind} id={index} macro=ship_{index}_macro rows=engine,1,engine_arg_s_01_macro;shield,1,shield_arg_s_01_macro;software,software_dockmk2')
        rows.append(f'LOADOUT_ROUNDTRIP_NATIVE id={index} source=SRC-00{index} clone=NEW-00{index} input=2 unused=0 sourceHull=1000 cloneHull=1000 sourceShield=500 cloneShield=500')
    return '\n'.join(rows+['LOADOUT_ROUNDTRIP complete count=8'])


class LoadoutRoundtripValidatorTests(unittest.TestCase):
    def serialized_trace(self):
        rows=[trace(),'LOADOUT_ROUNDTRIP serialized-mode=true']
        for i in range(1,9):
            d={'macro':f'ship_{i}_macro','owner':'argon','slots':[
                {'kind':'engine','index':0,'macro':'engine_arg_s_01_macro'},
                {'kind':'shield','index':0,'macro':'shield_arg_s_01_macro'}],
                'groups':[],'software':['software_dockmk2'],'thruster':None}
            wire=encode(d)
            rows.append(f'LOADOUT_ROUNDTRIP descriptor id={i} bytes={len(wire)} hex={wire.encode().hex()}')
            rows.append(f'LOADOUT_ROUNDTRIP identity id={i} sourceMacro=ship_{i}_macro cloneMacro=ship_{i}_macro sourceOwner=argon cloneOwner=argon')
        return '\n'.join(rows)

    def test_serialized_native_equipment_and_actual_owner_match(self):
        self.assertTrue(validate(self.serialized_trace(),True)['serialized_descriptors_match_native_equipment_and_owner'])

    def test_serialized_validator_requires_complete_resolved_headers_and_native_identity(self):
        raw=self.serialized_trace()
        bad=[raw.replace('descriptor id=','descriptor unavailable id='),raw.replace('identity id=','identity unavailable id='),
             raw.replace('cloneOwner=argon','cloneOwner=player',1),raw.replace('cloneMacro=ship_1_macro','cloneMacro=ship_2_macro',1),
             raw.replace('descriptor id=8','descriptor id=7'),raw.replace('serialized-mode=true','serialized-mode=unavailable'),trace()]
        for value in bad:
            with self.subTest(value=value[-80:]),self.assertRaises(ValueError):validate(value,True)

    def test_complete_native_slot_primitive(self):
        self.assertEqual(validate(trace())['ship_macros'],8)

    def test_capacity_or_slot_difference_rejected_even_if_boolean_claims_match(self):
        for before,after in (('cloneHull=1000','cloneHull=999'),('unused=0','unused=1'),
                             ('cloneShield=500','cloneShield=nan'),
                             ('observed id=3 macro=ship_3_macro rows=engine,1,engine_arg_s_01_macro',
                              'observed id=3 macro=ship_3_macro rows=engine,1,engine_arg_s_02_macro')):
            with self.subTest(after=after),self.assertRaises(ValueError):
                validate(trace().replace(before,after,1)+'\nLOADOUT_ROUNDTRIP result id=3 exactSlots=true')

    def test_partial_duplicate_or_failed_capture_rejected(self):
        raw=trace()
        for bad in ('\n'.join(raw.splitlines()[1:]),raw+'\n'+raw.splitlines()[0],
                    raw.replace('ship_8_macro','ship_7_macro'),raw+'\nLua error: probe failed',
                    raw+'\n[=ERROR=] 0.00 extensions\\x4_lan_lab\\md\\probe.xml(18): Required attribute is missing',
                    raw.replace('LOADOUT_ROUNDTRIP complete count=8','')):
            with self.subTest(bad=bad[-90:]),self.assertRaises(ValueError):validate(bad)
