"""Execute the queued native command guards against changed execution-time state.

Engine side effects remain separately verified by native stock-mission captures.
"""
import re
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from xml.etree import ElementTree as E

TREE=E.parse(Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/md/x4lan_missions.xml')
ACTIONS={n.get('name'):n.find('actions') for n in TREE.iter() if n.tag in ('cue','library')}


def command(a,r,*,host=True,age=10.2,wall='now',epoch='a'*32,warp=1):
    env=dict(a=a,r=r,Session=NS(active=True,host=host,epoch=epoch),player=NS(age=age,wall=wall,warp=warp));effects=[]
    def evaluate(v):
        if v=='event.param3':return a
        if v=='@Session.$rows.{$a.$id}':return r if a.id==1 else None
        v=v.replace("player.systemtime.{'%Y-%m-%dT%H:%M:%S'}",'player.wall').replace('player.timewarp.factor','player.warp')
        v=re.sub(r'\b1s\b','1',v).replace('$','').replace('@','')
        v=re.sub(r'\ba.from\b',"a.start",v)
        for old,new in [(' ge ',' >= '),(' le ',' <= '),('true','True'),('false','False')]:v=v.replace(old,new)
        return eval(v,{},env)
    def run(nodes):
        chain=False
        for n in nodes:
            if n.tag in ('do_if','do_elseif','do_else'):
                if n.tag=='do_if':chain=False
                if not chain and (n.tag=='do_else' or evaluate(n.get('value'))):chain=True;run(n)
            elif n.tag=='set_value':env[n.get('name').lstrip('$')]=evaluate(n.get('exact'))
            elif n.tag=='include_actions':run(ACTIONS[n.get('ref')])
            elif n.tag=='signal_objects':effects.append(('accept',evaluate(n.get('object'))))
            elif n.tag=='signal_cue':effects.append(('abort',evaluate(n.get('cue'))))
            else:raise AssertionError('Unsupported MD action '+n.tag)
    run(ACTIONS['Command']);return effects


def fixture():
    a=NS(id=1,revision=2,epoch='a'*32,stamp='now',start=10,deadline=11,action='ACCEPT')
    r=NS(revision=2,state='O',mission=NS(exists=True,hasmissionoffer=True,hasmission=False),client=NS(exists=False),abort=NS(exists=True))
    return a,r


class MissionNativeFenceTests(unittest.TestCase):
    def test_virtual_stock_giver_can_accept_with_live_offer(self):
        a,r=fixture();self.assertEqual(command(a,r),[('accept',r.client)])
    def test_closed_changed_and_missing_records_cannot_execute_queued_accept(self):
        for field,value in [('revision',3),('state','A'),('state','C'),('client',None)]:
            a,r=fixture();setattr(r,field,value);self.assertFalse(command(a,r))
        a,r=fixture();r.mission.hasmissionoffer=False;self.assertFalse(command(a,r))
        a,r=fixture();r.mission.exists=False;self.assertFalse(command(a,r))
        a,r=fixture();self.assertFalse(command(a,None))
    def test_clock_epoch_role_and_timewarp_rechecked_at_execution(self):
        for kw in [dict(host=False),dict(epoch='b'*32),dict(age=9),dict(age=12),dict(wall='later'),dict(warp=10)]:
            self.assertFalse(command(*fixture(),**kw))
    def test_shared_abort_requires_still_active_native_mission(self):
        a,r=fixture();a.action='ABORT';r.state='A';r.mission.hasmission=True
        self.assertEqual(command(a,r),[('abort',r.abort)])
        r.mission.hasmission=False;self.assertFalse(command(a,r))
    def test_stale_revision_after_first_accept_cannot_execute_again(self):
        a,r=fixture();self.assertTrue(command(a,r));r.revision=3;r.state='A'
        self.assertFalse(command(a,r))
