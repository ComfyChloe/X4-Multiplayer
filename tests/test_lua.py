"""Optional: requires lupa with its luajit21 module; no X4 installation needed."""
from pathlib import Path
import ctypes
import unittest

from x4lan.host import Relay, Session

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "extension" / "x4_lan_lab" / "ui"
TOKEN = "a"*32


@unittest.skipUnless(LuaRuntime, "lupa.luajit21 not installed")
class LuaTests(unittest.TestCase):
    def runtime(self):
        import lupa.luajit21
        ctypes.CDLL(lupa.luajit21.__file__, mode=ctypes.RTLD_GLOBAL)
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.globals().X4LAN_NATIVE_PATH = str(UI.parent / "native/x4lan_net.so")
        for name in ("transport.lua", "client.lua"):
            lua.execute((UI / name).read_text())
        return lua

    def test_all_lua_files_parse_under_luajit(self):
        lua = self.runtime()
        for path in UI.glob("*.lua"):
            lua.execute("assert(loadstring(...))", path.read_text())

    def test_linux_native_clients_exchange_poses_through_real_udp(self):
        session = Session(TOKEN, "test", "bubble")
        relay = Relay("127.0.0.1", 0, session)
        clients, transports, runtimes = [], [], []
        try:
            for slot, nonce in (("p1", "1"*32), ("p2", "2"*32)):
                lua = self.runtime()
                runtimes.append(lua)
                transport = lua.globals().X4LAN_TRANSPORT.open("127.0.0.1", relay.socket.getsockname()[1])
                transports.append(transport)
                config = lua.table_from(dict(token=TOKEN, build="test", bubble="bubble", sector="sector_macro", slot=slot))
                clients.append(lua.globals().X4LAN_CLIENT.new(config, transport, nonce))
            for tick in range(60):
                now = tick * .05
                for index, client in enumerate(clients):
                    pose = runtimes[index].table_from((index*100+tick, 0, 0, 0, 0, 0))
                    client.update(client, now, pose)
                relay.step(now)
            self.assertEqual(len(session.peers), 2)
            self.assertGreater(session.accepted_states, 50)
            first = clients[0].render(clients[0], 2.95)
            second = clients[1].render(clients[1], 2.95)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertGreater(first[1], 100)
            self.assertLess(second[1], 100)
            # Reopen transport in the SAME Lua VM: no cdef redefinition error.
            transports[0].close(transports[0])
            replacement = runtimes[0].globals().X4LAN_TRANSPORT.open("127.0.0.1", relay.socket.getsockname()[1])
            replacement.close(replacement)
        finally:
            for transport in transports:
                transport.close(transport)
            relay.close()

    def test_client_filters_packets_and_interpolates_angle_wrap(self):
        lua = self.runtime()
        lua.execute('''
            local cfg = {token=string.rep("a",32), build="test", bubble="bubble", sector="sector", slot="p1"}
            client = X4LAN_CLIENT.new(cfg, {}, string.rep("1",32))
            local own, remote = string.rep("b",32), string.rep("c",32)
            client:receive("WELCOME|1|"..string.rep("1",32).."|"..own.."|20", 0)
            client:receive("PEER|1|wrong|1|"..remote.."|1|999|0|0|0|0|0", 0)
            assert(#client.samples == 0)
            client:receive("PEER|1|"..own.."|1|"..remote.."|1|0|0|0|3.1|0|0", 1)
            client:receive("PEER|1|"..own.."|2|"..remote.."|2|10|0|0|-3.1|0|0", 1.1)
            client:receive("PEER|1|"..own.."|1|"..remote.."|3|999|0|0|0|0|0", 1.2)
            assert(#client.samples == 2)
            local pose = client:render(1.15)
            assert(math.abs(pose[1] - 5) < 0.001)
            assert(math.abs(math.abs(pose[4])-math.pi) < 0.001)
            assert(client:render(2) == nil)
            client:receive("PEER|1|"..own.."|3|"..remote.."|3|nan|0|0|0|0|0", 1.2)
            assert(#client.samples == 2)
            client:receive("EMPTY|1|"..own.."|4", 1.3)
            assert(client:render(1.3) == nil)
        ''')

    def test_explicit_attachment_session_drains_world_bursts_before_observations_expire(self):
        lua=self.runtime()
        lua.execute('''
local queue,head,latest={},1,{}
local lease=string.rep('b',32)
local transport={send=function() return true end,receive=function()
 local packet=queue[head];if packet then head=head+1 end;return packet
end}
local cfg={token=string.rep('a',32),build='test',bubble='bubble',sector='sector',slot='p2',attachment_sync=true}
local client=X4LAN_CLIENT.new(cfg,transport,string.rep('1',32))
client:receive('WELCOME|1|'..string.rep('1',32)..'|'..lease..'|20',0)
client.receiveExtension=function(_,f,now)
 if f[1]=='UATTACH' then latest[tonumber(f[7])]={sequence=tonumber(f[6]),at=now};return true end
 if f[1]=='IDENT' then return true end
 return false
end
-- Two mixed relay batches per100ms; the old32/frame consumer accumulates
-- obsolete observations even without loss or engine callback overhead.
for step=1,120 do
 local now=step*.05
 if step%2==1 then
  for ship=1,128 do
   queue[#queue+1]='UATTACH|1|'..lease..'|'..lease..'|1|'..step..'|'..ship..'|0|FREE'
   if ship<=32 then queue[#queue+1]='IDENT|1|'..lease..'|'..lease..'|'..step..'|N|'..ship..'|NAT-001|4e5043|1|1' end
  end
  queue[#queue+1]='EMPTY|1|'..lease..'|'..step
  -- A foreign recipient never reaches the extension through the larger budget.
  queue[#queue+1]='UATTACH|1|'..string.rep('c',32)..'|'..lease..'|1|999999|129|0|FREE'
 end
 assert(client:update(now,nil),'session heartbeat starved by world traffic')
 if step%2==0 then
  for ship=1,128 do assert(latest[ship] and latest[ship].sequence==step-1,'world observation backlog') end
 end
end
assert(not latest[129],'foreign recipient bypassed the lease gate')
''')

    def test_receive_work_remains_bounded_and_requires_explicit_attachment_opt_in(self):
        lua=self.runtime()
        lua.execute('''
for _,flag in ipairs({false,'true',true}) do
 local reads=0
 local transport={send=function() return true end,receive=function() reads=reads+1;return 'INVALID' end}
 local cfg={token=string.rep('a',32),build='test',bubble='bubble',sector='sector',slot='p2',attachment_sync=flag}
 local client=X4LAN_CLIENT.new(cfg,transport,string.rep('1',32))
 assert(client:update(0,nil))
 assert(reads==(flag==true and 256 or 32),'receive flood exceeded fixed opt-in budget')
end
''')

    def test_native_argument_limits_and_idempotent_close(self):
        lua = self.runtime()
        lua.execute('''
            assert(not pcall(X4LAN_TRANSPORT.open, "localhost", 45841))
            assert(not pcall(X4LAN_TRANSPORT.open, "127.0.0.1", 0))
            assert(not pcall(X4LAN_TRANSPORT.open, "127.0.0.1", 12.5))
            assert(not pcall(X4LAN_TRANSPORT.open, "127.0.0.1", 0/0))
            local conn = X4LAN_TRANSPORT.open("127.0.0.1", 45842)
            assert(conn:receive() == nil)
            assert(conn:send(string.rep("x", 769)) == false)
            conn:close(); conn:close()
            assert(conn:send("x") == false)
            assert(conn:receive() == nil)
            assert(not pcall(conn.send, {}, "x"))
        ''')

    def test_client_waits_thirty_seconds_but_stale_motion_is_not_rendered(self):
        lua = self.runtime()
        lua.execute('''
            local cfg={token=string.rep('a',32),build='test',bubble='bubble',sector='sector',slot='p1'}
            local transport={send=function() return true end,receive=function() return nil end}
            local nonce,own,remote=string.rep('1',32),string.rep('b',32),string.rep('c',32)
            local client=X4LAN_CLIENT.new(cfg,transport,nonce)
            client:receive('WELCOME|1|'..nonce..'|'..own..'|20',0)
            client:receive('PEER|1|'..own..'|1|'..remote..'|1|0|0|0|0|0|0',0)
            assert(client:update(5,nil) and client:update(29.999,nil))
            assert(client:render(5)==nil)
            client:receive('EMPTY|1|wrong|2',29.999)
            assert(not client:update(30,nil))
        ''')

    def test_valid_server_response_after_short_outage_preserves_client(self):
        lua = self.runtime()
        lua.execute('''
            local cfg={token=string.rep('a',32),build='test',bubble='bubble',sector='sector',slot='p1'}
            local transport={send=function() return true end,receive=function() return nil end}
            local nonce,own=string.rep('1',32),string.rep('b',32)
            local client=X4LAN_CLIENT.new(cfg,transport,nonce)
            client:receive('WELCOME|1|'..nonce..'|'..own..'|20',0)
            assert(client:update(20,nil))
            client:receive('EMPTY|1|'..own..'|1',20)
            assert(client:update(49.999,nil) and client.lease==own)
            assert(not client:update(50,nil))
        ''')


if __name__ == "__main__":
    unittest.main()
