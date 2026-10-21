"""Actual universe adapter plus native API doubles; no native-session acceptance."""
from pathlib import Path
import unittest
import test_universe_adapter as universe_fixture
UI=universe_fixture.UI


class AttachmentWorldTests(unittest.TestCase):
    def make(self,slot='p2'):
        lua=universe_fixture.UniverseAdapterTests().make(slot,seed=True)
        for name in ('attachment_protocol.lua','attachment_world.lua'):
            lua.execute((UI/name).read_text())
        lua.execute('''
X4LAN_CONFIG.attachment_sync=true
client.lease=string.rep('d',32)
attachmentStates={};docks={};parents={}
api.GetParentComponent=function(object) return parents[object] or 0 end
api.IsShipAtExternalDock=function(object) return attachmentStates[object]=='EXTERNAL' end
local context=api.GetContextByClass
api.GetContextByClass=function(object,class,...)
 if class=='dockingbay' then return docks[object] or 0 end
 return context(object,class,...)
end
local data=GetComponentData
GetComponentData=function(object,field,...)
 object=tonumber(object)
 if field=='isinternallystored' then
  local mode=attachmentStates[object] or 'FREE';return mode=='STORED',mode~='FREE'
 end
 if field=='isdocked' then return (attachmentStates[object] or 'FREE')~='FREE' end
 return data(object,field,...)
end
X4LAN_SEED_DOCK_GATE={ready=true,importer={ready=function() return true end,
 fingerprints=function() return {[1]='dock-one',[2]='dock-two'} end,
 index=function(_,dock) return ({[201]=1,[202]=2})[dock] end}}
function receiveAttachment(sequence,state,dock,revision,lease,epoch)
 local packet=assert(X4LAN_ATTACHMENT_PROTOCOL.encode({recipient=lease or client.lease,
  epoch=epoch or string.rep('a',32),revision=revision or 1,sequence=sequence,
  ship=1,dock=dock or 0,state=state or 'FREE'}))
 U.receive(P.split(packet,'|'),now)
end
function freshIdentity()
 X4LAN_BUBBLE_API.identity.rows.N1={code='NAT-101',epoch=string.rep('a',32),at=now}
end
function admitted()
 tick(1);baseline(1,{testRow},true);tick(25);freshIdentity();tick(1)
 assert(U.attachments and not U.failed)
end
''')
        return lua

    def test_missing_attached_expired_and_replayed_observations_fence_pose(self):
        self.make().execute('''
admitted();assert(positions[101].x==101)
receiveAttachment(1);tick(1);assert(positions[101].x==1500)
positions[101].x=999;receiveAttachment(2,'STORED',1);tick(1);assert(positions[101].x==999)
receiveAttachment(3);tick(1);assert(positions[101].x==1500)
tick(41);positions[101].x=999;freshIdentity();receiveAttachment(3);tick(1)
assert(positions[101].x==999 and not U.failed)
receiveAttachment(4);tick(1);assert(positions[101].x==1500)
''')

    def test_wrong_recipient_epoch_revision_and_unknown_dock_do_not_admit_pose(self):
        for args in ("1,'FREE',0,1,string.rep('e',32)","1,'FREE',0,1,nil,string.rep('b',32)",
                     "1,'FREE',0,2", "1,'STORED',3"):
            with self.subTest(args=args):
                self.make().execute("admitted();receiveAttachment("+args+");tick(1);assert(positions[101].x==101 and not U.failed)")

    def test_native_stored_external_transit_unknown_and_player_carrier_never_allow_pose(self):
        for setup in ("attachmentStates[101]='STORED';docks[101]=201",
                      "attachmentStates[101]='EXTERNAL';docks[101]=201",
                      "attachmentStates[101]='TRANSIT';docks[101]=201",
                      "attachmentStates[101]='EXTERNAL';docks[101]=999",
                      "parents[11]=101"):
            with self.subTest(setup=setup):
                self.make().execute("admitted();"+setup+";receiveAttachment(1);tick(1);assert(positions[101].x==101)")

    def test_attachment_mode_never_uses_legacy_queued_sector_warp(self):
        self.make().execute('''
admitted();moves={};AddUITriggeredEvent=function(_,event,value) if event=='SeedMove' then moves[#moves+1]=value end end
sectors[101]=23;receiveAttachment(1);tick(2)
assert(#moves==0 and positions[101].x==101 and not U.failed)
''')

    def test_view_departure_cannot_retain_or_repopulate_old_authority(self):
        self.make().execute('''
admitted();receiveAttachment(1);tick(1);assert(positions[101].x==1500)
local W=U.attachments;W:clear_view();receiveAttachment(2)
W:commit(U.latest);assert(not W:allow_pose(1,101,now))
receiveAttachment(3);assert(W:allow_pose(1,101,now))
client.lease=string.rep('e',32);tick(1);assert(U.failed)
''')

    def test_native_descriptor_mutation_stops_retained_pose(self):
        self.make().execute('''
admitted();receiveAttachment(1);tick(1);positions[101].x=999
X4LAN_BUBBLE_API.identity.nativeCode=function() return 'CHANGED' end
tick(1);assert(U.failed and positions[101].x==999)
''')

    def test_player_ancestor_is_protected_during_initial_seed_import(self):
        self.make().execute('''
parents[11]=101;tick(1)
for _,entry in ipairs(activated) do assert(entry.id~=101) end
assert(not U.failed)
''')

    def test_host_publication_budget_remains_fair_across_repeated_baseline_refresh(self):
        self.make('p1').execute('''
tick(2);local W=assert(U.attachments);local rows={};U.publish={}
for i=1,4096 do
 local object=10000+i;positions[object]={x=i,y=0,z=0,yaw=0,pitch=0,roll=0};sectors[object]=22
 U.seedObjects.entries[i]={object=object,macro='ship_arg_s_scout_01_a_macro',owner='argon',code='NAT-'..object}
 U.publish[i]={id=i,object=object}
 rows[i]=i..',ship,ship_arg_s_scout_01_a_macro,argon,0,0,0,0,0,0,none,0'
end
local seen={};local sent=0;local tickCount=0
X4LAN_BUBBLE_API.state.transport.send=function(_,wire)
 local row=assert(X4LAN_ATTACHMENT_PROTOCOL.decode(wire));sent=sent+1
 if seen[row.ship] then assert(tickCount-seen[row.ship]<=32) end
 seen[row.ship]=tickCount
end
for step=1,96 do
 tickCount=step;U.sender=P.sender(P.baseline(client.lease,step,'cluster_14_sector001_macro',rows))
 local before=sent;W:frame(now+step*.051);assert(sent-before==128)
end
local total=0;for _ in pairs(seen) do total=total+1 end
assert(total==4096 and sent==96*128)
''')

    def test_host_publishes_actual_saved_state_and_skips_unresolved_transit(self):
        self.make('p1').execute('''
tick(1);U.wanted='cluster_14_sector001_macro';tick(20)
assert(U.attachments and U.sender and not U.failed)
local sent={};X4LAN_BUBBLE_API.state.transport.send=function(_,wire) if wire:sub(1,8)=='UATTACH|' then sent[#sent+1]=wire end end
attachmentStates[101]='STORED';docks[101]=201
attachmentStates[102]='TRANSIT';docks[102]=202
tick(2);local found=false
for _,wire in ipairs(sent) do
 local row=assert(X4LAN_ATTACHMENT_PROTOCOL.decode(wire))
 assert(row.recipient==client.lease and row.epoch==client.lease and row.revision==U.sender.baseline.revision)
 if row.ship==1 then assert(row.state=='STORED' and row.dock==1);found=true end
 assert(row.ship~=2)
end
assert(found and not U.failed)
''')


if __name__=='__main__':unittest.main()
