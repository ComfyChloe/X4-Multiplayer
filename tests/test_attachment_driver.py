"""Real protocol/controller/bridge orchestration with correlated native doubles."""
from pathlib import Path
import unittest
import test_attachment_controller as fixture
UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'


class AttachmentDriverTests(unittest.TestCase):
    def make(self):
        lua=fixture.AttachmentControllerTests().make()
        for name in ('attachment_protocol.lua','attachment_execution.lua','attachment_driver.lua'):
            lua.execute((UI/name).read_text())
        lua.execute('''
R=X4LAN_ATTACHMENT_PROTOCOL.receiver(string.rep('b',32),2)
R:admit(epoch,1,{[1]='saved-ship-1'},{[1]='saved-dock-1'})
queries={};prepares={};applies={};paused=false
D=X4LAN_ATTACHMENT_DRIVER.new({receiver=R,epoch=epoch,inspect=options.inspect,
 paused=function() return paused end,
 query=function(index,state,now,callback) queries[#queries+1]={index=index,state=state,callback=callback};return true end,
 target=function(id,row,state) assert(row==dock);return row end,
 prepare=function(operation,info) prepares[#prepares+1]={operation=operation,info=info} end,
 apply=function(operation) applies[#applies+1]=operation end})
function observe(sequence,state,index,now)
 local wire=assert(X4LAN_ATTACHMENT_PROTOCOL.encode({recipient=R.lease,epoch=epoch,revision=R.revision,
  sequence=sequence,ship=1,state=state,dock=index}))
 assert(R:receive(wire,now)=='applied');assert(D:observe(1,now)=='accepted')
end
function attach()
 observe(1,'EXTERNAL',1,0);assert(D:frame(0,false)=='ready' and #queries==1 and #prepares==0)
 queries[1].callback(dock,.05);assert(#prepares==1)
 assert(D:window(1,true,.1,false)=='issued' and #applies==1)
end
''')
        return lua

    def test_actual_state_and_correlated_ack_both_precede_release_then_undock(self):
        self.make().execute('''
attach();ship.state='EXTERNAL';ship.dock=1;ship.container=300
D:frame(.2,false);assert(not D:allow_pose(1,.2,false))
assert(D:acknowledge(1,'applied',.3)=='accepted');D:frame(.3,false)
observe(2,'FREE',0,.4);D:frame(.4,false);assert(#prepares==2 and #queries==1)
D:window(2,true,.5,false);D:acknowledge(2,'applied',.6)
assert(not D:allow_pose(1,.6,false));ship.state='FREE';ship.dock=0;ship.container=nil
D:frame(.7,false);assert(D:allow_pose(1,.7,false) and not D:failed())
''')

    def test_superseded_query_never_queues_and_pose_waits_until_its_callback(self):
        self.make().execute('''
observe(1,'EXTERNAL',1,0);D:frame(0,false)
observe(2,'FREE',0,.05);D:frame(.05,false)
assert(not D:allow_pose(1,.05,false))
queries[1].callback(dock,.1);assert(#prepares==0 and D:allow_pose(1,.1,false))
''')

    def test_revision_change_during_query_requires_fresh_target_query(self):
        self.make().execute('''
observe(1,'EXTERNAL',1,0);D:frame(0,false)
R:admit(epoch,2,{[1]='saved-ship-1'},{[1]='saved-dock-1'})
queries[1].callback(dock,.1);assert(#prepares==0)
D:frame(.2,false);assert(#queries==2)
queries[2].callback(dock,.25);assert(#prepares==1)
''')

    def test_departure_keeps_issued_fence_until_actual_result_then_allows_new_work(self):
        self.make().execute('''
attach();R.records={};D:cancel(1);D:acknowledge(1,'applied',.2)
D:frame(.3,false);assert(not D:allow_pose(1,.3,false))
ship.state='EXTERNAL';ship.dock=1;ship.container=300;D:frame(.4,false)
observe(2,'FREE',0,.5);D:frame(.5,false);assert(#prepares==2)
''')

    def test_fresh_equal_observations_do_not_starve_native_query_and_window(self):
        self.make().execute('''
observe(1,'EXTERNAL',1,0);D:frame(0,false)
local wire=assert(X4LAN_ATTACHMENT_PROTOCOL.encode({recipient=R.lease,epoch=epoch,revision=1,
 sequence=2,ship=1,state='EXTERNAL',dock=1}))
assert(R:receive(wire,.05)=='applied');D:observe(1,.05)
queries[1].callback(dock,.1);assert(#prepares==1)
wire=wire:gsub('|2|1|1|EXTERNAL$','|3|1|1|EXTERNAL')
assert(R:receive(wire,.15)=='applied');D:observe(1,.15)
assert(D:window(1,true,.2,false)=='issued' and #applies==1)
''')

    def test_equal_observation_cannot_extend_the_frozen_native_authorization(self):
        self.make().execute('''
observe(1,'EXTERNAL',1,0);D:frame(0,false);queries[1].callback(dock,.4)
local wire=assert(X4LAN_ATTACHMENT_PROTOCOL.encode({recipient=R.lease,epoch=epoch,revision=1,
 sequence=2,ship=1,state='EXTERNAL',dock=1}))
assert(R:receive(wire,.7)=='applied');D:observe(1,.7)
assert(D:window(1,true,.76,false)=='expired' and #applies==0)
D:frame(.8,false);assert(#queries==2)
queries[2].callback(dock,.85);assert(#prepares==2)
assert(D:window(2,true,.9,false)=='issued' and #applies==1)
''')

    def test_render_frames_between_prepare_and_window_preserve_the_queued_ticket(self):
        self.make().execute('''
observe(1,'EXTERNAL',1,0);D:frame(0,false);queries[1].callback(dock,.05)
D:frame(.06,false);D:frame(.07,false)
assert(#prepares==1 and #queries==1 and not D:allow_pose(1,.08,false))
assert(D:window(1,true,.1,false)=='issued' and #applies==1)
''')

    def test_missing_query_response_stops_instead_of_reusing_a_cached_target(self):
        self.make().execute('''
observe(1,'EXTERNAL',1,0);D:frame(0,false);D:frame(5.01,false)
assert(D:failed() and #prepares==0 and #applies==0)
queries[1].callback(dock,5.1);assert(#prepares==0 and not D:allow_pose(1,5.1,false))
''')

    def test_unknown_outcome_or_unresolved_native_state_remains_fenced(self):
        for condition in ("D:frame(5.2,false)", "D:acknowledge(1,'unknown',.2)",
                          "D:acknowledge(1,'applied',.2);ship.state='TRANSIT';D:frame(.3,false);D:frame(30.31,false)"):
            with self.subTest(condition=condition):
                self.make().execute('attach();'+condition+";assert(D:failed() and not D:allow_pose(1,31,false))")


if __name__=='__main__':unittest.main()
