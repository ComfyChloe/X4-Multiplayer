"""Combat census table summary (dev/combat_scenario/census_report.py); the table writer itself is
tested by `cargo test -p x4mp-combat` (game::census). Never launches X4."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('combat_census_report', ROOT / 'dev' / 'combat_scenario' / 'census_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

COLUMNS = ('now weapon_calls weapon_nested weapon turret missile_launcher missile_turret with_target aiming firing '
           'fired shots missile_shots bullet_calls bullet_checks bullet_hits hit_resets bullet_fresh fresh_same_frame '
           'rng_weapon rng_bullet rng_max_call rng_unmatched rng_frame weapon_ticks bullet_ticks frame_ticks').split()


def table(rows):
    lines = ['X4MP-COMBATCENSUS 1 steam_build=23660954 source=native',
             '# frames=%d dropped_frames=0 foreign=0 uninitialised_tls=0 rng_unmatched=0 rng_frame_over_cap=0 '
             'ticks_per_second=1000000' % len(rows),
             '# shots mean=1.000 p50=1.000 p95=1.000 max=1.000',
             '\t'.join(COLUMNS)]
    for row in rows:
        values = dict.fromkeys(COLUMNS, 0)
        values.update(row)
        lines.append('\t'.join(str(values[c]) for c in COLUMNS))
    return '\n'.join(lines) + '\n'


class CensusReport(unittest.TestCase):
    def test_summary_and_derived_shares(self):
        rows = [
            dict(now=10.0, weapon_calls=100, with_target=40, shots=4, bullet_calls=50, bullet_checks=10,
                 bullet_hits=2, bullet_fresh=4, fresh_same_frame=3, rng_weapon=16, rng_bullet=4, rng_frame=200,
                 weapon_ticks=2000, bullet_ticks=500, frame_ticks=50000),
            dict(now=10.05, weapon_calls=100, with_target=60, shots=0, bullet_calls=52, bullet_checks=12,
                 bullet_hits=0, bullet_fresh=0, fresh_same_frame=0, rng_weapon=0, rng_bullet=6, rng_frame=-1,
                 weapon_ticks=1000, bullet_ticks=600, frame_ticks=40000),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x4mp-combatcensus.txt'
            path.write_text(table(rows), encoding='utf-8')
            meta, parsed = report.read(path)
        self.assertEqual(meta['ticks_per_second'], 1000000)
        s = report.summarise(meta, parsed)
        self.assertEqual(s['frames'], 2)
        self.assertEqual(s['per_frame']['shots']['max'], 4)
        self.assertAlmostEqual(s['rng_combat_share'], 20 / 200, msg='frames over the cap are left out')
        self.assertAlmostEqual(s['rng_per_shot_upper'], 4.0)
        self.assertAlmostEqual(s['rng_bullet_per_check'], 10 / 22)
        self.assertAlmostEqual(s['fresh_same_frame_share'], 0.75)
        self.assertAlmostEqual(s['idle_weapon_share'], 0.5)
        self.assertAlmostEqual(s['frame_ms']['max'], 50.0)
        self.assertIn('rng_combat_share', report.text(s))

    def test_refuses_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'other.txt'
            path.write_text('X4CJSTEP-HEX 1\n', encoding='utf-8')
            self.assertEqual(report.main([str(path)]), 3)


if __name__ == '__main__':
    unittest.main()
