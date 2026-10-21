"""Local MD handoff ordering; injected native callbacks, no engine acceptance."""
from pathlib import Path
import unittest
import test_attachment_controller as controller_fixture

ROOT=Path(__file__).resolve().parents[1]


class NativeExecutionTests(unittest.TestCase):
    def make(self):
        lua=controller_fixture.AttachmentControllerTests().make()
        lua.execute((ROOT/'extension/x4_lan_lab/ui/attachment_execution.lua').read_text())
        lua.execute('''
prepares={};applies={};latest=nil
options.requires_ack=true
options.dispatch=function(command) issued[#issued+1]=command;return E:dispatch(command) end
C=X4LAN_ATTACHMENT_CONTROLLER.new(options)
execOptions={controller=C,epoch=epoch,ttl=2,
 observation=function(ship,now) assert(ship==1);return latest end,
 prepare=function(operation,info) prepares[#prepares+1]={operation=operation,info=info} end,
 apply=function(operation) applies[#applies+1]=operation end}
E=X4LAN_ATTACHMENT_EXECUTION.new(execOptions)
function want(sequence,state,index,now)
 latest={epoch=epoch,sequence=sequence,state=state,dock=index,identity='saved-ship-1',received_at=now}
 return C:desire(1,latest,now)
end
function enqueue(now)
 local state,operation=E:queue(ready(now),now,false);assert(state=='queued',state);return operation
end
''')
        return lua

    def test_valid_window_requires_consumption_ack_and_independent_state_readback(self):
        self.make().execute('''
want(1,'EXTERNAL',1,0);local op=enqueue(0)
assert(op==1 and E:busy() and not E:window_command() and #prepares==1 and #applies==0)
assert(E:window(op,true,.1,false)=='issued' and #applies==1 and #issued==1)
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:prepare(1,.2,false)=='waiting')
assert(E:acknowledge(op,'applied',.3)=='accepted' and not E:busy())
assert(C:prepare(1,.3,false)=='settled')
assert(E:acknowledge(op,'rejected',.4)=='stale')
''')

    def test_old_window_expired_observation_and_pause_never_dispatch(self):
        for intervention in (
            "assert(E:window(op,true,.51,false)=='expired')",
            "latest.received_at=-1;assert(E:window(op,true,.1,false)=='expired')",
            "latest=nil;assert(E:window(op,true,.1,false)=='expired')",
            "want(2,'FREE',0,.1);assert(E:window(op,true,.2,false)=='expired')",
            "assert(E:window(op,true,.1,true)=='paused')",
            "assert(E:frame(.6)=='expired');assert(E:window(op,true,.6,false)=='stale')",
        ):
            with self.subTest(intervention=intervention):
                self.make().execute("want(1,'EXTERNAL',1,0);local op=enqueue(0);"+intervention+'''
assert(#applies==0 and #issued==0 and not E:busy())
''')

    def test_observation_requires_full_native_expiry_margin_at_both_boundaries(self):
        self.make().execute('''
want(1,'EXTERNAL',1,0)
assert(E:queue(ready(.76),.76,false)=='expired' and #prepares==0)
want(2,'EXTERNAL',1,1);local op=enqueue(1.5)
assert(E:window(op,true,1.76,false)=='expired' and #applies==0)
''')

    def test_native_rejection_is_consumed_and_cannot_release_a_retry(self):
        self.make().execute('''
want(1,'EXTERNAL',1,0);local op=enqueue(0)
assert(E:window(op,false,.1,false)=='rejected' and #applies==0)
local retry=enqueue(.2);assert(retry~=op)
assert(E:window(op,true,.2,false)=='stale')
assert(E:window(retry,true,.3,false)=='issued')
assert(E:acknowledge(retry,'rejected',.4)=='rejected')
local third=enqueue(.5);assert(E:window(third,true,.6,false)=='issued')
assert(E:acknowledge(retry,'rejected',.6)=='stale' and E:busy())
assert(E:acknowledge(third,'applied',.7)=='accepted')
assert(C:prepare(1,.7,false)=='waiting' and #applies==2)
''')

    def test_unknown_missing_or_late_native_outcome_never_means_no_mutation(self):
        for action in ("E:acknowledge(op,'unknown',.2)", "E:frame(5.2)", "E:acknowledge(op,'rejected',5.2)"):
            with self.subTest(action=action):
                self.make().execute("want(1,'EXTERNAL',1,0);local op=enqueue(0);E:window(op,true,.1,false);"+action+'''
assert(E:failed() and C:prepare(1,6,false)=='stopped' and #applies==1)
''')

    def test_direct_dispatch_outside_correlated_window_fails_closed(self):
        self.make().execute('''
want(1,'EXTERNAL',1,0);local ticket=ready(0)
assert(C:execute(ticket,.1,false)=='failed' and #applies==0)
assert(not pcall(E.dispatch,E,issued[1]))
''')

    def test_queued_then_dock_rechecks_the_controller_before_native_apply(self):
        self.make().execute('''
want(1,'EXTERNAL',1,0);local op=enqueue(0)
ship.state='STORED';ship.dock=2;ship.container=300
assert(E:window(op,true,.1,false)=='changed' and #applies==0 and not E:busy())
''')

    def test_cancel_releases_unissued_windows_but_keeps_issued_fences(self):
        self.make().execute('''
want(1,'EXTERNAL',1,0);local op=enqueue(0);E:cancel(1)
assert(E:window(op,true,.1,false)=='stale' and #applies==0)
want(2,'EXTERNAL',1,.2);op=enqueue(.2);E:window(op,true,.3,false);E:cancel(1)
assert(E:busy() and C:prepare(1,.4,false)=='waiting')
assert(E:acknowledge(op,'applied',.5)=='accepted')
assert(C:prepare(1,.6,false)=='waiting')
ship.state='EXTERNAL';ship.dock=1;ship.container=300
assert(C:prepare(1,.7,false)=='inactive')
''')

    def test_config_mutation_clock_rollback_and_foreign_ack_cannot_widen_authority(self):
        self.make().execute('''
want(1,'EXTERNAL',1,0);local op=enqueue(0)
execOptions.ttl=60;execOptions.apply=function() error('replaced') end
assert(E:window(op,true,.1,false)=='issued' and #applies==1)
assert(E:acknowledge(op+1,'rejected',.2)=='stale' and E:busy())
assert(E:frame(.1)=='failed' and E:failed())
assert(E:acknowledge(op,'rejected',.3)=='failed')
''')


if __name__=='__main__':unittest.main()
