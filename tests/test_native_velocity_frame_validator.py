"""Frame classification must follow independent native motion and orientation."""
import math
import unittest
from pathlib import Path

from dev.native_velocity_frame_test import analyze, INPUT
from test_game_adapter import MOCK, LuaRuntime


def basis(yaw,pitch,roll):
    y,p,r=map(math.radians,(yaw,pitch,roll));cy,sy,cp,sp,cr,sr=math.cos(y),math.sin(y),math.cos(p),math.sin(p),math.cos(r),math.sin(r)
    return ((cy*cr+sy*sp*sr,cp*sr,-sy*cr+cy*sp*sr),
            (-cy*sr+sy*sp*cr,cp*cr,sy*sr+cy*sp*cr),(sy*cp,-sp,cy*cp))


def fixture(component_input=False):
    lines=[]
    for phase,angles in enumerate(((0,0,0),(90,0,0),(180,0,0),(0,45,0),(0,0,45),(35,20,-15)),1):
        axes=basis(*angles);start=100+(phase-1)*8
        ack=dict(set=phase,gameTime=start)
        ack.update({name+a:axes[i][j] for name,i in (('forward',2),('up',1)) for j,a in enumerate('XYZ')})
        lines.append('VELOCITY_FRAME '+' '.join(f'{k}={v}' for k,v in ack.items()))
        velocity=[sum(axes[i][j]*INPUT[i] for i in range(3)) for j in range(3)] if component_input else INPUT
        component=[sum(axes[i][j]*velocity[j] for j in range(3)) for i in range(3)]
        for index in range(26):
            elapsed=2+index*.2
            row=dict(sample=phase,gameTime=start+elapsed)
            row.update({a:velocity[j]*elapsed for j,a in enumerate('xyz')})
            row.update({'v'+a:component[j] for j,a in enumerate('xyz')})
            row.update({b+a:axes[i][j] for i,b in enumerate('ruf') for j,a in enumerate('xyz')})
            row.update({b+a:float(j==axis) for b,axis in (('pf',2),('pu',1),('sf',2),('su',1)) for j,a in enumerate('xyz')})
            lines.append('VELOCITY_FRAME '+' '.join(f'{k}={v}' for k,v in row.items()))
    lines+=['VELOCITY_FRAME set=7 gameTime=148 forwardX=0 forwardY=0 forwardZ=1 upX=0 upY=1 upZ=0','VELOCITY_FRAME complete=1']
    return '\n'.join(lines)


class VelocityFrameValidatorTests(unittest.TestCase):
    def test_sector_vector_requires_all_rotated_native_controls(self):
        r=analyze(fixture());self.assertTrue(r['measurement_complete'])
        self.assertTrue(r['input_matches_sector_vector']);self.assertFalse(r['input_matches_component_vector'])
        self.assertFalse(r['two_native_acceptance'])

    def test_component_setter_is_classified_without_assuming_sector_frame(self):
        r=analyze(fixture(True));self.assertTrue(r['measurement_complete'])
        self.assertFalse(r['input_matches_sector_vector']);self.assertTrue(r['input_matches_component_vector'])

    def test_missing_phase_duplicate_or_nonfinite_readback_fails(self):
        for log in (fixture().replace('sample=6','sample=8'),fixture().replace('set=6','set=5'),
                    fixture().replace('vx=100.0','vx=nan'),fixture().replace('vx=100.0','bad=100.0')):
            self.assertFalse(analyze(log)['measurement_complete'])

    def test_native_velocity_must_agree_with_actual_displacement(self):
        self.assertFalse(analyze(fixture().replace('vx=100.0','vx=0.0'))['measurement_complete'])

    def test_setter_ack_alone_does_not_prove_stable_sample_orientation(self):
        self.assertFalse(analyze(fixture().replace('fx=0.0','fx=0.02'))['measurement_complete'])

    def test_order_and_settling_window_are_required(self):
        lines=fixture().splitlines();lines[1],lines[2]=lines[2],lines[1]
        self.assertFalse(analyze('\n'.join(lines))['measurement_complete'])
        self.assertFalse(analyze(fixture().replace('gameTime=102.0','gameTime=100.2'))['measurement_complete'])

    def test_missing_completion_or_engine_error_fails(self):
        self.assertFalse(analyze(fixture().replace('VELOCITY_FRAME complete=1',''))['measurement_complete'])
        self.assertFalse(analyze(fixture()+'\nError in X4LAN_VelocityFrame: invalid object')['measurement_complete'])

    @unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
    def test_rotation_control_preserves_measured_native_translation(self):
        lua=LuaRuntime(unpack_returned_tuples=True);lua.execute(MOCK)
        lua.execute('''
          X4LAN_CONFIG.test_profile='45841';X4LAN_CONFIG.velocity_frame_probe=true
          positions[42]={x=1000,y=10000,z=-2000,yaw=0,pitch=0,roll=0}
          GetCurTime=function() return now end
          X4LAN_BUBBLE_API={coop={ready=true,ships={[2]=42}},state={client={lease='test'}},
            universe={frame=function() end},paused=function() return false end}
        ''')
        lua.execute((Path(__file__).resolve().parents[1]/'tools/dev/velocity_frame_probe.lua').read_text())
        lua.execute('''
          local A=X4LAN_BUBBLE_API;A.universe.frame(0);events['X4LAN_VelocityFrame.Ready']()
          for index=0,5 do
            now=index*8;positions[42].x=1000+index*123;positions[42].y=10000-index*23;positions[42].z=-2000+index*12
            A.universe.frame(now)
            assert(writes[#writes].x==1000+index*123 and writes[#writes].y==10000-index*23 and writes[#writes].z==-2000+index*12)
          end
          assert(#writes==6 and writes[2].yaw==90 and writes[4].pitch==45 and writes[5].roll==45)
          now=49;A.universe.frame(now);assert(#writes==6)
        ''')
