"""Observation completeness must not turn a failed projectile gate into a pass."""
from pathlib import Path
import re
import unittest

from dev.native_angular_test import analyze
from test_game_adapter import LuaRuntime, MOCK
import test_native_projectile_validator as projectile_fixture


class AngularValidatorTests(unittest.TestCase):
    def fixture(self):
        rows = ['PROJECTILE_PROBE orientation_control=native_only',
                projectile_fixture.ProjectileValidatorTests().fixture().replace('forwardX=0', 'forwardX=0.003')]
        serial = 0
        for index, phase in enumerate(('stationary', 'rendered', 'physical', 'native')):
            frames = []
            for n in range(61):
                serial += 1
                t = 105+12*index+n*.05
                frames.append((serial, t))
                rows.append(f'ANGULAR_PROBE frame={serial} phase={phase} elapsed={5+12*index+n*.05} beforeTime={t} afterTime={t} beforeYaw=0.003 beforePitch=0 beforeRoll=0 afterYaw=0 afterPitch=0 afterRoll=0')
                rows.append(f'ANGULAR_PROBE native={serial} phase={phase} time={t+.01} uiTime={t} fx=0.003 fy=0 fz=1 ux=0 uy=1 uz=0')
            for n in range(6):
                shot = 6*index+n+1
                t = 106+12*index+shot*.01
                frame = max(s for s, stamp in frames if stamp <= t)
                rows.append(f'ANGULAR_PROBE shot={shot} frame={frame} time={t} fx=0.003 fy=0 fz=1 ux=0 uy=1 uz=0')
        return '\n'.join(rows)

    def test_complete_observation_keeps_projectile_failure(self):
        r = analyze(self.fixture())
        self.assertTrue(r['observation_complete'])
        self.assertFalse(r['projectile_measurement_complete'])
        self.assertFalse(r['damage_or_aim_acceptance'])
        self.assertAlmostEqual(r['phases']['rendered']['median_derived_rate'], .06)
        self.assertEqual(r['phases']['rendered']['max_after_angle'], 0)

    def test_missing_duplicate_nonfinite_or_uncorrelated_samples_fail(self):
        log = self.fixture()
        for candidate in (log.replace('ANGULAR_PROBE native=1 ', 'REMOVED native=1 '),
                          log+'\n'+next(s for s in log.splitlines() if 'ANGULAR_PROBE frame=1 ' in s),
                          log.replace('beforeYaw=0.003', 'beforeYaw=nan'),
                          log.replace('uiTime=105.0 ', 'uiTime=104.0 '),
                          log.replace('shot=1 frame=21', 'shot=1 frame=99999')):
            with self.subTest(candidate=candidate[-100:]):
                self.assertFalse(analyze(candidate)['observation_complete'])

    def test_independent_shot_time_and_basis_must_agree(self):
        for change in ('time=107 fx=0.003', 'time=106.01 fx=0.4'):
            log = re.sub(r'(ANGULAR_PROBE shot=1 frame=\d+ )time=106.01 fx=0.003', r'\g<1>'+change, self.fixture())
            self.assertFalse(analyze(log)['observation_complete'])

    def test_native_readback_latency_is_checked(self):
        self.assertFalse(analyze(self.fixture().replace('time=105.01 ', 'time=106.0 '))['observation_complete'])

    @unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
    def test_observer_reads_before_and_after_real_adapter_without_extra_pose_writes(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK)
        lua.execute('''
          X4LAN_CONFIG.test_profile='45841';X4LAN_CONFIG.projectile_probe=true;X4LAN_CONFIG.angular_probe=true
          positions[42]={x=1000,y=10000,z=-2000,yaw=.1,pitch=.2,roll=.3}
          calls=0
          X4LAN_BUBBLE_API={coop={ready=true,ships={[2]=42},remote={[2]={track={samples={{time=506}}}}},
            frame=function(t) calls=calls+1;assert(t==106);api.SetObjectSectorPos(42,22,{x=1000,y=10000,z=-2000,yaw=0,pitch=0,roll=0}) end},
            state={client={lease='test'}},universe={frame=function() end},paused=function() return false end}
          requests={};AddUITriggeredEvent=function(screen,control,value) requests[#requests+1]={screen,control,value} end
        ''')
        lua.execute((Path(__file__).resolve().parents[1]/'dev/angular_probe.lua').read_text())
        lua.execute('''
          now=106;X4LAN_BUBBLE_API.universe.frame(now);events['X4LAN_AngularProbe.Ready']()
          X4LAN_BUBBLE_API.coop.frame(now)
          assert(calls==1 and #writes==1)
          assert(logs[1]:find('beforeYaw=0.100000000') and logs[1]:find('afterYaw=0.000000000'))
          assert(requests[2][2]=='Sample' and requests[2][3].serial==1 and requests[2][3].phase=='stationary')
        ''')
