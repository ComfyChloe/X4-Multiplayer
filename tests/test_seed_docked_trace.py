from pathlib import Path
import unittest
from test_game_adapter import LuaRuntime


@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class SeedDockedTraceTests(unittest.TestCase):
    def test_trace_preserves_native_writes_unless_exact_original_docked_omission_is_selected(self):
        for skip in (False,True):
            lua=LuaRuntime(unpack_returned_tuples=True)
            lua.execute('''
              X4LAN_CONFIG={test_profile='45841',seed_binding=true,slot='p2',seed_probe_trace_docked=true}
              local positions={[1]={x=1,y=0,z=0},[2]={x=2,y=0,z=0},[3]={x=3,y=0,z=0}}
              writes={}
              local native={SetObjectSectorPos=function(id,sector,p) writes[id]=(writes[id] or 0)+1;positions[id]=p end,
               GetObjectPositionInSector=function(id) local p=positions[id];return {x=p.x,y=p.y,z=p.z} end}
              C=setmetatable({},{__index=function(_,key) return native[key] end})
              require=function() return {C=C} end;DebugError=function() end
              GetComponentData=function(id,field)
               if field=='isdocked' then return id~=2 end
               return id==2 and 'ship_arg_xl_carrier_02_a_macro' or 'ship_arg_s_fighter_01_a_macro'
              end
              X4LAN_BUBBLE_API={id=function(v) return v end,identity={},universe={frame=function() end,latest={revision=1},
               seedObjects={index=function(_,object) return object end},
               replicas={[1]={object=1,seedOriginal=true},[2]={object=2,seedOriginal=true},[3]={object=3,seedOriginal=false}}}}
            ''')
            lua.globals().X4LAN_CONFIG.seed_probe_skip_docked=skip
            lua.execute((Path(__file__).resolve().parents[1]/'dev/seed_objects_probe.lua').read_text())
            lua.execute('for id=1,3 do C.SetObjectSectorPos(id,9,{x=100,y=0,z=0}) end;assert(writes[2]==1 and writes[3]==1)')
            lua.execute('assert(writes[1]==nil)' if skip else 'assert(writes[1]==1)')
