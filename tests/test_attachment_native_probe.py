"""Probe scheduling against separate pause/unpause API semantics; not native proof."""
from pathlib import Path
import unittest

from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]


class AttachmentProbeTests(unittest.TestCase):
    def make(self, execution=False):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.globals().execution_mode=execution
        for name in ('seed_docks.lua', 'attachment_controller.lua', 'attachment_execution.lua'):
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
 IsGamePaused=function() return paused end,CanDockAtDockingBay=function() return true end,
 SetObjectSectorPos=function() assert(not paused);poseWrites=poseWrites+1 end,
}
local mockffi={C=C,cdef=function() end,new=realffi.new,string=realffi.string}
local originalRequire=require
function require(name) if name=='ffi' then return mockffi end;return originalRequire(name) end
X4LAN_CONFIG={seed_id=string.rep('a',32),attachment_controller_probe=true,seed_probe='load',enabled=false,test_profile='45841',attachment_execution_probe=execution_mode}
X4LAN_SEED_DOCKS={complete=true,frame=function() end,importer={frame=function() return true end,
 query=function(_,index,state,at,callback)
  assert(index==3 and state=='STORED')
  callback({object=realffi.new('uint64_t',303),container=realffi.new('uint64_t',201),
   fingerprint='dock-3',storage=true,external=false,capacity=10});return true
 end}}
function GetCurRealTime() return now end
function ConvertStringToLuaID(value) return tonumber(value) end
function GetComponentData(_,field)
 if field=='isdocked' then return false end
 if field=='macro' then return 'ship_fighter_macro','argon' end
 assert(field=='isinternallystored');return false,false,'ship_fighter_macro','argon'
end
function RegisterEvent(name,callback) events[name]=callback end
function DebugError(text) logs[#logs+1]=text end
function AddUITriggeredEvent(_,name,value) out[#out+1]={name=name,value=value} end
function Pause() paused=true end -- Does not toggle, matching the engine API.
function Unpause() paused=false;unpauses=unpauses+1 end
''')
        lua.execute((ROOT / 'dev/attachment_controller_probe.lua').read_text())
        return lua

    def test_frozen_import_releases_pause_before_first_native_state_read(self):
        lua=self.make()
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

    def execution_ready(self):
        lua=self.make(True)
        lua.execute('''
X4LAN_SEED_DOCKS.frame(now)
events['X4LAN_AttachmentProbe.Selected'](nil,'1|2|3|4|0x65|0xc9')
assert(out[2].name=='Start')
now=.1;X4LAN_SEED_DOCKS.frame(now);assert(not paused)
now=.2;X4LAN_SEED_DOCKS.frame(now);assert(#out==2,'must wait for native Ready')
events['X4LAN_Attachment.Ready'](nil,string.rep('a',32)..'|'..string.rep('a',32))
now=.3;X4LAN_SEED_DOCKS.frame(now);assert(out[3].name=='Read' and out[3].value==0)
events['X4LAN_AttachmentProbe.Read'](nil,'0')
now=.4;X4LAN_SEED_DOCKS.frame(now)
assert(out[4].name=='Prepare' and out[4].value.operation==1)
assert(out[4].value.code=='ABC-123' and out[4].value.fromState=='FREE' and out[4].value.dock==3)
''')
        return lua

    def test_native_execution_probe_only_dispatches_inside_accepted_correlated_window(self):
        self.execution_ready().execute('''
now=.5;events['X4LAN_Attachment.Window'](nil,string.rep('a',32)..'|'..string.rep('a',32)..'|1|1')
assert(out[5].name=='Apply' and out[5].value.operation==1)
assert(logs[#logs]:find('window operation=1 result=issued',1,true))
now=.6;events['X4LAN_Attachment.Applied'](nil,string.rep('a',32)..'|'..string.rep('a',32)..'|1|applied')
assert(logs[#logs]:find('execution-ack operation=1 outcome=applied',1,true))
''')

    def test_native_execution_probe_denial_pause_and_foreign_session_never_apply(self):
        for intervention in (
            "events['X4LAN_Attachment.Window'](nil,string.rep('a',32)..'|'..string.rep('a',32)..'|1|0')",
            "paused=true;events['X4LAN_Attachment.Window'](nil,string.rep('a',32)..'|'..string.rep('a',32)..'|1|1')",
            "events['X4LAN_Attachment.Window'](nil,string.rep('b',32)..'|'..string.rep('a',32)..'|1|1')",
        ):
            with self.subTest(intervention=intervention):
                self.execution_ready().execute('now=.5;'+intervention+";for _,event in ipairs(out) do assert(event.name~='Apply') end")


if __name__ == '__main__':
    unittest.main()
