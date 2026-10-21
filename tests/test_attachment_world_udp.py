"""Real Linux transport/relay/client path with simulated X4 readbacks, not two games."""
import ctypes
import unittest
import lupa.luajit21
from x4lan.host import Relay
from x4lan.universe import UniverseSession
import test_attachment_world as fixture


class AttachmentWorldUDPTests(unittest.TestCase):
    def test_real_transport_routes_native_state_into_guest_pose_admission(self):
        ctypes.CDLL(lupa.luajit21.__file__,mode=ctypes.RTLD_GLOBAL)
        session=UniverseSession('a'*32,'world1-test','bubble',attachments=True)
        relay=Relay('127.0.0.1',0,session);self.addCleanup(relay.close)
        peers=[]
        for index,slot in enumerate(('p1','p2')):
            lua=fixture.AttachmentWorldTests().make(slot)
            lua.globals().X4LAN_NATIVE_PATH=str(fixture.UI.parent/'native/x4lan_net.so')
            for name in ('transport.lua','client.lua'):lua.execute((fixture.UI/name).read_text())
            lua.globals().port=relay.socket.getsockname()[1]
            lua.globals().nonce=str(index+1)*32
            lua.execute('''
net=X4LAN_TRANSPORT.open('127.0.0.1',port)
local original=client
client=X4LAN_CLIENT.new({token=string.rep('a',32),build='world1-test',bubble='bubble',
 sector='cluster_14_sector001_macro',slot=X4LAN_CONFIG.slot},net,nonce)
client.receiveExtension=original.receiveExtension
X4LAN_BUBBLE_API.state.client=client;X4LAN_BUBBLE_API.state.transport=net
''')
            net=lua.globals().net;self.addCleanup(net.close,net);peers.append(lua)
        host,guest=peers;clock=10
        host.execute('positions[101].x=1500')
        def drive(frames):
            nonlocal clock
            for _ in range(frames):
                clock+=.051
                for lua in peers:lua.globals().now=clock
                if 'p1' in session.peers:
                    guest.globals().authority=session.peers['p1'].lease
                    guest.execute("X4LAN_BUBBLE_API.identity.rows.N1={code='NAT-101',epoch=authority,at=now}")
                relay.step(clock)
                for lua in peers:lua.execute('frames.onUpdate()')
                relay.step(clock)
        drive(100)
        self.assertEqual(len(session.peers),2)
        self.assertFalse(host.eval('U.failed'),host.eval(r"table.concat(logs,'\n')"))
        self.assertFalse(guest.eval('U.failed'),guest.eval(r"table.concat(logs,'\n')"))
        self.assertEqual(guest.eval('positions[101].x'),1500)
        self.assertTrue(session.attachments.live)
        # Host changes the real-readback double to stored; the guest remains free.
        # The observation must fence pose before physical transitions are integrated.
        host.execute("attachmentStates[101]='STORED';docks[101]=201")
        drive(10)
        guest.execute('positions[101].x=999')
        drive(10)
        self.assertEqual(guest.eval('positions[101].x'),999)
        host.execute("attachmentStates[101]='FREE';docks[101]=nil")
        drive(10)
        self.assertEqual(guest.eval('positions[101].x'),1500)
        # No new host observation: successful relay retries cannot extend guest TTL.
        host.execute("attachmentStates[101]='TRANSIT';docks[101]=201")
        drive(45)
        guest.execute('positions[101].x=999')
        drive(5)
        self.assertEqual(guest.eval('positions[101].x'),999)
        self.assertFalse(guest.eval('U.failed'))


if __name__=='__main__':unittest.main()
