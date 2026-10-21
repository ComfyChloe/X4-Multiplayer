"""World adapter native-event orchestration; engine API/readback doubles only."""
import unittest
import test_attachment_world as fixture
UI=fixture.UI


class AttachmentWorldExecutionTests(unittest.TestCase):
    def make(self):
        lua=fixture.AttachmentWorldTests().make()
        for name in ('seed_docks.lua','attachment_controller.lua','attachment_execution.lua','attachment_driver.lua'):
            lua.execute((UI/name).read_text())
        lua.execute('''
X4LAN_CONFIG.attachment_execute=true
nativeCommands={};nativeQueries={}
local trigger=AddUITriggeredEvent
AddUITriggeredEvent=function(screen,control,value)
 if screen=='X4LAN_Attachment' then nativeCommands[#nativeCommands+1]={control=control,value=value}
 else trigger(screen,control,value) end
end
positions[300]={x=100,y=0,z=0,yaw=0,pitch=0,roll=0}
api.CanDockAtDockingBay=function() return true end
api.GetNumDockedShips=function() return 0 end
X4LAN_SEED_DOCK_GATE.importer.query=function(_,index,state,at,callback)
 nativeQueries[#nativeQueries+1]={index=index,state=state,callback=callback};return true
end
function readyNative()
 events['X4LAN_Attachment.Ready'](nil,X4LAN_CONFIG.seed_id..'|'..U.latest.epoch)
end
function nativeEvent(kind,operation,outcome)
 events['X4LAN_Attachment.'..kind](nil,X4LAN_CONFIG.seed_id..'|'..U.latest.epoch..'|'..operation..'|'..outcome)
end
function queryResult(index)
 nativeQueries[index].callback({object=201,parent=300,container=300,fingerprint='dock-one',
  external=true,storage=false,capacity=1},now)
end
''')
        return lua

    def test_startup_then_attach_and_undock_require_target_window_ack_and_actual_state(self):
        self.make().execute('''
admitted();assert(#nativeCommands==1 and nativeCommands[1].control=='Start')
receiveAttachment(1);tick(1);assert(positions[101].x==101)
readyNative();tick(1);assert(positions[101].x==1500)
positions[101].x=999;receiveAttachment(2,'EXTERNAL',1);tick(1)
assert(#nativeQueries==1 and #nativeCommands==1 and positions[101].x==999)
queryResult(1);assert(#nativeCommands==2 and nativeCommands[2].control=='Prepare')
local command=nativeCommands[2].value
assert(command.ship==1 and command.fromState=='FREE' and command.fromDock==0 and command.code=='NAT-101')
nativeEvent('Window',1,'1');assert(nativeCommands[3].control=='Apply')
attachmentStates[101]='EXTERNAL';docks[101]=201;tick(1)
assert(positions[101].x==999)
nativeEvent('Applied',1,'applied');tick(1)
receiveAttachment(3);tick(1)
assert(#nativeQueries==1 and nativeCommands[4].control=='Prepare' and nativeCommands[4].value.action=='UNDOCK')
nativeEvent('Window',2,'1');nativeEvent('Applied',2,'applied');tick(1)
assert(positions[101].x==999)
attachmentStates[101]='FREE';docks[101]=nil;tick(1)
assert(positions[101].x==1500 and not U.failed)
''')

    def test_missing_startup_and_foreign_native_events_stop_without_apply(self):
        for intervention in ("tick(201)",
                             "events['X4LAN_Attachment.Ready'](nil,string.rep('f',32)..'|'..U.latest.epoch)"):
            with self.subTest(intervention=intervention):
                self.make().execute('admitted();'+intervention+'''
assert(U.failed and nativeCommands[#nativeCommands].control=='Stop')
for _,command in ipairs(nativeCommands) do assert(command.control~='Apply') end
''')

    def test_admission_loss_before_native_window_revokes_execution(self):
        self.make().execute('''
admitted();readyNative();receiveAttachment(1,'EXTERNAL',1);tick(1);queryResult(1)
X4LAN_SEED_DOCK_GATE.ready=false
nativeEvent('Window',1,'1');assert(U.failed and nativeCommands[#nativeCommands].control=='Stop')
for _,command in ipairs(nativeCommands) do assert(command.control~='Apply') end
''')

    def test_pause_after_prepare_does_not_dispatch_until_a_fresh_query(self):
        self.make().execute('''
admitted();readyNative();receiveAttachment(1,'EXTERNAL',1);tick(1);queryResult(1)
paused=true;nativeEvent('Window',1,'1')
assert(#nativeCommands==2 and not U.failed)
paused=false;tick(1);assert(#nativeQueries==2);queryResult(2)
nativeEvent('Window',2,'1');assert(nativeCommands[#nativeCommands].control=='Apply' and not U.failed)
''')


if __name__=='__main__':unittest.main()
