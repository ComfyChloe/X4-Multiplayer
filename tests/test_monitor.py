"""Server visibility, timeouts and duplicate launch behaviour, without changing leases."""
import io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from x4lan.economy import EconomySession
from x4lan.host import Relay
from x4lan.monitor import Monitor,render,probe,attach

TOKEN='a'*32
class MonitorTests(unittest.TestCase):
 def make(self):
  session=EconomySession(TOKEN,'build','bubble')
  relay=SimpleNamespace(session=session,socket=SimpleNamespace(getsockname=lambda:('127.0.0.1',45841)),received_packets=0,sent_packets=0,send_errors=0)
  return session,relay,Monitor(relay,0)
 def test_two_players_timeout_and_reconnect_are_visible(self):
  s,r,m=self.make()
  for slot,addr,n in [('p1',('127.0.0.1',1),'b'),('p2',('10.1.1.2',2),'c')]:
   s.receive(f'HELLO|1|{TOKEN}|build|sector|bubble|{slot}|{n*32}'.encode(),addr,0)
  snap=m.snapshot(.1);self.assertEqual(snap['connected'],2)
  self.assertNotIn(TOKEN,json.dumps(snap));self.assertNotIn(s.peers['p1'].lease,json.dumps(snap))
  p=s.peers['p1'];s.receive(f'IDLE|1|{p.lease}|0'.encode(),p.address,3)
  snap=m.snapshot(3);self.assertEqual(snap['players'][1]['status'],'NO UPDATES')
  s.expire(29.9);snap=m.snapshot(29.9)
  self.assertEqual(snap['players'][1]['status'],'NO UPDATES')
  self.assertEqual(snap['connection_timeout'],30)
  s.receive(f'IDLE|1|{p.lease}|1'.encode(),p.address,29.9)
  s.expire(30);snap=m.snapshot(30)
  self.assertEqual(snap['players'][1]['status'],'TIMED OUT');self.assertEqual(snap['players'][1]['address'],'10.1.1.2:2')
  self.assertEqual(snap['players'][0]['status'],'CONNECTED')
  self.assertTrue(any('P2 TIMED OUT' in e['message'] for e in snap['events']))
  self.assertIn('Connection timeout: 30s',render(snap))
  s.receive(f'HELLO|1|{TOKEN}|build|sector|bubble|p2|{"d"*32}'.encode(),('10.1.1.2',3),30.2)
  self.assertEqual(m.snapshot(30.3)['players'][1]['status'],'CONNECTED')
 def test_idle_stale_and_update_rate_are_distinct(self):
  s,r,m=self.make();addr=('127.0.0.1',1)
  s.receive(f'HELLO|1|{TOKEN}|build|sector|bubble|p1|{"b"*32}'.encode(),addr,0);m.snapshot(0)
  p=s.peers['p1']
  for i in range(10):s.receive(f'STATE|1|{p.lease}|{i}|0|0|0|0|0|0'.encode(),addr,.05*i)
  snap=m.snapshot(.5);self.assertEqual(snap['players'][0]['updates_hz'],20);self.assertEqual(snap['players'][0]['pose'],'live')
  self.assertEqual(m.snapshot(1.3)['players'][0]['pose'],'stale')
  s.receive(f'IDLE|1|{p.lease}|10'.encode(),addr,1.4)
  self.assertEqual(m.snapshot(1.5)['players'][0]['pose'],'idle / paused')
 def test_invalid_and_probe_packets_do_not_claim_slots(self):
  s,r,m=self.make();addr=('127.0.0.1',1)
  s.receive(f'PING|1|{TOKEN}|{"b"*32}'.encode(),addr,0);s.receive(b'junk',addr,0)
  snap=m.snapshot(1);self.assertEqual(snap['connected'],0);self.assertEqual(snap['players'][0]['status'],'WAITING')
 def test_render_is_english_clipped_and_strips_control_sequences(self):
  s,r,m=self.make();snap=m.snapshot(1,[{'kind':'ZeroTier','interface':'zt\x1bBAD','ip':'10.1.1.1'}])
  text=render(snap,60);self.assertNotIn('\x1b',text);self.assertTrue(all(len(l)<=60 for l in text.splitlines()))
  self.assertIn('ZeroTier',text);self.assertIn('WAITING',text);self.assertIn('viewer',render(snap,100,True))
 def test_revision_age_resets_when_host_changes(self):
  s,r,m=self.make();s.world=(1,);s.world_revision=2;s.world_owner='old'
  m.snapshot(1);self.assertEqual(m.snapshot(3)['streams']['world']['age'],2)
  s.world_owner='new';self.assertEqual(m.snapshot(4)['streams']['world']['age'],0)
  s.world=None;self.assertIsNone(m.snapshot(5)['streams']['world']['age'])
 def test_stale_status_cannot_masquerade_as_a_running_server(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaisesRegex(RuntimeError,'status is unavailable'):attach(Path(d)/'missing.json')
 def test_real_udp_probe_and_packet_counters(self):
  import threading
  s=EconomySession(TOKEN,'build','bubble');r=Relay('127.0.0.1',0,s)
  stop=threading.Event()
  def worker():
   while not stop.wait(.005):r.step()
  thread=threading.Thread(target=worker);thread.start()
  try:
   self.assertTrue(probe({'token':TOKEN,'build':'build','bubble':'bubble','port':r.socket.getsockname()[1]}))
   self.assertEqual(len(s.peers),0);self.assertEqual(r.received_packets,1);self.assertEqual(r.sent_packets,1)
  finally:stop.set();thread.join();r.close()


class CombatSnapshotTests(unittest.TestCase):
 """Sanitized, read-only combat diagnostics of a co-op relay (D01/D02)."""
 WORLD=[100,100,0,64.5,30,0,100,100,100,100,200,1,1,0,0,0,0]
 def setUp(self):
  from x4lan.coop import CoopSession
  from test_host import TOKEN as T,P1,P2,hello
  self.P1,self.P2=P1,P2
  self.s=s=CoopSession(T,'test','bubble')
  self.a=s.receive(hello(),P1,0)[0].decode().split('|')[3]
  self.b=s.receive(hello('p2','2'*32),P2,0)[0].decode().split('|')[3]
  s.receive(f'WORLD|1|{self.a}|1|{"|".join(map(str,self.WORLD))}'.encode(),P1,.05)
  s.receive(f'CREW|1|{self.a}|1|1|1'.encode(),P1,.08);s.receive(f'CREW|1|{self.b}|1|2|2'.encode(),P2,.1)
  s.snapshots(.12);s.receive(f'HACK|1|{self.b}|2|{s.health_gen[2]}'.encode(),P2,.13)
  s.receive(f'HEALTH|1|{self.b}|2|{s.health_gen[2]}|1|64.5|30.25'.encode(),P2,.14)
  s.receive(f'SHOT|1|{self.b}|2|{s.health_gen[2]}|1|{s.health_gen[1]}|1|3|weapon_ter_s_plasma_01_mk1_macro:40.000'.encode(),P2,.15)
  relay=SimpleNamespace(session=s,socket=SimpleNamespace(getsockname=lambda:('127.0.0.1',45841)),received_packets=0,sent_packets=0,send_errors=0)
  self.m=Monitor(relay,0)
 def test_health_generations_live_state_and_the_shot_ledger_are_reported(self):
  c=self.m.snapshot(.2)['combat'];s=self.s
  self.assertEqual([(h['ship'],h['generation'],h['current'],h['live']) for h in c['health']],
                   [(1,s.health_gen[1],True,False),(2,s.health_gen[2],True,True)])
  self.assertEqual(c['health'][1]['frame'],{'seq':1,'hull_percent':64.5,'shield_percent':30.25,'age':.06})
  self.assertIsNone(c['health'][0]['frame']);self.assertIsNone(c['health'][0]['last_seen_age'])
  self.assertEqual(c['shots'],[{'shooter':2,'shooter_gen':s.health_gen[2],'victim':1,'victim_gen':s.health_gen[1],
                                'hits':3,'seq':1,'weapon':'weapon_ter_s_plasma_01_mk1_macro:40.000','age':.05}])
  self.assertEqual((c['shot_rows'],c['projectiles']),(1,[]))
  self.assertIn('CONNECTED',render(self.m.snapshot(.21)))     # the terminal view ignores the new section
 def test_no_lease_nonce_or_token_appears(self):
  from test_host import TOKEN as T
  text=json.dumps(self.m.snapshot(.2))
  for secret in (T,self.a,self.b,'2'*32,'1'*32):self.assertNotIn(secret,text)
 def test_the_snapshot_never_advances_a_generation(self):
  s=self.s;before=dict(s.health_gen)
  s.crew[self.b]=(9,0,0)                      # the guest left its ship; no relay call has rekeyed yet
  c=self.m.snapshot(.2)['combat']
  self.assertEqual(s.health_gen,before);self.assertFalse(c['health'][1]['current'])
 def test_shot_rows_are_bounded_newest_first(self):
  from x4lan.monitor import MAX_SHOT_ROWS
  for i in range(MAX_SHOT_ROWS+8):
   self.s.shots[(2,1,3,1000+i)]=dict(lease=self.b,seq=1,hits=i,at=.1+i/1000,fields=['x:1.000'])
  c=self.m.snapshot(.2)['combat']
  self.assertEqual((len(c['shots']),c['shot_rows']),(MAX_SHOT_ROWS,MAX_SHOT_ROWS+9))
  self.assertNotIn(0,[r['hits'] for r in c['shots'] if r['victim']==3])
 def test_economy_relays_keep_their_snapshot_shape(self):
  s=EconomySession(TOKEN,'build','bubble')
  relay=SimpleNamespace(session=s,socket=SimpleNamespace(getsockname=lambda:('127.0.0.1',45841)),received_packets=0,sent_packets=0,send_errors=0)
  self.assertNotIn('combat',Monitor(relay,0).snapshot(1))
