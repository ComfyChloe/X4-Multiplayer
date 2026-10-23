import unittest
from dev.station_snapshot import extract
from x4lan import seed,world_start

ID='a'*32
BEGIN=f'SEED_PROBE begin mode=capture id={ID} count=1'
ROW='SEED_PROBE station object=1 code=ABC-123 macro=station owner=argon sector=sector pose=0,0,0,0,0,0'
END=f'SEED_PROBE end mode=capture id={ID} count=1'
COMPLETE=f'SEED_PROBE saved success=true id={ID}'


class StationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.recipe=world_start.create(game_build='test',mod_sha256='b'*64,extensions=[],generation_seed=42)

    def read(self,lines):return extract('\n'.join(lines),self.recipe,capture_id=ID,mode='capture')

    def test_complete_ordered_native_observation(self):
        self.assertEqual(len(self.read([BEGIN,ROW,END,COMPLETE])['stations']),1)

    def test_boundary_count_completion_and_identity_failures(self):
        for lines in ([ROW,BEGIN,END,COMPLETE],[BEGIN,END,ROW,COMPLETE],[BEGIN,ROW,COMPLETE,END],
                      [COMPLETE,BEGIN,ROW,END],[BEGIN,ROW,END],[BEGIN,ROW,END,COMPLETE,COMPLETE],
                      [BEGIN,ROW,ROW,END,COMPLETE],[BEGIN.replace('count=1','count=2'),ROW,END,COMPLETE],
                      [BEGIN.replace(ID,'c'*32),ROW,END,COMPLETE],[BEGIN,ROW,END,COMPLETE,ROW],[BEGIN,ROW,END,COMPLETE+'extra'],
                      [BEGIN,ROW,END,COMPLETE.replace(ID,'c'*32)]):
            with self.subTest(lines=lines),self.assertRaises(seed.SeedError):self.read(lines)

    def test_native_mod_error_rejected(self):
        with self.assertRaises(seed.SeedError):self.read([BEGIN,ROW,END,COMPLETE,'Lua error x4lan/seed_probe failure'])
