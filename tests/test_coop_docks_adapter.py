"""Saved-bay observation/pose fences; these doubles do not prove native docking."""
from pathlib import Path
import unittest
import test_coop_adapter as coop
from test_game_adapter import LuaRuntime

UI=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'

@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class CoopDockAdapterTests(unittest.TestCase):
    def make(self):
        l=coop.CoopAdapterTests().make()
        l.execute("X4LAN_CONFIG.coop_docks=true;X4LAN_CONFIG.seed_id=string.rep('a',32);X4LAN_SEED_GATE={frame=function() return true end}")
        l.execute((UI/'coop_dock_game.lua').read_text())
        l.execute('''
         function dock(seq,mode,index,lease,epoch)
          client:receiveExtension({'CDOCK','1',client.lease,lease or string.rep('b',32),'2',tostring(epoch or 1),tostring(seq),mode,tostring(index or 0)},now)
         end
         function own(mode,index,ship)
          events['X4LAN_CoopDocks.Row'](nil,table.concat({'50','2026-10-24T00:01:00','2',ship or '42',mode,tostring(index or 0)},'|'))
         end
         now=10.2;frames.onUpdate();events['X4LAN_CoopDocks.Ready'](nil,X4LAN_CONFIG.seed_id)
         writes={};nav(2,1,1,'cluster_14_sector001_macro',50)
        ''')
        return l

    def test_missing_mismatched_or_attached_state_never_authorizes_pose(self):
        l=self.make();l.execute('''
         own('FREE');now=10.21;frames.onUpdate();assert(#writes==0)
         dock(1,'FREE',0,string.rep('c',32));now=10.22;frames.onUpdate();assert(#writes==0)
         dock(1,'FREE');now=10.23;frames.onUpdate();assert(#writes==1)
         writes={};nav(2,1,2,'cluster_14_sector001_macro',60)
         now=10.24;frames.onUpdate();assert(#writes==0)
         dock(2,'EXTERNAL',12);now=10.25;frames.onUpdate();assert(#writes==0)
         assert(countRequest('Dock')==0)
        ''')

    def test_free_pair_needs_fresh_matching_native_ship_and_attachment(self):
        for action in ("own('EXTERNAL',12)", "own('UNKNOWN')", "now=10.6",
                       "GetComponentData=function(id,key) if key=='isdocked' then return true end;return 'cluster_14_sector001_macro' end"):
            l=self.make();l.execute("own('FREE');dock(1,'FREE');"+action+";frames.onUpdate();assert(#writes==0)")
        l=self.make();l.execute("own('FREE',0,'999');dock(1,'FREE');frames.onUpdate();assert(#writes==0)")

    def test_stale_epoch_and_older_free_cannot_reopen_an_attached_row(self):
        l=self.make();l.execute('''
         own('FREE');dock(3,'EXTERNAL',12);dock(2,'FREE')
         nav(2,1,3,'cluster_14_sector001_macro',80);frames.onUpdate();assert(#writes==0)
         auth(2,'p1',1,'p1','p2',2,'p2');nav(2,2,1,'cluster_14_sector001_macro',90)
         dock(4,'FREE',0,nil,1);frames.onUpdate();assert(#writes==0)
         dock(1,'FREE',0,nil,2);now=10.23;frames.onUpdate();assert(#writes==1)
        ''')

    def test_publish_uses_nav_sequence_and_unknown_when_native_readback_missing(self):
        l=self.make();l.execute('''
         local c=X4LAN_BUBBLE_API.coop
         c.publishDock(1,17,now)
         assert(sent[#sent]:match('|1|1|17|UNKNOWN|0$'))
         events['X4LAN_CoopDocks.Row'](nil,'50|2026-10-24T00:01:00|1|11|FREE|0')
         c.publishDock(1,18,now);assert(sent[#sent]:match('|1|1|18|FREE|0$'))
         events['X4LAN_CoopDocks.Error'](nil,'missing saved dock registry')
         c.publishDock(1,19,now);assert(sent[#sent]:match('|1|1|19|UNKNOWN|0$'))
        ''')

if __name__=='__main__':unittest.main()
