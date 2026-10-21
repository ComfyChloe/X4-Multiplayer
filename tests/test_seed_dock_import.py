"""Exercise ordered local-MD import and correlated live queries in real LuaJIT."""
from pathlib import Path
import unittest
from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]


class DockImportTests(unittest.TestCase):
    def make(self, count=2):
        lua = LuaRuntime(unpack_returned_tuples=True)
        for name in ('seed_docks', 'seed_dock_import'):
            lua.execute((ROOT / f'extension/x4_lan_lab/ui/{name}.lua').read_text())
        lua.globals().total = count
        lua.execute('''
seed=string.rep('a',32); sent={}; I=X4LAN_SEED_DOCK_IMPORT.new(seed,'load',function(v) sent[#sent+1]=v end)
function event(kind,s,now) return I:receive(kind,seed..'|'..s,now or 1) end
function begin(first,last,seq,now) return event('Begin',string.format('%d|%d|%d|%d',seq or 0,first,last,total),now) end
function row(i,seq,object,now)
 return event('Row',string.format('%d|%d|%s|0xa|0xa|dock_a_macro|ship_carrier_macro|1|0|0|0.001',
 seq or 0,i,object or string.format('0x%x',i+10)),now)
end
function finish(first,last,seq,now) return event('End',string.format('%d|%d|%d|%d|1',seq or 0,first,last,total),now) end
function collect()
 for first=1,total,64 do
  assert(I:frame(1));local last=math.min(first+63,total)
  assert(begin(first,last));for i=first,last do assert(row(i)) end;assert(finish(first,last))
 end
 assert(I:ready())
end
function rejects(fn) assert(not pcall(fn)) end
''')
        return lua

    def test_complete_batches_only_publish_once_and_preserve_saved_indices(self):
        self.make(65).execute('''
collect();assert(#sent==2 and sent[1].start==1 and sent[2].start==65)
assert(I:index(11)==1 and I:index(75)==65 and #I:fingerprints()==65)
assert(I:frame(2) and #sent==2)
local map=I:fingerprints();map[1]='changed';assert(I:fingerprints()[1]~='changed')
''')

    def test_missing_duplicate_reordered_rows_and_changed_bounds_never_publish(self):
        for script in (
            "assert(begin(1,2));assert(row(1));assert(not finish(1,2))",
            "assert(begin(1,2));assert(row(1));assert(not row(1))",
            "assert(begin(1,2));assert(not row(2))",
            "assert(not row(1))",
            "assert(not begin(1,1))",
            "assert(begin(1,2));assert(not begin(1,2))",
            "assert(begin(1,2));assert(row(1));assert(row(2));assert(not event('End','0|1|2|2|0'))",
        ):
            with self.subTest(script=script):
                self.make().execute('assert(I:frame(0));' + script + '''
assert(not I:ready() and I:error() and I:index(11)==nil)
assert(not I:frame(2));rejects(function() I:fingerprints() end)
''')

    def test_large_native_handles_do_not_round_through_double(self):
        self.make().execute('''
assert(I:frame(0));assert(begin(1,2))
assert(row(1,0,'0x20000000000000'));assert(row(2,0,'0x20000000000001'));assert(finish(1,2))
assert(I:index('9007199254740992')==1 and I:index('9007199254740993')==2)
''')

    def test_query_requires_fresh_correlated_complete_native_response(self):
        self.make().execute('''
collect();local calls=0
assert(I:query(1,'EXTERNAL',2,function(r,now)
 calls=calls+1;assert(tostring(r.object)=='11ULL' and now==2)
end));assert(sent[2].serial==1 and sent[2].start==1)
assert(begin(1,1,1,2));assert(row(1,1,nil,2));assert(calls==0)
assert(finish(1,1,1,2));assert(calls==1 and I:ready())
assert(I:query(2,'EXTERNAL',3,function() calls=calls+1 end))
assert(event('Row','1|1|ignored',3));assert(calls==1) -- old query cannot fulfill new request
assert(begin(2,2,2,3));assert(row(2,2,nil,3));assert(finish(2,2,2,3));assert(calls==2)
''')

    def test_type_mismatch_is_local_but_changed_witness_invalidates_registry(self):
        self.make().execute('''
collect();rejects(function() I:query(1,'STORED',2,function() end) end);assert(I:ready())
assert(I:query(1,'EXTERNAL',2,function() error('must not run') end))
assert(begin(1,1,1,2));assert(not row(1,1,'0xff',2));assert(not I:ready())
''')

    def test_bad_handle_relative_pose_seed_and_timeout_fail_closed(self):
        for mutation in ('0x0', '0x10000000000000000', '11', '0xxyz'):
            with self.subTest(handle=mutation):
                lua=self.make()
                lua.globals().bad=mutation
                lua.execute('assert(I:frame(0));assert(begin(1,2));assert(not row(1,0,bad));assert(not I:ready())')
        self.make().execute("assert(I:frame(0));assert(not I:frame(6));assert(I:error())")
        self.make().execute("assert(I:frame(1));assert(not I:frame(0));assert(I:error())")
        self.make().execute("assert(I:frame(0));assert(not I:receive('Begin',string.rep('b',32)..'|0|1|2|2',1))")
        for error in ('nan', 'inf', '0.051', '-1'):
            with self.subTest(pose_error=error):
                self.make().execute("assert(I:frame(0));assert(begin(1,2));assert(not event('Row','0|1|0xb|0xa|0xa|dock_a_macro|ship_carrier_macro|1|0|0|" + error + "'))")

    def test_native_probe_waits_for_pause_before_mark_and_each_import_step(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''
paused=false;pauses=0;sent={};handlers={};steps=0
X4LAN_CONFIG={enabled=false,test_profile='45841',seed_dock_import_probe=true}
function require(name) assert(name=='ffi');return {C={IsGamePaused=function() return paused end}} end
function Pause() pauses=pauses+1 end
function DebugError() end
function GetCurRealTime() return 0 end
function AddUITriggeredEvent(screen,event,value) sent[#sent+1]={screen,event,value} end
function RegisterEvent(name,fn) handlers[name]=fn end
X4LAN_SEED_DOCK_IMPORT={new=function() return {
 frame=function() assert(paused);steps=steps+1;return true end,
 ready=function() return false end} end}
''')
        lua.execute((ROOT/'dev/seed_dock_import_probe.lua').read_text())
        lua.execute('''
local D=X4LAN_SEED_DOCKS
D.start('capture',string.rep('a',32),function() error('not complete') end)
assert(#sent==0);D.frame(1);assert(pauses==1 and #sent==0 and steps==0)
paused=true;D.frame(2);assert(#sent==1 and sent[1][2]=='Mark' and steps==0)
handlers['X4LAN_SeedDocks.Marked'](nil,string.rep('a',32))
D.frame(3);assert(steps==1)
paused=false;D.frame(4);assert(pauses==2 and steps==1)
paused=true;D.frame(5);assert(steps==2 and #sent==1)
''')


if __name__ == '__main__':
    unittest.main()
