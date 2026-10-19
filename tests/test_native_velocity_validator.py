"""Reject incomplete native motion, lifecycle and release evidence."""
import unittest
from dev.native_velocity_test import analyze,PHASES,NUMERIC,FLAGS


def row(phase,tag,elapsed,managed=False,speed=0,pilot=False):
    r={k:0 for k in NUMERIC};r.update({k:False for k in FLAGS})
    r.update(sample=phase,tag=tag,elapsed=elapsed,time=100+PHASES.index(phase)*6+elapsed,
             rx=1,uy=1,fz=1,speed=speed,vx=speed,managed=managed,wantManaged=managed,
             pilot=pilot,parentZone=True,topology=True,generation=2,wantGeneration=2,
             sequence=5,wantSequence=5)
    if phase in ('motion','resume'):
        r.update(x=speed*elapsed,wantX=speed*elapsed,wantVx=speed)
    return r


def fixture():
    rows=[]
    for phase in PHASES:
        for i in range(15):
            rows.append(row(phase,'settled',2+i*.2,phase in ('motion','freeze','resume'),
                            110 if phase in ('motion','resume','takeover') else 0,phase=='takeover'))
    for phase,tag in (('revoked','old_generation'),)+tuple((p,p) for p in PHASES[6:11]):
        for i in range(12):rows.append(row(phase,tag,1+i*.2,phase not in ('revoked','expired','wrong_parent')))
    a=row('watchdog','watchdog_window',.75,True,110);a['deadline']=125.05
    b=row('watchdog','watchdog_window',1.1);b['deadline']=125.05
    rows.extend((a,b))
    return rows


def log(rows):
    return '\n'.join('NATIVE_VELOCITY sample='+r['sample']+' '+' '.join(f'{k}={str(v).lower() if isinstance(v,bool) else v}' for k,v in r.items() if k!='sample') for r in rows)+'\nNATIVE_VELOCITY complete=1'


class NativeVelocityValidatorTests(unittest.TestCase):
    def test_complete_evidence_retains_single_native_scope(self):
        r=analyze(log(fixture()));self.assertTrue(r['measurement_complete'])
        self.assertFalse(r['two_native_acceptance']);self.assertIn('not an engine',r['pause_scope'])

    def test_missing_phase_nonfinite_or_incomplete_native_readback_fails(self):
        rows=fixture();self.assertFalse(analyze(log([r for r in rows if r['sample']!='freeze']))['measurement_complete'])
        rows[0]['vx']=float('nan');self.assertFalse(analyze(log(rows))['measurement_complete'])
        del rows[0]['vx'];self.assertFalse(analyze(log(rows))['measurement_complete'])

    def test_pose_agreement_alone_cannot_replace_native_velocity(self):
        rows=fixture();rows[0]['vx']=0
        self.assertFalse(analyze(log(rows))['measurement_complete'])

    def test_native_speed_alone_cannot_replace_actual_displacement(self):
        rows=fixture()
        for r in rows:
            if r['sample']=='motion':r['x']=r['wantX']=0
        self.assertFalse(analyze(log(rows))['measurement_complete'])

    def test_delayed_command_must_not_reactivate_revoked_override(self):
        rows=fixture();r=next(r for r in rows if r['tag']=='old_generation');r['managed']=True
        self.assertFalse(analyze(log(rows))['measurement_complete'])
        rows=fixture();r=next(r for r in rows if r['tag']=='wrong_session');r['sequence']+=1
        self.assertFalse(analyze(log(rows))['measurement_complete'])

    def test_watchdog_deadline_and_real_pilot_release_are_required(self):
        rows=fixture();rows[-1]['time']+=.2
        self.assertFalse(analyze(log(rows))['measurement_complete'])
        rows=fixture()
        for r in rows:
            if r['sample']=='takeover':r['pilot']=False
        self.assertFalse(analyze(log(rows))['measurement_complete'])

    def test_unproven_parent_topology_or_engine_error_fails(self):
        rows=fixture();rows[0]['topology']=False
        self.assertFalse(analyze(log(rows))['measurement_complete'])
        self.assertFalse(analyze(log(fixture())+'\nError in X4LAN_Coop.Velocity: invalid reference')['measurement_complete'])
