from copy import deepcopy
import math
import unittest
from x4lan import seed, station_catalog as stations, world_start


class StationCatalogTests(unittest.TestCase):
    def setUp(self):
        self.recipe=world_start.create(world_id='a'*32,generation_seed=42,game_build='test',mod_sha256='b'*64,extensions=[])
        self.row=dict(object=100,code='ABC-123',macro='station_factory',owner='argon',sector='sector_1',pose=[1,2,3,0,0,0])
        self.host=stations.create(self.recipe,[self.row])

    def guest(self, **changes):
        return stations.create(self.recipe,[deepcopy(self.row) | {'object':500} | changes])

    def test_handles_rebind_without_using_pose_as_identity(self):
        result=stations.match(self.host,self.guest())
        self.assertTrue(result['station_layout_matches']);self.assertEqual(result['local_bindings'],{'100':500})
        self.assertFalse(result['ongoing_world_authority'])

    def test_same_seed_does_not_accept_wrong_station_position(self):
        result=stations.match(self.host,self.guest(pose=[1700,2,3,0,0,0]))
        self.assertFalse(result['station_layout_matches']);self.assertEqual(result['local_bindings'],{})
        self.assertEqual(result['max_position_error_m'],1699)

    def test_identity_owner_sector_or_macro_change_rejects_even_at_same_position(self):
        for field,value in [('owner','teladi'),('sector','sector_2'),('macro','station_shipyard'),('code','XYZ-789')]:
            result=stations.match(self.host,self.guest(**{field:value}))
            self.assertFalse(result['station_layout_matches']);self.assertTrue(result['missing']);self.assertTrue(result['extra'])

    def test_duplicate_display_codes_disambiguated_by_fixed_descriptors(self):
        other=deepcopy(self.row) | {'object':101,'sector':'sector_2'}
        host=stations.create(self.recipe,[self.row,other])
        guest=stations.create(self.recipe,[other | {'object':501},self.row | {'object':500}])
        self.assertEqual(stations.match(host,guest)['local_bindings'],{'100':500,'101':501})

    def test_ambiguous_identity_never_resolved_by_position(self):
        other=deepcopy(self.row) | {'object':101,'pose':[200,300,400,0,0,0]}
        with self.assertRaisesRegex(seed.SeedError,'ambiguous'):stations.create(self.recipe,[self.row,other])

    def test_duplicate_handles_unresolved_ids_and_invalid_numbers_rejected(self):
        for value in (math.nan,math.inf,True,1e13):
            with self.assertRaises(seed.SeedError):self.guest(pose=[value,0,0,0,0,0])
        with self.assertRaises(seed.SeedError):self.guest(owner='unknown')
        with self.assertRaises(seed.SeedError):self.guest(object=True)
        with self.assertRaises(seed.SeedError):stations.create(self.recipe,[self.row,self.row | {'code':'DEF-456'}])

    def test_angle_wrap_and_native_float_tolerance(self):
        guest=self.guest(pose=[1.016,2,3,2*math.pi,0,0])
        self.assertTrue(stations.match(self.host,guest)['station_layout_matches'])
        self.assertFalse(stations.match(self.host,self.guest(pose=[1,2,3,0.001,0,0]))['station_layout_matches'])

    def test_changed_recipe_and_incomplete_catalog_refused(self):
        guest=self.guest();guest['recipe_sha256']='c'*64
        with self.assertRaises(seed.SeedError):stations.match(self.host,guest)
        with self.assertRaises(seed.SeedError):stations.create(self.recipe,[])
        extra=deepcopy(self.row) | {'object':101,'code':'DEF-456'}
        result=stations.match(self.host,stations.create(self.recipe,[self.row,extra]))
        self.assertFalse(result['station_layout_matches']);self.assertEqual(result['local_bindings'],{})
