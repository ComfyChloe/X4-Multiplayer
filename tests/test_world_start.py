import json
from pathlib import Path
import tempfile
import unittest

from x4lan import seed, world_start

CONTENT = dict(game_build='world1-9.00-steam23660954', mod_sha256='c'*64,
               extensions=[dict(id='ego_dlc_timelines', version='900')])


class WorldStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / '45841'; self.profile.mkdir()
        self.config = self.profile / 'config.xml'
        self.config.write_text('<root><autosave>false</autosave></root>')
        self.recipe = world_start.create(**CONTENT, generation_seed=42)

    def test_same_transferred_recipe_gives_identical_seed_arguments_without_profile_writes(self):
        before = self.config.read_bytes()
        copied = json.loads(json.dumps(self.recipe))
        expected = world_start.arguments(self.recipe, self.profile, observed_content=CONTENT)
        self.assertEqual(world_start.arguments(copied, self.profile, observed_content=CONTENT), expected)
        self.assertIn('-seed:42', expected); self.assertNotIn('-seed', expected)
        self.assertNotIn('-load', expected)
        self.assertEqual(self.config.read_bytes(), before)

    def test_conflicting_duplicate_or_noncanonical_config_seed_rejected_without_edits(self):
        for extra in ('<seed>43</seed>', '<seed>42</seed><seed>42</seed>', '<seed>0042</seed>'):
            self.config.write_text('<root><autosave>false</autosave>'+extra+'</root>')
            before = self.config.read_bytes()
            with self.assertRaises(seed.SeedError):
                world_start.arguments(self.recipe, self.profile, observed_content=CONTENT)
            self.assertEqual(self.config.read_bytes(), before)
        self.config.write_text('<root><autosave>false</autosave><seed>42</seed></root>')
        self.assertIn('-seed:42', world_start.arguments(self.recipe, self.profile, observed_content=CONTENT))

    def test_mismatching_content_and_existing_load_refused(self):
        for field, value in [('game_build','other'),('mod_sha256','d'*64),('extensions',[])]:
            with self.assertRaises(seed.SeedError):
                world_start.arguments(self.recipe,self.profile,observed_content=CONTENT | {field:value})
        self.config.write_text('<root><autosave>false</autosave><load>save_001</load></root>')
        with self.assertRaises(seed.SeedError):
            world_start.arguments(self.recipe,self.profile,observed_content=CONTENT)

    def test_recipe_bounds_and_unknown_fields(self):
        for value in (0,-1,2**31,True,42.0,'42',None):
            with self.subTest(value=value), self.assertRaises(seed.SeedError):
                world_start.validate(self.recipe | {'generation_seed': value})
        with self.assertRaises(seed.SeedError): world_start.validate(self.recipe | {'load':'save_001'})
        with self.assertRaises(seed.SeedError): world_start.validate(self.recipe | {'gamestart':'../bad'})

    def test_recipe_fingerprint_binds_content_world_and_generation_seed(self):
        original=world_start.fingerprint(self.recipe)
        for field,value in [('generation_seed',43),('world_id','a'*32),('mod_sha256','d'*64)]:
            self.assertNotEqual(original,world_start.fingerprint(self.recipe | {field:value}))

    def test_normal_profile_and_autosave_enabled_refused(self):
        with self.assertRaises(seed.SeedError):
            world_start.arguments(self.recipe,self.profile.parent,observed_content=CONTENT)
        self.config.write_text('<root><autosave>true</autosave></root>')
        with self.assertRaises(seed.SeedError):
            world_start.arguments(self.recipe,self.profile,observed_content=CONTENT)
