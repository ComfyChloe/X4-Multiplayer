import unittest
from dev.validate_native_seed import validate

SEED = "a" * 32
ROWS = (
    "[=ERROR=] 4.2 [X4LAN] SEED_PROBE station object=300 code=ABC-001 macro=station_factory "
    "owner=argon sector=sector_a pose=10000,0,20000,0.5,0,0\n"
    "[=ERROR=] 4.2 [X4LAN] SEED_PROBE station object=400 code=DEF-002 macro=station_wharf "
    "owner=teladi sector=sector_b pose=-20000,800,-50000,1,0,0\n")


def log(mode, rows=ROWS):
    completion = "saved success=true" if mode == "capture" else "loaded"
    return (f"SEED_PROBE begin mode={mode} id={SEED} count=2\n" + rows
            + f"SEED_PROBE end mode={mode} id={SEED} count=2\n"
            + f"SEED_PROBE {completion} id={SEED}\n")


class NativeSeedValidatorTests(unittest.TestCase):
    def test_repeated_display_codes_are_disambiguated_by_fixed_native_descriptors(self):
        rows=ROWS.replace('DEF-002','ABC-001')
        loaded='\n'.join(reversed(rows.replace('object=300','object=900').replace('object=400','object=901').splitlines()))+'\n'
        result=validate(log('capture',rows),log('load',loaded),SEED)
        self.assertEqual(result['stations'],2)
        self.assertEqual(result['duplicate_code_groups'],{'ABC-001':2})
        self.assertEqual(result['station_identity_key'],['code','macro','owner','sector'])

    def test_ambiguous_descriptor_collisions_are_not_matched_by_position(self):
        rows=ROWS.replace('DEF-002','ABC-001').replace('station_wharf','station_factory').replace('owner=teladi','owner=argon').replace('sector_b','sector_a')
        with self.assertRaises(ValueError):validate(log('capture',rows),log('load',rows),SEED)

    def test_duplicate_local_component_is_not_two_stations(self):
        rows=ROWS.replace('object=400','object=300')
        with self.assertRaises(ValueError):validate(log('capture',rows),log('load',rows),SEED)

    def test_same_native_layout_with_distinct_local_handles(self):
        result = validate(log("capture"), log("load", ROWS.replace("object=300", "object=900")), SEED)
        self.assertEqual(result["stations"], 2)
        self.assertTrue(result["native_reload"])
        self.assertFalse(result["local_component_ids_equal"])
        self.assertFalse(result["ongoing_simulation_authority"])
        self.assertFalse(result["two_native_acceptance"])

    def test_missing_unknown_duplicate_or_changed_station_is_rejected(self):
        for rows in (ROWS.splitlines()[0] + "\n", ROWS.replace("macro=station_factory", "macro=nil"),
                     ROWS.replace("DEF-002", "ABC-001"), ROWS.replace("sector_b", "sector_a"),
                     ROWS.replace("owner=argon", "owner=teladi"), ROWS.replace("10000,0", "10001,0"),
                     ROWS.replace(",0.5,0", ",0.6,0"), ROWS.replace("10000,0", "nan,0")):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate(log("capture"), log("load", rows), SEED)

    def test_completion_epoch_boundaries_and_native_errors_are_required(self):
        original = log("load")
        for changed in (original.replace(SEED, "b" * 32),
                        original.replace("SEED_PROBE loaded", "SEED_PROBE load_requested"),
                        original.replace("SEED_PROBE end", "SEED_PROBE interrupted"),
                        original + original,
                        original + "Error in MD cue md.X4LAN_SeedProbe.Read\n"):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate(log("capture"), changed, SEED)


if __name__ == "__main__":
    unittest.main()
