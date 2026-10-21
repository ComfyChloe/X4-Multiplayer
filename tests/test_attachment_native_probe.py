"""Probe scheduling against separate pause/unpause API semantics; not native proof."""
from pathlib import Path
import unittest

from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]


class AttachmentProbeTests(unittest.TestCase):
    def test_frozen_import_releases_pause_before_first_native_state_read(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        for name in ('seed_docks.lua', 'attachment_controller.lua'):
            lua.execute((ROOT / 'extension/x4_lan_lab/ui' / name).read_text())
        lua.execute('''
local realffi=require('ffi')
realffi.cdef('typedef uint64_t UniverseID; typedef struct {float x,y,z,yaw,pitch,roll;} UIPosRot;')
now=0;paused=true;unpauses=0;poseWrites=0;events={};out={};logs={}
local C={
 GetPlayerControlledShipID=function() return 0 end,GetPlayerOccupiedShipID=function() return 0 end,
 GetPlayerContainerID=function() return 0 end,GetParentComponent=function() return 0 end,
 IsComponentOperational=function() return true end,GetNumDockedShips=function() return 0 end,
 GetDockedShips=function() return 0 end,ActivateObject=function() end,
 GetContextByClass=function(_,class) return class=='sector' and 400 or 0 end,
 GetObjectPositionInSector=function() return {x=0,y=0,z=0,yaw=0,pitch=0,roll=0} end,
 GetObjectIDCode=function() return 'ABC-123' end,IsShipAtExternalDock=function() return false end,
 IsGamePaused=function() return paused end,
 SetObjectSectorPos=function() assert(not paused);poseWrites=poseWrites+1 end,
}
local mockffi={C=C,cdef=function() end,new=realffi.new,string=realffi.string}
local originalRequire=require
function require(name) if name=='ffi' then return mockffi end;return originalRequire(name) end
X4LAN_CONFIG={seed_id=string.rep('a',32),attachment_controller_probe=true,seed_probe='load',enabled=false,test_profile='45841'}
X4LAN_SEED_DOCKS={complete=true,frame=function() end,importer={frame=function() return true end}}
function GetCurRealTime() return now end
function ConvertStringToLuaID(value) return tonumber(value) end
function GetComponentData(_,field)
 if field=='isdocked' then return false end
 assert(field=='isinternallystored');return false,false,'ship_fighter_macro','argon'
end
function RegisterEvent(name,callback) events[name]=callback end
function DebugError(text) logs[#logs+1]=text end
function AddUITriggeredEvent(_,name,value) out[#out+1]={name=name,value=value} end
function Pause() paused=true end -- Does not toggle, matching the engine API.
function Unpause() paused=false;unpauses=unpauses+1 end
''')
        lua.execute((ROOT / 'dev/attachment_controller_probe.lua').read_text())
        lua.execute('''
X4LAN_SEED_DOCKS.frame(now)
assert(out[1].name=='Select')
events['X4LAN_AttachmentProbe.Selected'](nil,'1|2|3|4|0x65|0xc9')
now=.1;X4LAN_SEED_DOCKS.frame(now)
assert(not paused and unpauses==1 and poseWrites==0 and #out==1)
now=.2;X4LAN_SEED_DOCKS.frame(now)
assert(unpauses==1 and poseWrites==1 and out[2].name=='Read' and out[2].value==0)
assert(logs[#logs]:find('read phase=0 state=FREE dock=0',1,true))
''')


if __name__ == '__main__':
    unittest.main()
