"""Saved dock witnesses and opaque 64-bit references; no native engine run."""
from pathlib import Path
import unittest

from lupa.luajit21 import LuaRuntime

SOURCE = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui/seed_docks.lua'


class SeedDockLedgerTests(unittest.TestCase):
    def make(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(SOURCE.read_text())
        lua.execute('''
D=X4LAN_SEED_DOCKS_LEDGER; seed=string.rep('b',32)
function copy(t) local c={};for k,v in pairs(t) do c[k]=v end;return c end
function entry(object,parent,container,storage)
 return {object=object,parent=parent,container=container,macro=storage and 'storage_a_macro' or 'dock_a_macro',
  container_macro='ship_carrier_macro',external=not storage,storage=storage,capacity=storage and 40 or 0}
end
function observed(def)
 local r=copy(def);r.alive=true;r.pose_error=0;return r
end
definitions={entry(11,10,10,false),entry(12,10,10,true),entry(21,20,20,false)}
live={};for _,d in ipairs(definitions) do live[D.handle(d.object)]=observed(d) end
function inspect(object) return live[D.handle(object)] end
R=D.registry(seed,3,inspect)
function collect() for i,d in ipairs(definitions) do R:observe(i,d) end;R:seal() end
function rejects(fn) assert(not pcall(fn)) end
''')
        return lua

    def test_full_ordered_registry_exposes_copies_only_after_seal(self):
        self.make().execute('''
assert(not R:ready() and R:index(11)==nil)
rejects(function() R:resolve(1,'EXTERNAL') end)
collect();assert(R:ready() and R:index(12)==2)
local row=R:resolve(1,'EXTERNAL');assert(row.object==11 and row.container==10)
row.object=999;definitions[1].macro='changed_macro'
assert(R:resolve(1,'EXTERNAL').object==11)
local map=R:fingerprints();map[1]='wrong';assert(R:fingerprints()[1]~='wrong')
assert(R:resolve(2,'STORED').capacity==40)
''')

    def test_incomplete_duplicate_and_out_of_order_rows_fail_closed(self):
        for script in ("R:observe(1,definitions[1]);R:seal()",
                       "R:observe(2,definitions[2])",
                       "R:observe(1,definitions[1]);R:observe(1,definitions[1])",
                       "R:observe(1,definitions[1]);R:observe(2,definitions[1])"):
            with self.subTest(script=script):
                self.make().execute('rejects(function() ' + script + ''' end)
assert(not R:ready() and R:index(11)==nil)
rejects(function() R:seal() end)
''')

    def test_renumbered_graph_has_same_fingerprint_but_split_or_merged_graph_does_not(self):
        self.make().execute('''
collect();original=R:fingerprints()
local function make(defs)
 local current={};for _,d in ipairs(defs) do current[D.handle(d.object)]=observed(d) end
 local other=D.registry(seed,3,function(o) return current[D.handle(o)] end)
 for i,d in ipairs(defs) do other:observe(i,d) end;other:seal();return other:fingerprints()
end
local renumbered=make({entry(31,30,30,false),entry(32,30,30,true),entry(41,40,40,false)})
for i=1,3 do assert(original[i]==renumbered[i]) end
local split=make({entry(31,30,30,false),entry(32,50,50,true),entry(41,40,40,false)})
assert(original[2]~=split[2])
local merged=make({entry(31,30,30,false),entry(32,30,30,true),entry(41,30,30,false)})
assert(original[3]~=merged[3])
''')

    def test_large_native_handles_remain_distinct_without_float_conversion(self):
        self.make().execute('''
local ffi=require('ffi')
local a=ffi.new('uint64_t',9007199254740992ULL)
local b=ffi.new('uint64_t',9007199254740993ULL)
assert(D.handle(a)=='9007199254740992' and D.handle(b)=='9007199254740993')
assert(D.handle('18446744073709551615')=='18446744073709551615')
assert(D.handle('18446744073709551616')==nil and D.handle(9007199254740992)==nil)
definitions={entry(a,10,10,false),entry(b,10,10,true)};live={}
for _,d in ipairs(definitions) do live[D.handle(d.object)]=observed(d) end
R=D.registry(seed,2,inspect)
R:observe(1,definitions[1]);R:observe(2,definitions[2]);R:seal()
assert(R:index(a)==1 and R:index(b)==2)
''')

    def test_live_reference_type_and_relative_pose_are_revalidated_before_use(self):
        for mutation in ("live['11'].parent=99", "live['11'].container=99",
                         "live['11'].macro='other_macro'", "live['11'].container_macro='other_macro'",
                         "live['11'].external=false", "live['11'].capacity=1",
                         "live['11'].alive=false", "live['11'].pose_error=.05001",
                         "live['11'].pose_error=0/0", "live['11'].pose_error=math.huge"):
            with self.subTest(mutation=mutation):
                self.make().execute('collect();' + mutation + '''
rejects(function() R:resolve(1,'EXTERNAL') end);assert(not R:ready())
rejects(function() R:resolve(2,'STORED') end)
''')

    def test_wrong_type_is_not_silently_substituted_for_another_bay(self):
        self.make().execute('''
collect();rejects(function() R:resolve(1,'STORED') end)
assert(R:ready() and R:resolve(1,'EXTERNAL').object==11)
assert(R:resolve(2,'STORED').object==12)
''')

    def test_unresolved_native_collection_never_publishes_a_partial_ledger(self):
        self.make().execute('''
R:observe(1,definitions[1]);live['12'].parent=99
rejects(function() R:observe(2,definitions[2]) end)
assert(not R:ready());rejects(function() R:fingerprints() end)
rejects(function() R:observe(3,definitions[3]) end)
''')

    def test_invalid_handles_and_limits_are_rejected(self):
        self.make().execute('''
for _,h in ipairs({'0','-1','01','1e3','nan','inf','',true,{}}) do assert(D.handle(h)==nil) end
rejects(function() D.registry(seed,250001,inspect) end)
rejects(function() D.registry(seed,0,inspect) end)
rejects(function() D.registry('wrong',1,inspect) end)
definitions[1].object=0;rejects(function() R:observe(1,definitions[1]) end)
assert(not R:ready())
''')


if __name__ == '__main__':
    unittest.main()
