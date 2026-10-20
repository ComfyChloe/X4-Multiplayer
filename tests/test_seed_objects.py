"""Native identity rules without depending on the engine's numeric handles."""
from pathlib import Path
import unittest
from lupa.luajit21 import LuaRuntime

SOURCE = Path(__file__).resolve().parents[1] / "extension/x4_lan_lab/ui/seed_objects.lua"


class SeedObjectsTests(unittest.TestCase):
    def make(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(SOURCE.read_text())
        lua.execute('''
alive={[1001]=true,[2002]=true,[3003]=true};protected={}
B=X4LAN_SEED_OBJECTS.registry(function(o) return alive[o] end,function(o) return protected[o] end)
function rejects(fn) local ok=pcall(fn);assert(not ok) end
''')
        return lua

    def test_remote_id_binds_original_local_component_and_survives_view_release(self):
        self.make().execute('''
assert(B:observe('ABC-123',2002,'ship_test_macro','argon'))
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==2002)
assert(B:release(77)==2002 and alive[2002])
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==2002)
''')

    def test_unknown_code_does_not_invent_a_component_and_late_discovery_resolves(self):
        self.make().execute('''
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==nil)
assert(next(B.ids)==nil)
B:observe('ABC-123',1001,'ship_test_macro','argon')
assert(B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')==1001)
''')

    def test_ambiguous_codes_and_multiple_network_ids_fail(self):
        self.make().execute('''
B:observe('ABC-123',1001,'ship_test_macro','argon')
rejects(function() B:observe('ABC-123',2002,'ship_test_macro','argon') end)
B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')
rejects(function() B:resolve('epoch',78,'ABC-123','ship_test_macro','argon') end)
''')

    def test_equal_native_cdata_handles_cannot_bind_to_two_shared_objects(self):
        self.make().execute('''
local ffi=require('ffi')
local a,b=ffi.new('uint64_t',1001),ffi.new('uint64_t',1001)
B=X4LAN_SEED_OBJECTS.registry(function() return true end,function() return false end)
B:observe('ABC-123',a,'ship_test_macro','argon')
B:observe('DEF-456',b,'ship_test_macro','argon')
B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')
rejects(function() B:resolve('epoch',78,'DEF-456','ship_test_macro','argon') end)
''')

    def test_descriptor_epoch_and_bound_lifetime_changes_are_rejected(self):
        self.make().execute('''
B:observe('ABC-123',1001,'ship_test_macro','argon')
rejects(function() B:resolve('epoch',77,'ABC-123','other_macro','argon') end)
rejects(function() B:resolve('epoch',77,'ABC-123','ship_test_macro','player') end)
B:resolve('epoch',77,'ABC-123','ship_test_macro','argon')
rejects(function() B:resolve('other_epoch',77,'ABC-123','ship_test_macro','argon') end)
alive[1001]=false
B:observe('ABC-123',2002,'ship_test_macro','argon')
rejects(function() B:resolve('epoch',77,'ABC-123','ship_test_macro','argon') end)
''')

    def test_player_and_fixture_handles_are_protected_even_after_observation(self):
        self.make().execute('''
protected[1001]=true
assert(not B:observe('ABC-123',1001,'ship_test_macro','player'))
B:observe('DEF-456',2002,'ship_test_macro','player');protected[2002]=true
rejects(function() B:resolve('epoch',77,'DEF-456','ship_test_macro','player') end)
''')


if __name__ == '__main__':
    unittest.main()
