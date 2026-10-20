import copy
import unittest
from pathlib import Path
from lupa.luajit21 import LuaRuntime
from x4lan.entity_descriptor import encode,decode,MAX_BYTES


def descriptor():
    return {'macro':'ship_arg_l_destroyer_02_a_macro','owner':'argon',
            'slots':[{'kind':'engine','index':0,'macro':'engine_arg_l_travel_01_mk1_macro'},
                     {'kind':'weapon','index':1,'macro':'weapon_arg_l_beam_01_mk1_macro'}],
            'groups':[{'kind':'turret','path':'..','group':'group_back','macro':'turret_arg_m_flak_02_mk1_macro','count':2},
                      {'kind':'shield','path':'../con_engine[2]','group':'','macro':'shield_arg_m_standard_02_mk2_macro','count':1}],
            'software':['software_dockmk2','software_flightassistmk1'],
            'thruster':'thruster_gen_l_allround_01_mk1_macro'}


class EntityDescriptorTests(unittest.TestCase):
    def test_canonical_roundtrip_preserves_slots_groups_and_software(self):
        value=descriptor();encoded=encode(value);decoded=decode(encoded)
        self.assertEqual(encode(decoded),encoded)
        value['slots'].reverse();value['groups'].reverse();value['software'].reverse()
        self.assertEqual(encode(value),encoded)
        self.assertEqual(decoded['slots'][1]['index'],1)
        self.assertEqual(decoded['groups'][0]['path'],'../con_engine[2]')

    def test_rejects_partial_duplicate_unknown_and_noncanonical_wire_rows(self):
        encoded=encode(descriptor())
        for bad in (encoded.rsplit('\n',1)[0],encoded+'\nV|none',encoded+'\n',encoded.replace('S|engine|0|','S|engine|00|'),
                    encoded.replace('X4SHIP1','X4SHIP2'),encoded.replace('V|','UNKNOWN|'),
                    encoded.replace('W|software_dockmk2','W|software_dockmk2\nW|software_dockmk2'),
                    '\n'.join(reversed(encoded.splitlines())), 'a'*(MAX_BYTES+1)):
            with self.subTest(bad=bad[:100]),self.assertRaises(ValueError):decode(bad)

    def test_rejects_ambiguous_slots_groups_and_engine_unsafe_values(self):
        mutations=[lambda d:d['slots'].append(copy.deepcopy(d['slots'][0])),
                   lambda d:d['groups'].append(copy.deepcopy(d['groups'][0])),
                   lambda d:d['slots'][0].update(index=True),lambda d:d['slots'][0].update(index=512),
                   lambda d:d['groups'][0].update(count=0),lambda d:d['groups'][0].update(path='x\0y'),
                   lambda d:d['groups'][0].update(path='..',group=''),lambda d:d.update(owner='argon|player'),
                   lambda d:d.update(crew=[]),lambda d:d.update(software=[[]]),
                   lambda d:d['slots'][0].update(kind=[]),lambda d:d['groups'][0].update(path={})]
        for mutate in mutations:
            value=descriptor();mutate(value)
            with self.assertRaises(ValueError):encode(value)

    def test_empty_hardware_is_explicit_not_an_incomplete_message(self):
        value=descriptor();value.update(slots=[],groups=[],software=[],thruster=None)
        self.assertEqual(decode(encode(value)),value)


class DescriptorLuaParityTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        source=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui/entity_descriptor.lua'
        self.codec=self.lua.execute(source.read_text())

    def convert(self,value):
        if value is None:return False
        if isinstance(value,dict):return self.lua.table_from({k:self.convert(v) for k,v in value.items()})
        if isinstance(value,list):return self.lua.table_from([self.convert(v) for v in value])
        return value

    def test_canonical_bytes_match_python_including_empty_hardware(self):
        values=[descriptor()]
        blank=descriptor();blank.update(slots=[],groups=[],software=[],thruster=None);values.append(blank)
        # Numeric slot ordering must not become lexicographic (2 before 10).
        many=descriptor();many['slots']=[dict(kind='weapon',index=i,macro='weapon_test_macro') for i in (10,2,0,511)]
        many['groups'].append(dict(kind='shield',path='A|B',group='group;with spaces',macro='shield_test_macro',count=512))
        values.append(many)
        for value in values:
            expected=encode(value)
            self.assertEqual(self.codec.encode(self.convert(value)),expected)
            self.assertEqual(self.codec.encode(self.codec.decode(expected)),expected)

    def test_both_decoders_reject_incomplete_corrupt_or_noncanonical_data(self):
        good=encode(descriptor())
        bad=[good.rsplit('\n',1)[0],good+'\nV|none',good+'\n',good.replace('X4SHIP1','X4SHIP2'),
             good.replace('S|engine|0|','S|engine|00|'),good.replace('S|engine|0|','S|engine|512|'),
             good.replace('W|software_dockmk2','W|software_dockmk2\nW|software_dockmk2'),
             good.replace('G|shield|','G|engine|'),good.replace('2e2e','2E2E'),
             good.replace('|argon','|argøn'),'a'*(MAX_BYTES+1)]
        # Every truncation after a complete prefix still lacks mandatory closure.
        bad.extend(good[:i] for i in range(len(good)) if good[:i]!=good)
        for value in bad:
            with self.subTest(value=value[:100]):
                self.assertIsNone(self.codec.decode(value))
                with self.assertRaises(ValueError):decode(value)

    def test_lua_encoder_rejects_sparse_or_ambiguous_native_observations(self):
        value=self.convert(descriptor());value.slots[2]=None;value.slots[3]=self.convert(descriptor()['slots'][0])
        self.assertIsNone(self.codec.encode(value))
        for mutate in (lambda d:d['slots'].append(copy.deepcopy(d['slots'][0])),
                       lambda d:d['groups'][0].update(count=0),lambda d:d.update(thruster=True),
                       lambda d:d.update(software=[[]])):
            d=descriptor();mutate(d);self.assertIsNone(self.codec.encode(self.convert(d)))
