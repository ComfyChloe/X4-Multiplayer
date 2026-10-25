"""Lua STATEHASH queue and capture-request adapter; engine/network doubles only."""
from pathlib import Path
import unittest
from test_game_adapter import LuaRuntime

UI = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui'


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ResyncAdapterTests(unittest.TestCase):
    def make(self, slot='p2'):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''
         sent={};logs={};now=10;frames=0
         RegisterEvent=function() end;DebugError=function(v) logs[#logs+1]=v end
         X4LAN_CONFIG={world_sync=true,slot='p2'}
         X4LAN_BUBBLE_API={state={client={lease=string.rep('b',32)},transport={send=function(self,p) sent[#sent+1]=p end}},
          universe={revision=4,latest={epoch=string.rep('a',32),revision=4,sector='cluster_14_sector001_macro'},
           receive=function() return false end,frame=function() frames=frames+1 end}}
        ''')
        lua.globals().X4LAN_CONFIG.slot = slot
        lua.execute((UI / 'universe_protocol.lua').read_text())
        lua.execute((UI / 'resync_game.lua').read_text())
        lua.execute('U=X4LAN_BUBBLE_API.universe;M=X4LAN_STATEHASH')
        return lua

    def sample(self, lua, frame=1):
        lua.globals().sample = lua.table_from({
            'complete': True, 'frame': frame, 'rng': '4' * 16, 'time': '5' * 16,
            'sectors': lua.table_from({
                'cluster_14_sector001_macro': lua.table_from({
                    'identity': '1' * 16, 'pose': '2' * 16, 'health': '3' * 16})
            })
        })
        lua.execute('M.capture(sample)')

    def test_complete_capture_is_queued_and_sent_on_adapter_frame(self):
        lua = self.make()
        self.sample(lua)
        lua.execute('''
         assert(#sent==0);U.frame(now);assert(#sent==2 and frames==1)
         assert(sent[1]:find('|cluster_14_sector001_macro|',1,true))
         assert(sent[2]:find('|global|-|-|-|',1,true))
         U.frame(now+.3);assert(#sent==2)
        ''')
        self.sample(lua, 2)
        lua.execute('U.frame(now+.3);assert(#sent==4)')

    def test_guest_epoch_and_sector_come_from_committed_baseline(self):
        lua = self.make()
        lua.execute("U.latest=nil")
        self.sample(lua)
        lua.execute('U.frame(now);assert(#sent==0 and M.latest)')
        lua.execute("U.latest={epoch=string.rep('c',32),sector='cluster_14_sector001_macro'};U.frame(now+.3)")
        sent = [lua.eval('sent')[i] for i in range(1, 3)]
        self.assertTrue(all(('|' + 'c' * 32 + '|') in packet for packet in sent))

    def test_host_request_is_epoch_checked_deduplicated_and_sector_scoped(self):
        lua = self.make('p1')
        lua.execute("U.wanted='cluster_14_sector001_macro';U.nextScan=100")
        packet = ['URESYNC', '1', 'b' * 32, 'b' * 32, '9', 'cluster_14_sector001_macro', '4', 'identity,pose']
        lua.globals().packet = lua.table_from(packet)
        lua.execute('U.receive(packet,now);assert(U.resync and U.nextScan==0)')
        lua.execute('U.resync=nil;U.nextScan=100;U.receive(packet,now);assert(not U.resync and U.nextScan==100)')
        lua.globals().packet = lua.table_from(packet[:3] + ['f' * 32] + packet[4:])
        lua.execute("packet[5]='10';U.receive(packet,now);assert(not U.resync)")
        lua.globals().packet = lua.table_from(packet)
        lua.execute("packet[5]='10';packet[6]='cluster_01_sector001_macro';U.receive(packet,now);assert(not U.resync)")
        lua.execute("packet[5]='11';packet[6]=U.wanted;packet[8]='hull';U.receive(packet,now);assert(not U.resync)")

    def test_fresh_existing_sender_reuses_capture(self):
        lua = self.make('p1')
        lua.execute("U.wanted='cluster_14_sector001_macro'")
        packet = ['URESYNC', '1', 'b' * 32, 'b' * 32, '9', 'cluster_14_sector001_macro', '4', 'health']
        lua.globals().packet = lua.table_from(packet)
        lua.execute('U.receive(packet,now);assert(U.resync)')
        lua.execute("U.sender={baseline={sector=U.wanted,revision=5}};U.frame(now);assert(not U.resync)")


if __name__ == '__main__':
    unittest.main()
