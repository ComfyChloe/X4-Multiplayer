"""Native API doubles for capture ordering and read-only content verification."""
from pathlib import Path
import unittest
from test_game_adapter import MOCK,LuaRuntime
UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class CaptureTests(unittest.TestCase):
    def make(self,extra=''):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK+'''
X4LAN_CONFIG={enabled=false,test_profile='45841',seed_capture=true,seed_id=string.rep('a',32),seed_content='ego_dlc_test:900,x4_lan_lab:8'}
GetExtensionList=function() return {{id='x4_lan_lab',version='0.08',enabled=true},{id='ego_dlc_test',version='9.00',enabled=true},{id='ws_disabled',version='1.00',enabled=false}} end
api.IsSaveListLoadingComplete=function() return true end
api.ReloadSaveList=function() end
GetSaveList=function() return occupied and {{filename='save_010'}} or {} end
IsSavingPossible=function() return true end
SaveGame=function(filename) saved=filename;pausedAtSave=paused end
QuitGame=function() quit=true end
Pause=function() paused=not paused end
AddUITriggeredEvent=function(screen,control,value) request=control end
'''+extra)
        lua.execute((UI/'seed_content.lua').read_text());lua.execute((UI/'seed_capture.lua').read_text());return lua
    def test_save_then_native_completion_then_exit(self):
        lua=self.make();lua.execute('''
frames.onUpdate();now=4;frames.onUpdate();frames.onUpdate()
events['X4LAN_Seed.State'](nil,'|1|0');assert(request=='Mark' and saved==nil)
now=6;frames.onUpdate();events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|0')
assert(saved=='save_010' and pausedAtSave and not quit)
now=10;frames.onUpdate();assert(not quit)
events.gameSaved(nil,true);assert(not quit);now=12;frames.onUpdate();assert(quit and X4LAN_SEED_CAPTURE.done)
''')
    def test_foreign_world_existing_slot_and_failed_save_do_not_exit_or_overwrite(self):
        for extra,payload in (('occupied=true',None),('', 'foreign|1|0'),('', None)):
            lua=self.make(extra);lua.execute('frames.onUpdate();now=4;frames.onUpdate();frames.onUpdate()')
            if payload:lua.globals().events['X4LAN_Seed.State'](None,payload)
            elif not extra:
                lua.execute("events['X4LAN_Seed.State'](nil,'|1|0');now=6;frames.onUpdate();events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|0');events.gameSaved(nil,false)")
            lua.execute('assert(X4LAN_SEED_CAPTURE.failed and not quit);assert(not paused)')
    def test_extra_active_content_stops_without_changing_selection(self):
        lua=self.make("X4LAN_CONFIG.seed_content='different:1'")
        lua.execute('assert(X4LAN_SEED_CAPTURE.failed and saved==nil and frames.onUpdate==nil)')

    def dock_capture(self):
        lua=self.make('X4LAN_CONFIG.coop_docks=true')
        lua.execute('''frames.onUpdate();now=4;frames.onUpdate();frames.onUpdate()
events['X4LAN_Seed.State'](nil,'|1|0');now=6;frames.onUpdate()
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|0')
assert(request=='Mark' and saved==nil and X4LAN_SEED_CAPTURE.pendingDocks)
''');return lua

    def test_optional_docks_require_matching_ack_and_new_pristine_readback(self):
        lua=self.dock_capture();lua.execute('''
now=7;frames.onUpdate();assert(saved==nil and not paused)
events['X4LAN_SeedDocks.Marked'](nil,X4LAN_CONFIG.seed_id)
assert(saved==nil);frames.onUpdate();assert(request=='Inspect')
events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|1|0')
assert(saved=='save_010' and pausedAtSave)
''')

    def test_dock_error_foreign_ack_timeout_or_changed_world_never_saves(self):
        for action in ("events['X4LAN_SeedDocks.Error'](nil,'empty registry')",
                       "events['X4LAN_SeedDocks.Marked'](nil,string.rep('b',32))",
                       'now=181;frames.onUpdate()',
                       "events['X4LAN_SeedDocks.Marked'](nil,X4LAN_CONFIG.seed_id);now=7;frames.onUpdate();events['X4LAN_Seed.State'](nil,X4LAN_CONFIG.seed_id..'|0|1')"):
            lua=self.dock_capture();lua.execute(action)
            lua.execute('assert(X4LAN_SEED_CAPTURE.failed and saved==nil and not paused and not quit)')

if __name__=='__main__':unittest.main()
