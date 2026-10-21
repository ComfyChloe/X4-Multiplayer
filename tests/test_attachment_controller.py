"""Transition ordering against injected readbacks; no X4/gameplay acceptance."""
from pathlib import Path
import unittest

from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]


class AttachmentControllerTests(unittest.TestCase):
    def make(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        for name in ('seed_docks.lua', 'attachment_controller.lua'):
            lua.execute((ROOT / 'extension/x4_lan_lab/ui' / name).read_text())
        lua.execute('''
epoch=string.rep('a',32); issued={}; target_reads=0
ship={object=101,identity='saved-ship-1',alive=true,protected=false,state='FREE',dock=0}
dock={object=201,container=300,identity='saved-dock-1',dock=1,alive=true,
      protected=false,compatible=true,available=true,cycle_free=true,external=true,storage=false}
options={epoch=epoch,ttl=10,timeout=30,
 inspect=function(id) assert(id==1);return ship end,
 target=function(id,index,state) target_reads=target_reads+1;return dock end,
 dispatch=function(command) issued[#issued+1]=command;return true end}
C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
function desire(seq,state,index,at,now)
 return C:desire(1,{epoch=epoch,sequence=seq,state=state,dock=index,
  identity='saved-ship-1',received_at=at},now or at)
end
function ready(now)
 local status,ticket=C:prepare(1,now,false);assert(status=='ready',status);return ticket
end
''')
        return lua

    def test_async_state_match_cannot_release_a_command_before_native_ack(self):
        self.make().execute('''
options.requires_ack=true;C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
desire(1,'EXTERNAL',1,0);C:execute(ready(0),0,false)
local receipt=issued[1].receipt
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:prepare(1,.1,false)=='waiting')
desire(2,'FREE',0,.2)
assert(C:prepare(1,.2,false)=='waiting' and #issued==1)
assert(C:acknowledge({},true,.2)=='stale')
assert(C:acknowledge(receipt,true,.3)=='accepted')
assert(C:acknowledge(receipt,false,.3)=='stale')
assert(C:describe(ready(.3)).action=='UNDOCK' and #issued==1)
''')

    def test_native_ack_alone_does_not_replace_actual_readback(self):
        self.make().execute('''
options.requires_ack=true;C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
desire(1,'EXTERNAL',1,0);C:execute(ready(0),0,false)
assert(C:acknowledge(issued[1].receipt,true,.1)=='accepted')
assert(C:prepare(1,.1,false)=='waiting')
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:prepare(1,.2,false)=='settled')
assert(not C:allow_pose(1,.2,false))
''')

    def test_confirmed_no_mutation_rejection_can_retry_but_late_ack_cannot_release_retry(self):
        self.make().execute('''
options.requires_ack=true;C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
desire(1,'EXTERNAL',1,0);C:execute(ready(0),0,false)
local first=issued[1].receipt
assert(C:acknowledge(first,false,.1)=='rejected')
C:execute(ready(.2),.2,false)
assert(#issued==2 and issued[2].receipt~=first)
assert(C:acknowledge(first,true,.3)=='stale')
assert(C:prepare(1,.3,false)=='waiting')
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:prepare(1,.4,false)=='waiting')
assert(C:acknowledge(issued[2].receipt,true,.5)=='accepted')
assert(C:prepare(1,.5,false)=='settled')
''')

    def test_missing_or_late_native_ack_remains_fenced_and_times_out(self):
        self.make().execute('''
options.requires_ack=true;C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
desire(1,'EXTERNAL',1,0);C:execute(ready(0),0,false)
ship.state='EXTERNAL';ship.dock=1;ship.container=300
C:cancel(1)
assert(C:prepare(1,29,false)=='waiting')
assert(C:acknowledge(issued[1].receipt,false,30)=='failed')
assert(desire(2,'FREE',0,31)=='failed' and #issued==1)
''')

    def test_async_mode_cannot_be_disabled_by_changing_the_callers_options(self):
        self.make().execute('''
options.requires_ack=true;C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
options.requires_ack=false
desire(1,'EXTERNAL',1,0);C:execute(ready(0),0,false)
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:prepare(1,.1,false)=='waiting')
C:stop();assert(C:acknowledge(issued[1].receipt,true,.2)=='stopped')
''')

    def test_expired_idle_views_release_capacity_without_resetting_replay_marks(self):
        self.make().execute('''
for id=1,4096 do
 assert(C:desire(id,{epoch=epoch,sequence=1,state='FREE',dock=0,
   identity='saved-ship-'..id,received_at=0},0)=='accepted')
end
-- No adapter prepare/cancel was made: capacity admission must reap idle expiry.
assert(C:desire(4097,{epoch=epoch,sequence=1,state='FREE',dock=0,
 identity='saved-ship-4097',received_at=11},11)=='accepted')
assert(C:prepare(1,11,false)=='unknown')
assert(desire(1,'FREE',0,11)=='duplicate')
assert(desire(2,'FREE',0,11)=='accepted')
assert(C:prepare(1,22,false)=='expired')
assert(C:prepare(1,22,false)=='unknown')
assert(desire(2,'FREE',0,22)=='duplicate')
''')

    def test_capacity_reaping_preserves_an_issued_expired_transition(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);local ticket=ready(0)
assert(C:execute(ticket,0,false)=='issued')
for id=2,4096 do
 assert(C:desire(id,{epoch=epoch,sequence=1,state='FREE',dock=0,
  identity='saved-ship-'..id,received_at=0},0)=='accepted')
end
assert(C:desire(4097,{epoch=epoch,sequence=1,state='FREE',dock=0,
 identity='saved-ship-4097',received_at=11},11)=='accepted')
assert(C:prepare(1,11,false)=='waiting' and #issued==1)
assert(C:prepare(1,30,false)=='failed' and #issued==1)
assert(desire(2,'FREE',0,30)=='failed')
''')

    def test_free_to_external_requires_native_readback_before_settling(self):
        self.make().execute('''
assert(desire(1,'EXTERNAL',1,0)=='accepted')
ticket=ready(0);local info=C:describe(ticket)
assert(info.action=='ATTACH' and info.target==201 and info.object==101)
assert(not C:allow_pose(1,0,false))
assert(C:execute(ticket,.1,false)=='issued' and #issued==1)
assert(C:execute(ticket,.1,false)=='stale' and #issued==1)
assert(C:prepare(1,1,false)=='waiting')
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:prepare(1,2,false)=='settled' and not C:allow_pose(1,2,false))
assert(#issued==1)
''')

    def test_changing_carriers_waits_for_undock_then_rechecks_destination(self):
        self.make().execute('''
ship.state='STORED';ship.dock=2;ship.container=400
assert(desire(1,'EXTERNAL',1,0)=='accepted')
first=ready(0);assert(C:describe(first).action=='UNDOCK' and target_reads==0)
assert(C:execute(first,.1,false)=='issued')
assert(C:prepare(1,1,false)=='waiting' and #issued==1)
ship.state='FREE';ship.dock=0;ship.container=nil
second=ready(2);assert(C:describe(second).action=='ATTACH')
dock.available=false
assert(C:execute(second,2,false)=='blocked' and #issued==1)
dock.available=true;third=ready(3);assert(C:execute(third,3,false)=='issued')
assert(#issued==2 and issued[1].action=='UNDOCK' and issued[2].action=='ATTACH')
''')

    def test_queue_then_dock_or_player_takeover_never_dispatches_old_action(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);ticket=ready(0)
ship.state='STORED';ship.dock=2;ship.container=400
assert(C:execute(ticket,.1,false)=='changed' and #issued==0)
ticket=ready(1);ship.protected=true
assert(C:execute(ticket,1,false)=='failed' and #issued==0)
assert(C:prepare(1,2,false)=='failed')
''')

    def test_native_identity_replacement_and_target_replacement_are_rejected(self):
        for mutation in ("ship.object=102", "ship.identity='another-ship'",
                         "dock.object=202", "dock.container=401", "dock.identity='other-bay'"):
            with self.subTest(mutation=mutation):
                self.make().execute('''
desire(1,'EXTERNAL',1,0);ticket=ready(0)
''' + mutation + '''
assert(C:execute(ticket,.1,false)=='failed' and #issued==0)
''')

    def test_occupancy_reservation_cycle_and_type_are_rechecked_at_consumption(self):
        for mutation in ("dock.available=false", "dock.compatible=false", "dock.protected=true",
                         "dock.cycle_free=false", "dock.external=false", "dock.container=101",
                         "dock.alive=false", "dock.dock=2"):
            with self.subTest(mutation=mutation):
                self.make().execute('''
desire(1,'EXTERNAL',1,0);ticket=ready(0)
''' + mutation + '''
assert(C:execute(ticket,.1,false)=='blocked' and #issued==0)
''')

    def test_reordered_intentions_and_pause_revoke_queued_capabilities(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);old=ready(0)
assert(desire(2,'FREE',0,1)=='accepted')
assert(C:execute(old,1,false)=='stale' and #issued==0)
assert(desire(1,'EXTERNAL',1,1)=='stale')
assert(C:allow_pose(1,1,false))
desire(3,'EXTERNAL',1,2);old=ready(2)
assert(C:execute(old,2,true)=='paused' and #issued==0)
assert(C:execute(old,3,false)=='stale')
new=ready(3);assert(C:execute(new,3,false)=='issued')
''')

    def test_duplicate_never_refreshes_ttl_and_expired_state_cannot_move_or_release_pose(self):
        self.make().execute('''
assert(desire(1,'FREE',0,0)=='accepted' and C:allow_pose(1,0,false))
assert(desire(1,'FREE',0,9)=='duplicate')
assert(not C:allow_pose(1,10.01,false))
assert(C:prepare(1,10.01,false)=='expired')
assert(desire(2,'EXTERNAL',1,0,11)=='expired')
assert(#issued==0)
''')

    def test_superseded_inflight_action_keeps_its_readback_fence(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);first=ready(0);C:execute(first,0,false)
desire(2,'FREE',0,1)
assert(C:prepare(1,1,false)=='waiting' and not C:allow_pose(1,1,false))
ship.state='EXTERNAL';ship.dock=1;ship.container=300
second=ready(2);assert(C:describe(second).action=='UNDOCK')
C:execute(second,2,false)
ship.state='FREE';ship.dock=0;ship.container=nil
assert(C:prepare(1,3,false)=='settled' and C:allow_pose(1,3,false))
assert(#issued==2)
''')

    def test_cancelled_inflight_work_cannot_be_assumed_finished(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);first=ready(0);C:execute(first,0,false)
C:cancel(1);assert(C:prepare(1,1,false)=='waiting')
assert(desire(1,'EXTERNAL',1,1)=='duplicate')
desire(2,'FREE',0,2);assert(C:prepare(1,2,false)=='waiting')
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:describe(ready(3)).action=='UNDOCK')
''')

    def test_timeout_is_not_extended_by_fresh_observations_or_retries(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);first=ready(0);C:execute(first,0,false)
for i=1,3 do desire(i+1,'EXTERNAL',1,i*9);assert(C:prepare(1,i*9,false)=='waiting') end
assert(C:prepare(1,30,false)=='failed' and #issued==1)
assert(C:execute(first,30,false)=='stale')
assert(desire(5,'EXTERNAL',1,31)=='failed')
''')

    def test_dispatch_exception_is_uncertain_and_never_retried_automatically(self):
        self.make().execute('''
options.dispatch=function(command) issued[#issued+1]=command;error('unknown native outcome') end
C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
desire(1,'EXTERNAL',1,0);ticket=ready(0)
assert(C:execute(ticket,0,false)=='failed' and #issued==1)
assert(C:execute(ticket,1,false)=='stale' and C:prepare(1,1,false)=='failed' and #issued==1)
''')

    def test_config_mutation_foreign_ticket_and_clock_rollback_cannot_widen_gate(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);ticket=ready(0)
options.ttl=100000;options.epoch='other';options.dispatch=function() error('changed') end
assert(C:execute({},1,false)=='stale')
assert(C:execute(ticket,11,false)=='stale' and #issued==0)
assert(C:prepare(1,10,false)=='stopped')
assert(desire(2,'FREE',0,12)=='stopped' and not C:allow_pose(1,12,false))
''')

    def test_pose_read_does_not_create_or_revoke_transition_ticket(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);ticket=ready(0)
assert(not C:allow_pose(1,0,false) and C:describe(ticket).action=='ATTACH')
assert(C:execute(ticket,0,false)=='issued' and #issued==1)
''')

    def test_repeated_frames_preserve_ticket_without_rebinding_changed_destination(self):
        self.make().execute('''
desire(1,'EXTERNAL',1,0);ticket=ready(0)
for i=1,10 do assert(ready(i/100)==ticket) end
assert(C:execute(ticket,.2,false)=='issued' and #issued==1)
''')
        self.make().execute('''
desire(1,'EXTERNAL',1,0);ticket=ready(0);dock.object=999
assert(C:prepare(1,.1,false)=='failed')
assert(C:execute(ticket,.1,false)=='stale' and #issued==0)
''')

    def test_active_view_is_bounded_and_cancel_does_not_reset_replay_watermarks(self):
        self.make().execute('''
local row={epoch=epoch,sequence=1,state='FREE',dock=0,identity='saved',received_at=0}
for id=1,4096 do assert(C:desire(id,row,0)=='accepted') end
assert(C:desire(4097,row,0)=='full')
C:cancel(1);assert(C:desire(1,row,0)=='duplicate')
assert(C:desire(4097,row,0)=='accepted')
row.sequence=2;assert(C:desire(1,row,0)=='full')
''')


if __name__ == '__main__':
    unittest.main()
