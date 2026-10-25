"""Native mission facts are authoritative; packet receipt never grants a reward."""
import unittest
import zlib
from pathlib import Path
from x4lan.missions import parse_record, advances
import test_coop
from test_host import P1, P2, FOREIGN
from test_game_adapter import LuaRuntime
import test_coop_adapter
UI=test_coop_adapter.UI


def payload(state='O', done=0, total=2, title='Destroy hostile ships', description='Ordinary mission | ä\nSecond line', reward=2500000):
    texts=[title, description, 'Destroy matching ships']
    return ','.join(['quota',state,str(done),str(total),str(reward),'argon']+[s.encode().hex() for s in texts])


def packets(lease, epoch, data, rev=1, ident=1):
    chunks=[data[i:i+500] for i in range(0,len(data),500)]
    return [f'MSTATE|1|{lease}|{epoch}|{ident}|{rev}|{i}|{len(chunks)}|{zlib.adler32(data.encode())}|{c}' for i,c in enumerate(chunks,1)]


class MissionRelayTests(unittest.TestCase):
    def setUp(self):
        f=test_coop.CoopTests();f.setUp();self.s,self.a,self.b=f.s,f.a,f.b
    def put(self, data=None, rev=1, ident=1, address=P1, lease=None):
        for p in packets(lease or self.a,self.a,data or payload(),rev,ident):self.s.receive(p.encode(),address,.1)
    def request(self, action='ACCEPT', revision=1, address=P2, lease=None):
        self.s.receive(f'MREQ|1|{lease or self.b}|{self.a}|1|{revision}|{action}'.encode(),address,.2)
    def frames(self, kind, now=.5):
        return [(a,p) for a,p in self.s.snapshots(now) if p.startswith(kind.encode()+b'|')]
    def test_guest_and_foreign_cannot_publish_completion(self):
        self.put(payload('C',2),address=P2,lease=self.b);self.put(address=FOREIGN)
        self.assertFalse(self.s.missions.records)
    def test_out_of_order_chunks_atomic_loss_retry_and_unicode(self):
        data=payload(description='ä,|\n'*600);ps=packets(self.a,self.a,data)
        for p in reversed(ps[1:]):self.s.receive(p.encode(),P1,.1)
        self.assertFalse(self.s.missions.records)
        for p in ps:self.s.receive(p.encode(),P1,.2)
        self.assertEqual(self.s.missions.records[1]['row']['description'],'ä,|\n'*600)
    def test_changed_chunk_checksum_and_conflicting_metadata_do_not_commit(self):
        data=payload(description='A'*1000);ps=packets(self.a,self.a,data)
        self.s.receive((ps[0][:-1]+'b').encode(),P1,.1)
        for p in ps[1:]:self.s.receive(p.encode(),P1,.1)
        self.assertFalse(self.s.missions.records)
    def test_only_guest_requests_current_registered_offer(self):
        self.request();self.assertFalse(self.s.missions.requests)
        self.put()
        for args in [dict(address=FOREIGN),dict(address=P1,lease=self.a),dict(revision=2),dict(action='ABORT')]:self.request(**args)
        self.assertFalse(self.s.missions.requests)
        self.request();self.assertEqual(len(self.frames('MREQ')),1)
        self.assertEqual(self.s.missions.records[1]['row']['state'],'O')
    def test_completion_can_skip_lost_intermediate_revisions_without_double_reward(self):
        self.put();self.request();self.put(payload('C',2),3)
        self.assertEqual(self.s.missions.records[1]['row']['state'],'C')
        self.assertFalse(self.frames('MREQ'))
        for data,rev in [(payload('A',1),4),(payload('C',2),5),(payload(),1)]:self.put(data,rev)
        self.assertEqual(self.s.missions.records[1]['rev'],3)
        self.assertEqual(len(self.frames('MSTATE',.8)),1)
    def test_metadata_mutation_and_regressing_progress_rejected(self):
        self.put(payload('A',1))
        for data in [payload('A',0),payload('A',1,title='Another contract'),payload('A',1,reward=999)]:self.put(data,2)
        self.assertEqual(self.s.missions.records[1]['rev'],1)
    def test_accept_retries_until_native_transition_and_abort_only_active(self):
        self.put();self.request();self.assertTrue(self.frames('MREQ'))
        self.assertTrue(self.frames('MREQ',.8));self.put(payload('A'),2)
        self.assertFalse(self.frames('MREQ',1.1));self.request('ABORT',2)
        self.assertTrue(self.frames('MREQ',1.4))
    def test_bounded_ids_and_malformed_records(self):
        self.put(ident=257);self.assertFalse(self.s.missions.records)
        for bad in [payload('C',0),payload('O',1),payload(total=0),payload(description='\x00'),payload().replace('44657374726f79','ff',1)]:
            with self.assertRaises((ValueError,UnicodeError)):parse_record(bad)
    def test_new_progress_is_prioritized_over_retained_history(self):
        for ident in range(1,100):self.put(payload('A'),ident=ident)
        self.s.missions.changed.clear()
        self.put(payload('A',1),rev=2,ident=99)
        frame=self.frames('MSTATE')[0][1].decode().split('|')
        self.assertEqual(frame[4:6],['99','2'])

    def test_expired_host_does_not_publish_cached_facts(self):
        self.put();self.s.expire(31);self.assertFalse(self.frames('MSTATE',31))


