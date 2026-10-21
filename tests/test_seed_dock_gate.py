"""Startup/event routing uses the real saved-dock importer; no native engine claim."""
from pathlib import Path
import unittest
import test_seed_dock_import as importer_fixture

ROOT=Path(__file__).resolve().parents[1]


class DockGateTests(unittest.TestCase):
    def make(self):
        lua=importer_fixture.DockImportTests().make()
        lua.execute('''
X4LAN_CONFIG={enabled=true,attachment_sync=true,seed_binding=true,seed_verified=true,test_profile='45841',seed_id=seed}
handlers={};requests={};now=1
function RegisterEvent(name,fn) handlers[name]=fn end
function AddUITriggeredEvent(screen,control,value)
 assert(screen=='X4LAN_SeedDocks' and control=='Read');requests[#requests+1]=value
end
function GetCurRealTime() return now end
function DebugError() end
function receive(kind,payload) handlers['X4LAN_SeedDocks.'..kind](nil,seed..'|'..payload) end
''')
        lua.execute((ROOT/'extension/x4_lan_lab/ui/seed_dock_game.lua').read_text())
        return lua

    def test_complete_native_registry_is_required_and_changed_live_query_revokes_it(self):
        self.make().execute('''
local D=X4LAN_SEED_DOCK_GATE
assert(not D.frame(1) and #requests==1 and requests[1].start==1)
receive('Begin','0|1|2|2')
receive('Row','0|1|0xb|0xa|0xa|dock_a_macro|ship_carrier_macro|1|0|0|0.001')
assert(not D.ready)
receive('Row','0|2|0xc|0xa|0xa|dock_a_macro|ship_carrier_macro|1|0|0|0.001')
receive('End','0|1|2|2|1');now=2
assert(D.frame(now) and D.ready and D.importer:index(11)==1)
now=3;assert(D.importer:query(1,'EXTERNAL',now,function() error('changed query cannot succeed') end))
assert(#requests==2 and requests[2].serial==1)
receive('Begin','1|1|1|2')
receive('Row','1|1|0xff|0xa|0xa|dock_a_macro|ship_carrier_macro|1|0|0|0.001')
assert(D.failed and not D.ready and not D.frame(4))
''')

    def test_unsolicited_or_missing_native_response_never_admits(self):
        self.make().execute('''
local D=X4LAN_SEED_DOCK_GATE
receive('Begin','0|1|2|2');assert(D.failed and not D.frame(1) and #requests==0)
''')
        self.make().execute('''
local D=X4LAN_SEED_DOCK_GATE
D.frame(1);assert(not D.ready);assert(not D.frame(7) and D.failed)
''')


if __name__=='__main__':unittest.main()
