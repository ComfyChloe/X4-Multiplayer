"""Seat transitions through real Lua protocol + Python relay; engine calls are doubles."""
from collections import deque
from pathlib import Path
import unittest
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA
from x4lan.economy import EconomySession

UI = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime, 'lupa.luajit21 not installed')
class DockingSessionTests(unittest.TestCase):
    def test_ninety_seconds_on_foot_keeps_lease_world_and_trade_pump(self):
        for slot in ('p1', 'p2'):
            with self.subTest(slot=slot):
                lua = LuaRuntime(unpack_returned_tuples=True)
                lua.execute(MOCK + EXTRA)
                lua.execute((UI / 'client.lua').read_text())
                relay = EconomySession('a'*32, 'test', 'bubble')
                inbox = deque()
                address = ('127.0.0.1', 10001)
                def send(packet):
                    inbox.extend(p.decode() for p in relay.receive(packet.encode(), address, lua.globals().now))
                    return True
                lua.globals().wireSend = send
                lua.globals().wireReceive = lambda: inbox.popleft() if inbox else None
                lua.globals().X4LAN_CONFIG.slot = slot
                lua.execute('''
X4LAN_TRANSPORT.open=function() return {
 send=function(self,packet) return wireSend(packet) end,
 receive=function() return wireReceive() end,
 close=function() closeCount=closeCount+1 end} end
''')
                lua.execute((UI / 'bubble_game.lua').read_text())
                lua.execute("events['X4LAN.Loaded']();now=10;frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate()")
                lease = relay.peers[slot].lease
                lua.execute('''
controlled=0;tradeFrames=0
X4LAN_BUBBLE_API.trade={frame=function() tradeFrames=tradeFrames+1 end,receive=function() return false end}
''')
                for tick in range(1, 901):
                    now = 10 + tick / 10
                    lua.globals().now = now
                    lua.execute("events['X4LAN_Bubble.World'](nil,'100|100|0|100|100|0|100|100|100|100|300|1|1|1|0|0|0');frames.onUpdate()")
                    for _, packet in relay.snapshots(now):
                        inbox.append(packet.decode())
                    self.assertIn(slot, relay.peers)
                    self.assertEqual(relay.peers[slot].lease, lease)
                self.assertEqual(lua.globals().tradeFrames, 900)
                self.assertEqual(lua.globals().closeCount, 0)
                self.assertEqual(relay.peers[slot].pose[:3], (100, -9800, 300))
                if slot == 'p1':
                    self.assertEqual(relay.world_owner, lease)
                    self.assertGreater(relay.world_revision, 800)
                lua.execute('controlled=11;now=100.1;frames.onUpdate()')
                self.assertEqual(relay.peers[slot].lease, lease)
                self.assertFalse(lua.eval('X4LAN_BUBBLE_API.state.failed'))