@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class MissionLuaTests(unittest.TestCase):
    def make(self):
        l=LuaRuntime(unpack_returned_tuples=True)
        for name in ['universe_protocol.lua','mission_protocol.lua']:l.execute((UI/name).read_text())
        return l
    def test_python_lua_contract_unicode_and_invalid_utf8(self):
        l=self.make();parse=l.eval('X4LAN_MISSION_PROTOCOL.parse')
        for data in [payload(),payload('C',2),payload(description='é𐀀\n|,\t')]:
            row=parse(data);self.assertIsNotNone(row)
            for k,v in parse_record(data).items():self.assertEqual(row[k],v)
        for code in ['ff','c080','eda080','f4908080','e280','00','7f']:
            data=payload().split(',');data[6]=code;self.assertIsNone(parse(','.join(data)))
    def test_lua_reassembly_loss_reorder_epoch_and_terminal_fence(self):
        l=self.make();a=l.eval('X4LAN_MISSION_PROTOCOL.assembly()');epoch='a'*32
        ps=packets('b'*32,epoch,payload(description='X'*1500))
        for p in reversed(ps[1:]):self.assertIsNone(a.add(a,l.table_from(p.split('|')),1))
        r=a.add(a,l.table_from(ps[0].split('|')),1);self.assertEqual(r.row.state,'O')
        self.assertIsNone(a.add(a,l.table_from(packets('b'*32,'c'*32,payload('A'),2)[0].split('|')),2))
        r=a.add(a,l.table_from(packets('b'*32,epoch,payload('C',2,description='X'*1500),3)[0].split('|')),3)
        # All chunks are necessary even for a terminal update.
        self.assertIsNone(r)
        for p in packets('b'*32,epoch,payload('C',2,description='X'*1500),3)[1:]:r=a.add(a,l.table_from(p.split('|')),3)
        self.assertEqual(r.row.state,'C')
    def test_guest_mirror_is_retried_until_md_ack_and_never_pays(self):
        l=test_coop_adapter.CoopAdapterTests().make('p2')
        l.execute('X4LAN_CONFIG.faction_missions=true;X4LAN_BUBBLE_API.health={mdStamp="wall",mdAt=11,mdAge=10}; now=11')
        for name in ['universe_protocol.lua','mission_protocol.lua','mission_game.lua']:l.execute((UI/name).read_text())
        for p in packets('b'*32,'a'*32,payload('A')):
            l.globals().packet=l.table_from(p.split('|'));l.execute('X4LAN_BUBBLE_API.missions.receive(packet,now)')
        l.execute('''local q=X4LAN_BUBBLE_API.missions
         q.frame(now);assert(countRequest('Setup')>=1)
         events['X4LAN_Missions.Ready'](nil,string.rep('a',32));q.frame(now)
         assert(countRequest('Mirror')==1)
         now=11.3;q.frame(now);assert(countRequest('Mirror')==2)
         events['X4LAN_Missions.Mirrored'](nil,'1|1');now=11.4;q.frame(now)
         assert(q.pending[1]==nil)
        ''')
    def test_stock_trial_remains_separate_and_feature_default_disabled(self):
        self.assertIn('faction_missions = false',(UI/'config.lua').read_text())
        self.assertNotIn('reward_player',(UI.parent/'md/x4lan_missions.xml').read_text())

    def test_partial_records_never_cross_epochs(self):
        l=self.make();a=l.eval('X4LAN_MISSION_PROTOCOL.assembly()');data=payload(description='X'*1000)
        first=packets('b'*32,'a'*32,data);second=packets('b'*32,'c'*32,data)
        self.assertIsNone(a.add(a,l.table_from(first[0].split('|')),1))
        for p in second[1:]:self.assertIsNone(a.add(a,l.table_from(p.split('|')),1))
        self.assertIsNone(a.records[1])

    def test_host_only_queues_live_revision_and_fresh_native_clock(self):
        l=test_coop_adapter.CoopAdapterTests().make('p1')
        l.execute('X4LAN_CONFIG.faction_missions=true;X4LAN_BUBBLE_API.health={mdStamp="wall",mdAt=11,mdAge=10};now=11')
        for name in ['universe_protocol.lua','mission_protocol.lua','mission_game.lua']:l.execute((UI/name).read_text())
        l.globals().native_payload=payload()
        l.execute('''local q=X4LAN_BUBBLE_API.missions
         q.frame(now);events['X4LAN_Missions.Ready'](nil,client.lease)
         q.records[1]={id=1,rev=3,row=X4LAN_MISSION_PROTOCOL.parse(native_payload),payload=native_payload,digest=X4LAN_UNIVERSE_PROTOCOL.checksum({native_payload})}
         local function req(rev,epoch)
          return {'MREQ','1',client.lease,epoch or client.lease,'1',rev,'ACCEPT'}
         end
         q.receive(req('2'),now);q.receive(req('3',string.rep('c',32)),now);assert(not q.requests[1])
         q.receive(req('3'),now);q.frame(now);assert(countRequest('Command')==1)
         now=12;q.frame(now);assert(countRequest('Command')==1)
        ''')
