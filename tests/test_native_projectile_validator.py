"""Reject incomplete or invalid native trajectory evidence."""
import unittest
from pathlib import Path
from dev.native_projectile_test import analyze
from test_game_adapter import MOCK, LuaRuntime


class ProjectileValidatorTests(unittest.TestCase):
    def fixture(self):
        rows = []; controls = []; serial = 0
        for phase_index,(phase, configured, vx) in enumerate((('stationary',0,0),('rendered',0,0),('physical',100,100),('native',100,100))):
            for _ in range(6):
                serial += 1
                t=100+12*phase_index+6+serial*.01
                rows.append(f'PROJECTILE_PROBE sample={serial} phase={phase} configuredVx={configured} '
                            f'startTime={t} endTime={t+.2} dt=0.2 dx={vx*.2} dy=0 dz=800 vx={vx} vy=0 vz=4000')
                controls.append(f'PROJECTILE_PROBE readback={serial} phase={phase} gameTime={t} shipVx={configured} shipVy=0 shipVz=0 shipSpeed={configured} bulletVx=0 bulletVy=0 bulletVz=4000 forwardX=0 forwardY=0 forwardZ=1 upX=0 upY=1 upZ=0')
            for i in range(15):
                x=0 if phase=='stationary' else i*20
                controls.append(f'PROJECTILE_PROBE ship phase={phase} sourceTime={500+12*phase_index+5+i*.2} gameTime={100+12*phase_index+5+i*.2} x={x} y=10000 z=-2000 engineSpeed={configured} yaw=0 pitch=0 roll=0 beforeYaw=0 beforePitch=0 beforeRoll=0')
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
        for before,after in [('dz=800','dz=0'),('dt=0.2','dt=0'),('dx=20.0','dx=nan'),('dx=20.0','dx=inf')]:
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

    def test_rotating_control_is_not_an_equivalent_firing_direction(self):
        self.assertFalse(analyze(self.fixture().replace('yaw=0','yaw=0.2'))['measurement_complete'])

    def test_post_correction_zero_does_not_hide_off_axis_firing(self):
        for before,after in [('beforeYaw=0','beforeYaw=0.2'),('forwardX=0','forwardX=0.05')]:
            with self.subTest(after=after):
                self.assertFalse(analyze(self.fixture().replace(before,after))['measurement_complete'])

    def test_shot_phase_and_time_must_correlate_with_trajectory(self):
        log=self.fixture().replace('readback=1 phase=stationary','readback=1 phase=native')
        self.assertFalse(analyze(log)['measurement_complete'])
        log=self.fixture().replace('gameTime=106.01','gameTime=106.51')
        self.assertFalse(analyze(log)['measurement_complete'])

    @unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
    def test_production_orientation_mode_does_not_secretly_correct_tested_poses(self):
        source=(Path(__file__).resolve().parents[1]/'dev/projectile_probe.lua').read_text()
        for hold in (False,True):
            lua=LuaRuntime(unpack_returned_tuples=True);lua.execute(MOCK)
            lua.globals().X4LAN_CONFIG.projectile_probe_hold_orientation=hold
            lua.execute('''
              X4LAN_CONFIG.test_profile='45841';X4LAN_CONFIG.projectile_probe=true
              positions[42]={x=1000,y=10000,z=-2000,yaw=0,pitch=0,roll=0}
              GetCurTime=function() return now end
              X4LAN_BUBBLE_API={coop={ready=true,ships={[2]=42},remote={[2]={track={samples={{time=506,velocity={0}}}}}}},
                state={client={lease='test'}},universe={frame=function() end},paused=function() return false end}
            ''')
            lua.execute(source)
            lua.execute('''
              local A=X4LAN_BUBBLE_API
              A.universe.frame(1);events['X4LAN_ProjectileProbe.Ready']()
              for _,t in ipairs({506,518,530}) do
                A.coop.remote[2].track.samples[1].time=t;now=t;A.universe.frame(t)
              end
            ''')
            self.assertEqual(lua.eval('#writes'),3 if hold else 0)
            lua.execute('''
              local A=X4LAN_BUBBLE_API;A.coop.remote[2].track.samples[1].time=542;now=542;A.universe.frame(542)
            ''')
            self.assertEqual(lua.eval('#writes'),4 if hold else 1)
