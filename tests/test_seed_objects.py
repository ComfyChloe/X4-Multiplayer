"""Saved reference indices stay distinct even when native display codes collide."""
from pathlib import Path
import unittest
from lupa.luajit21 import LuaRuntime

SOURCE=Path(__file__).resolve().parents[1]/"extension/x4_lan_lab/ui/seed_objects.lua"

class SeedObjectsTests(unittest.TestCase):
    def make(self):
        lua=LuaRuntime(unpack_returned_tuples=True);lua.execute(SOURCE.read_text())
        lua.execute("""
alive={[1001]=true,[2002]=true,[3003]=true};protected={}
B=X4LAN_SEED_OBJECTS.registry(function(o) return alive[o] end,function(o) return protected[o] end)
function rejects(fn) local ok=pcall(fn);assert(not ok) end
""")
        return lua

    def test_saved_index_binds_original_and_survives_view_release(self):
        self.make().execute("""
B:observe(77,2002,'ship_test_macro','argon','ABC-123')
assert(B:index(2002)==77)
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==2002)
assert(B:release(77)==2002 and alive[2002])
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==2002)
""")

    def test_duplicate_display_codes_still_bind_distinct_saved_objects(self):
        self.make().execute("""
B:observe(77,1001,'ship_test_macro','argon','ABC-123')
B:observe(78,2002,'ship_test_macro','argon','ABC-123')
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==1001)
assert(B:resolve('epoch',78,'ABC-123','ship_test_macro','argon')==2002)
""")

    def test_missing_and_host_created_objects_are_not_invented(self):
        self.make().execute("""
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==nil)
assert(B:resolve('epoch',250001,'ABC-123','ship_test_macro','argon')==nil)
assert(next(B.ids)==nil)
B:observe(77,1001,'ship_test_macro','argon','ABC-123')
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==1001)
""")

    def test_saved_reference_or_descriptor_epoch_and_lifetime_changes_are_rejected(self):
        self.make().execute("""
B:observe(77,1001,'ship_test_macro','argon','ABC-123')
rejects(function() B:observe(77,2002,'ship_test_macro','argon','ABC-123') end)
rejects(function() B:resolve('epoch',77,'ABC-123','other_macro','argon') end)
rejects(function() B:resolve('epoch',77,'ABC-123','ship_test_macro','player') end)
B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')
rejects(function() B:resolve('other_epoch',77,'ABC-123','ship_test_macro','argon') end)
alive[1001]=false
rejects(function() B:resolve('epoch',77,'ABC-123','ship_test_macro','argon') end)
""")

    def test_equal_cdata_components_cannot_occupy_two_seed_indices(self):
        self.make().execute("""
local ffi=require('ffi');local a,b=ffi.new('uint64_t',1001),ffi.new('uint64_t',1001)
B=X4LAN_SEED_OBJECTS.registry(function() return true end,function() return false end)
B:observe(77,a,'ship_test_macro','argon','ABC-123')
rejects(function() B:observe(78,b,'ship_test_macro','argon','DEF-456') end)
""")

    def test_player_and_service_references_are_never_taken_over(self):
        self.make().execute("""
B:observe(77,1001,'ship_test_macro','player','ABC-123');protected[1001]=true
rejects(function() B:resolve('epoch',77,'ABC-123','ship_test_macro','player') end)
rejects(function() B:observe(250000,2002,'ship_test_macro','argon','DEF-456') end)
""")

if __name__=='__main__': unittest.main()
