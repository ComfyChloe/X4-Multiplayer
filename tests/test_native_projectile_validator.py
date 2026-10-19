"""Reject incomplete or invalid native trajectory evidence."""
import unittest
from dev.native_projectile_test import analyze


class ProjectileValidatorTests(unittest.TestCase):
    def fixture(self):
        rows = []; controls = []; serial = 0
        for phase, configured, vx in (('stationary',0,0),('rendered',0,0),('physical',100,100),('native',100,100)):
            for _ in range(6):
                serial += 1
                rows.append(f'PROJECTILE_PROBE sample={serial} phase={phase} configuredVx={configured} '
                            f'dt=0.2 dx={vx*.2} dy=0 dz=800 vx={vx} vy=0 vz=4000')
                controls.append(f'PROJECTILE_PROBE readback={serial} shipVx={configured} shipVy=0 shipVz=0 shipSpeed={configured} bulletVx=0 bulletVy=0 bulletVz=4000')
            for i in range(15):
                x=0 if phase=='stationary' else i*20
                controls.append(f'PROJECTILE_PROBE ship phase={phase} gameTime={i*.2} x={x} y=10000 z=-2000 engineSpeed={configured}')
        return '\n'.join(controls+rows)

    def test_complete_measurements_report_deltas_without_gameplay_acceptance(self):
        r=analyze(self.fixture())
        self.assertTrue(r['measurement_complete'])
        self.assertEqual(r['rendered_lateral_delta'],0)
        self.assertEqual(r['physical_lateral_delta'],100)
        self.assertFalse(r['two_native_acceptance'])
        self.assertFalse(r['damage_or_aim_acceptance'])

    def test_missing_phase_or_projectile_does_not_pass(self):
        rows=self.fixture().splitlines()
        self.assertFalse(analyze('\n'.join(rows[:-1]))['measurement_complete'])
        self.assertFalse(analyze('\n'.join(r for r in rows if 'phase=native' not in r))['measurement_complete'])

    def test_invalid_objects_timestamps_or_nonfinite_vectors_do_not_pass(self):
        for before,after in [('vz=4000','vz=0'),('dt=0.2','dt=0'),('vx=100','vx=nan'),('dx=20.0','dx=inf')]:
            with self.subTest(after=after):
                self.assertFalse(analyze(self.fixture().replace(before,after))['measurement_complete'])

    def test_duplicate_or_malformed_readback_does_not_pass(self):
        self.assertFalse(analyze(self.fixture().replace('sample=24','sample=23'))['measurement_complete'])
        r=analyze(self.fixture()+'\nPROJECTILE_PROBE sample=25 phase=native')
        self.assertFalse(r['measurement_complete']);self.assertEqual(r['malformed_readbacks'],1)

    def test_projectiles_alone_do_not_establish_moving_controls(self):
        rows=[r for r in self.fixture().splitlines() if ' ship phase=' not in r]
        r=analyze('\n'.join(rows))
        self.assertTrue(r['projectile_samples_complete'])
        self.assertFalse(r['measurement_complete'])
        self.assertFalse(analyze(self.fixture().replace('shipVx=100','shipVx=0'))['measurement_complete'])

    def test_missing_native_readbacks_cannot_be_replaced_by_configured_values(self):
        rows=[r for r in self.fixture().splitlines() if 'readback=' not in r]
        self.assertFalse(analyze('\n'.join(rows))['measurement_complete'])
