"""Causal execution acknowledgements and native readback waits, not engine mocks as acceptance."""
from pathlib import Path
import unittest
import test_coop_docks_adapter as obs
from test_game_adapter import LuaRuntime
UI=Path(__file__).resolve().parents[1]/'src/extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class CoopDockExecutionTests(unittest.TestCase):
 def make(self):
  l=obs.CoopDockAdapterTests().make();l.execute((UI/'coop_dock_exec.lua').read_text())
  l.execute('''
   d=X4LAN_BUBBLE_API.coopDocks;x=d.execution
   local read=GetComponentData
   GetComponentData=function(id,k) if k=='isdocked' then return nativeDocked==true and tonumber(id)==42 end;return read(id,k) end
   function nativeEvent(name,suffix) events['X4LAN_CoopAttachment.'..name](nil,X4LAN_CONFIG.seed_id..'|'..x.epoch..(suffix and '|'..suffix or '')) end
   function lastExec(name)
    local last;for _,r in ipairs(requests) do if r[1]=='X4LAN_CoopAttachment' and r[2]==name then last=r[3] end end;return last
   end
   function ackFences()
    for e=1,2 do nativeEvent('Fenced',e..'|'..x.generation[e]) end
   end
   function nextPose(seq,mode,index)
    nav(2,1,seq,'cluster_14_sector001_macro',50+seq);dock(seq,mode,index)
   end
   own('FREE');dock(1,'FREE');frames.onUpdate();nativeEvent('Ready')
   frames.onUpdate()
  ''');return l

 def test_native_fence_ack_is_required_before_free_pose_or_queued_warp(self):
  l=self.make();l.execute('''
   assert(#writes==0 and not d.authorized(2,now))
   events['X4LAN_CoopAttachment.Fenced'](nil,'2|0');frames.onUpdate();assert(#writes==0)
   ackFences();now=10.21;frames.onUpdate();assert(#writes==1 and d.authorized(2,now))
   own('FREE');dock(2,'FREE');nav(2,1,2,'cluster_01_sector001_macro',100)
   now=10.22;frames.onUpdate();events['X4LAN_Coop.Sector.cluster_01_sector001_macro'](nil,23)
   now=10.23;frames.onUpdate()
   local q;for _,r in ipairs(requests) do if r[2]=='SectorMove' then q=r[3] end end
   assert(q and q.dockGeneration==x.generation[2] and q.dockAge==50 and q.dockStamp=='2026-10-24T00:01:00')
  ''')

 def test_changed_intent_closes_prepare_before_late_window_or_apply_ack(self):
  l=self.make();l.execute('''
   ackFences();nextPose(2,'EXTERNAL',12);frames.onUpdate();ackFences();frames.onUpdate()
   local p=x.pending;assert(p and p.action=='ATTACH')
   nextPose(3,'FREE',0)
   nativeEvent('Window',p.operation..'|1');assert(lastExec('Apply')==nil)
   frames.onUpdate();assert(x.pending==nil)
   nativeEvent('Applied',p.operation..'|applied');assert(x.pending==nil)
   assert(not d.authorized(2,now))
  ''')

 def test_undock_waits_for_actual_free_and_packet_pairing_does_not_restart_it(self):
  l=self.make();l.execute('''
   nativeDocked=true;own('EXTERNAL',12);nextPose(2,'FREE');ackFences();frames.onUpdate()
   local p=x.pending;assert(p and p.action=='UNDOCK')
   nativeEvent('Window',p.operation..'|1');nativeEvent('Applied',p.operation..'|applied')
   local op=p.operation
   for n=3,12 do
    now=now+.7;own('EXTERNAL',12)
    nav(2,1,n,'cluster_14_sector001_macro',50+n);frames.onUpdate()
    dock(n,'FREE');frames.onUpdate()
    assert(x.pending and x.pending.operation==op and x.operation==op)
    assert(#writes==0)
   end
   nativeDocked=false;own('FREE');frames.onUpdate();assert(x.pending==nil and #writes==1)
  ''')

 def test_native_refusal_keeps_pose_blocked_and_retries_from_new_observation(self):
  l=self.make();l.execute('''
   nextPose(2,'EXTERNAL',12);frames.onUpdate();ackFences();frames.onUpdate()
   local op=x.pending.operation;nativeEvent('Window',op..'|0')
   assert(x.pending==nil and #writes==0);frames.onUpdate();assert(x.operation==op)
   now=now+.51;own('FREE');nextPose(3,'EXTERNAL',12);frames.onUpdate()
   assert(x.pending and x.pending.operation==op+1 and #writes==0)
  ''')

 def test_physical_undock_with_retained_assignment_cancels_before_pose(self):
  l=self.make();l.execute('''
   nativeDocked=true;own('EXTERNAL',12);nextPose(2,'FREE');ackFences();frames.onUpdate()
   local op=x.pending.operation
   nativeEvent('Window',op..'|1');nativeEvent('Applied',op..'|applied')
   now=now+.2;own('APPROACH',12);nextPose(3,'FREE');frames.onUpdate()
   assert(x.pending and x.pending.operation==op and #writes==0) -- still physically attached
   nativeDocked=false;now=now+.1;own('APPROACH',12);nextPose(4,'FREE');frames.onUpdate()
   assert(x.pending==nil and #writes==0)
   now=now+.51;own('APPROACH',12);nextPose(5,'FREE');frames.onUpdate()
   assert(x.pending and x.pending.action=='CANCEL' and #writes==0)
   op=x.pending.operation;nativeEvent('Window',op..'|1');nativeEvent('Applied',op..'|applied')
   own('FREE');frames.onUpdate();assert(x.pending==nil and #writes==1)
  ''')

if __name__=='__main__':unittest.main()
