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
