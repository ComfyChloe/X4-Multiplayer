"""Dev probe completion ordering with injected native callbacks, not engine proof."""
from pathlib import Path
import unittest
from lupa.luajit21 import LuaRuntime
from dev.native_attachment_world_test import recipient_complete

ROOT=Path(__file__).resolve().parents[1]


class NativeHostProbeCompletionTests(unittest.TestCase):
    def test_last_baseline_ack_waits_for_every_saved_ships_attachment_delivery(self):
        required={1:{10,20},2:{10,20},3:{10,20}}
        delivered={1:{10,20},2:{10,20}}
        self.assertFalse(recipient_complete(required,delivered))
        delivered[3]={10}
        self.assertFalse(recipient_complete(required,delivered))
        delivered[3].add(20)
        self.assertTrue(recipient_complete(required,delivered))
        self.assertFalse(recipient_complete({1:{10,20},2:{10,20}},delivered))
        required[4]=set()
        self.assertFalse(recipient_complete(required,delivered))

    def test_native_readbacks_do_not_finish_before_the_last_baseline_is_sent(self):
        lua=LuaRuntime();lua.execute((ROOT/'extension/x4_lan_lab/ui/attachment_protocol.lua').read_text())
        lua.execute('''
now=0;paused=true;pauseCalls=0;events={};logs={};requests={};wire={}
package.loaded.ffi={C={IsGamePaused=function() return paused end}}
function Pause() paused=true;pauseCalls=pauseCalls+1 end
function Unpause() paused=false end
function GetCurRealTime() return now end
function DebugError(v) logs[#logs+1]=v end
function RegisterEvent(name,fn) events[name]=fn end
function AddUITriggeredEvent(screen,control,args) requests[#requests+1]=args end
function GetComponentData(object) return 'ship_arg_s_scout_01_a_macro','argon' end
X4LAN_CONFIG={slot='p1',test_profile='45841',attachment_world_probe=true,attachment_sync=true,
 seed_binding=true,seed_id=string.rep('a',32)}
X4LAN_SEED_GATE={frame=function() return true end}
X4LAN_SEED_DOCK_GATE={ready=true,importer={ready=function() return true end,
 fingerprints=function() return {[1]='dock'} end}}
U={frame=function() end,seedObjects={index=function() return 1 end},sender={done=false}}
X4LAN_BUBBLE_API={universe=U,state={},id=function(o) return o end,
 identity={nativeCode=function() return 'ABC-123' end},coop={ready=true},cargo={ready=true},
 trade={state={ready=true}},quest={ready=true}}
X4LAN_ATTACHMENT_WORLD={new=function() return {inspect=function() return 'FREE',0 end} end}
X4LAN_TRANSPORT={open=function() return {send=function(_,p) wire[#wire+1]=p end,
 receive=function() end,close=function() end} end}
''')
        lua.execute((ROOT/'dev/attachment_world_probe.lua').read_text())
        lua.execute('''
X4LAN_SEED_GATE.frame(now);assert(not paused)
local world=X4LAN_ATTACHMENT_WORLD.new();local net=X4LAN_TRANSPORT.open()
for revision=1,3 do
 now=revision/10;world:inspect(101)
 net:send('UATTACH|1|'..string.rep('b',32)..'|'..string.rep('b',32)..'|'..revision..'|'..revision..'|1|0|FREE')
 events['X4LAN_AttachmentWorldProbe.Read'](nil,revision)
end
now=13;U.frame(now)
assert(not paused and pauseCalls==0,'incomplete sender must keep the native host running')
U.sender.done=true;now=14;U.frame(now)
assert(paused and pauseCalls==1 and table.concat(logs,' '):find('complete slot=p1'))
''')


if __name__=='__main__':unittest.main()
