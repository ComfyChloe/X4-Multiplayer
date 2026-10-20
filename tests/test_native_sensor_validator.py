import unittest
from dev.validate_native_sensors import analyze


def trace(failure=False):
    rows = []
    for phase in ("remote1", "remote2", "activated1", "activated2", "native_ship", "native_pilot"):
        for index in (1, 2, 6, 7):
            known = not failure or phase.startswith("native") or index < 6
            name = "Contact" if known else "Unknown Ship"
            x = 1000 if index == 2 else 399000
            z = 2000 if index == 6 else 6000 if index == 7 else 0
            rows.append(f"[=ERROR=] 35.82 [X4LAN] SENSOR_PROBE phase={phase} index={index} object={index} "
                        f"known={str(known).lower()} radar=true range=40000 name={name} header={name} position={x},10000,{z}")
    return "\n".join(rows)


class SensorValidationTests(unittest.TestCase):
    def test_original_failure_followed_by_control_success_is_not_a_fix(self):
        result = analyze(trace(True))
        self.assertTrue(result["original_failure_reproduced"])
        self.assertTrue(result["native_control_discovery"])
        self.assertFalse(result["success"])

    def test_complete_production_discovery_passes_but_missing_data_does_not(self):
        self.assertTrue(analyze(trace())["success"])
        self.assertFalse(analyze("\n".join(trace().splitlines()[:-1]))["success"])
        self.assertFalse(analyze("")["success"])

    def test_near_origin_unknown_hud_insufficient_range_and_native_error_fail(self):
        for text in [trace().replace("399000", "1000"), trace().replace("range=40000", "range=5000"),
                     trace().replace("header=Contact", "header=Unknown Ship"),
                     trace() + "\nError in MD cue md.X4LAN_SensorProbe.Remote: invalid"]:
            with self.subTest(text=text[-80:]):
                self.assertFalse(analyze(text)["success"])

    def test_duplicate_and_malformed_rows_fail(self):
        for text in [trace() + "\n" + trace().splitlines()[0], trace().replace("known=true", "known=nil")]:
            with self.assertRaises(ValueError):
                analyze(text)
