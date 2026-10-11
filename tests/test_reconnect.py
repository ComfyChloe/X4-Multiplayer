"""Session recovery retains identity and rejects stale endpoints/transactions."""
from collections import deque
from pathlib import Path
import unittest
from x4lan.economy import EconomySession
from test_host import TOKEN, P1, P2, hello
from test_game_adapter import LuaRuntime, MOCK
from test_bubble_adapter import EXTRA
from test_economy import SNAP

UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'

class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.s=EconomySession(TOKEN,'test','bubble')
        self.a=self.s.receive(hello(),P1,0)[0].decode().split('|')[3]
        self.b=self.s.receive(hello('p2','2'*32),P2,0)[0].decode().split('|')[3]
    def resume(self,now=31,lease=None,old='1'*32,new='3'*32,endpoint=('127.0.0.1',19999)):
        return self.s.receive(f'RESUME|1|{TOKEN}|test|sector_macro|bubble|p1|{old}|{lease or self.a}|{new}'.encode(),endpoint,now)
    def test_expired_session_resumes_with_sequence_and_lease_preserved(self):
        sector=self.s.sector
        self.assertEqual(sector,'sector_macro')
        self.s.receive(f'STATE|1|{self.a}|90|0|0|0|0|0|0'.encode(),P1,1)
        reply=self.resume(32)
        self.assertEqual(reply,[f'WELCOME|1|{"3"*32}|{self.a}|20'.encode()])
        self.assertEqual(self.s.peers['p1'].seq,90)
        self.s.receive(f'STATE|1|{self.a}|91|0|0|0|0|0|0'.encode(),P1,32.1)
        self.assertEqual(self.s.peers['p1'].seq,90) # Old endpoint cannot resume traffic.
        self.s.receive(f'STATE|1|{self.a}|89|0|0|0|0|0|0'.encode(),('127.0.0.1',19999),32.2)
        self.assertEqual(self.s.peers['p1'].seq,90)
        self.assertEqual(self.resume(32.3),reply) # Lost WELCOME can be retried.
    def test_resume_requires_both_previous_credentials_and_no_new_slot_owner(self):
        self.assertEqual(self.resume(1,lease='f'*32),[])
        self.assertEqual(self.resume(1,old='f'*32),[])
        self.s.expire(31)
        self.s.receive(hello('p1','4'*32),P1,32)
        self.assertEqual(self.resume(33),[])
    def test_new_relay_explicitly_refuses_old_world(self):
        self.s=EconomySession(TOKEN,'test','bubble')
        self.assertEqual(self.resume(),[f'RESET|1|{"3"*32}'.encode()])
    def test_uncertain_order_is_never_reexecuted_after_resume(self):
        self.s.receive(f'ECON|1|{self.a}|1|1|1|{SNAP}'.encode(),P1,.1)
        request='e'*32
        self.s.receive(f'ORDER|1|{self.b}|{request}|{self.a}|m1|energycells,10,1600'.encode(),P2,.2)
        self.resume(31)
        self.assertEqual(self.s.orders[request]['result'],'UNCERTAIN')
        self.assertFalse(any(packet.startswith(b'ORDER_EXEC') for _,packet in self.s.snapshots(31.1)))

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class AdapterReconnectTests(unittest.TestCase):
    def test_dropped_replies_and_long_outage_resume_same_world_automatically(self):
        for both_directions,flap,initial in ((False,False,False),(True,False,False),(True,True,False),(True,False,True)):
            with self.subTest(both_directions=both_directions,flap=flap,initial=initial):
                lua=LuaRuntime(unpack_returned_tuples=True);lua.execute(MOCK+EXTRA)
                lua.execute((UI/'client.lua').read_text())
                relay=EconomySession(TOKEN,'test','bubble');inbox=deque()
                connected=True;drop=initial;address=[('127.0.0.1',10001)]
                failed_resume=[False];raise_receive=[False]
                def send(packet):
                    if connected:
                        replies=relay.receive(packet.encode(),address[0],lua.globals().now)
                        if not drop: inbox.extend(p.decode() for p in replies)
                        if flap and not drop and packet.startswith('RESUME|') and not failed_resume[0]:
                            failed_resume[0]=True;raise_receive[0]=True
                    return True
                def receive():
                    if raise_receive[0]:
                        raise_receive[0]=False;inbox.clear()
                        raise RuntimeError('Socket failed after relay accepted RESUME, before WELCOME')
                    return inbox.popleft() if inbox else None
                lua.globals().wireSend=send
                lua.globals().wireReceive=receive
                lua.execute('''X4LAN_TRANSPORT.open=function() return {
                  send=function(self,p) return wireSend(p) end,
                  receive=function() return wireReceive() end,
                  close=function() closeCount=closeCount+1 end} end''')
                lua.execute((UI/'bubble_game.lua').read_text())
                lua.execute("events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate()")
                lease=relay.peers['p1'].lease
                for tick in range(1,701):
                    now=10+tick/10;lua.globals().now=now
                    drop=(now<55 if initial else 11<now<55);connected=not (both_directions and drop)
                    if now==55: address[0]=('127.0.0.1',10002)
                    lua.execute('frames.onUpdate()')
                    for _,packet in relay.snapshots(now):
                        if not drop: inbox.append(packet.decode())
                if initial:self.assertNotEqual(relay.peers['p1'].lease,lease)
                else:self.assertEqual(relay.peers['p1'].lease,lease)
                self.assertEqual(relay.peers['p1'].address,address[0])
                self.assertFalse(lua.eval('X4LAN_BUBBLE_API.state.failed'))
                self.assertEqual(lua.eval('X4LAN_BUBBLE_API.state.client.lease'),relay.peers['p1'].lease)
                self.assertGreater(lua.globals().closeCount,0)
                self.assertEqual(lua.eval("countRequest('SetupHost')"),1)
                self.assertEqual(failed_resume[0],flap)
