from pathlib import Path
import unittest
from test_game_adapter import LuaRuntime


@unittest.skipUnless(LuaRuntime,'LuaJIT unavailable')
class ExactLoadoutProbeTests(unittest.TestCase):
    def test_slot_descriptor_reaches_preset_builder_without_group_reordering(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''
          X4LAN_CONFIG={test_profile='45841',loadout_roundtrip=true,loadout_saved_exact=true,loadout_probe_id='owned_probe'}
          local C={};local ffi={C=C,cdef=function() end,typeof=function() return true end,string=function(x) return x end}
          ffi.new=function(kind,n)
           if kind=='UILoadout2' then return {thruster={}} end
           local a={};for i=0,(n or 1)-1 do a[i]={weaponsetting={}} end;return a
          end
          require=function() return ffi end
          DebugError=function() end;RegisterEvent=function() end
          C.IsComponentOperational=function() return true end
          C.GetNumUpgradeSlots=function(o,m,k) return ({engine=1,weapon=2})[k] or 0 end
          C.GetUpgradeSlotCurrentComponent=function(o,k,i) return k=='engine' and 101 or 200+i end
          C.GetNumVirtualUpgradeSlots=function() return 1 end
          C.GetVirtualUpgradeSlotCurrentMacro=function() return 'thruster_gen_s_combat_01_mk3_macro' end
          C.GetNumSoftwareSlots=function() return 1 end
          C.GetSoftwareSlots=function(out) out[0].current='software_dockmk2';return 1 end
          local saved
          C.GetNumLoadoutsInfo=function() return saved and 1 or 0 end
          C.GetLoadoutsInfo=function(out) if saved then out[0]={id='local_owned',name='owned_probe_1'};return 1 end;return 0 end
          C.SaveLoadout2=function(macro,loadout,source,id,overwrite,name)
           assert(macro=='ship_arg_s_fighter_01_a_macro' and source=='local' and id=='owned_probe_1' and not overwrite)
           assert(loadout.numengines==1 and loadout.numweapons==2 and loadout.numshields==0 and loadout.numturrets==0)
           assert(loadout.weapons[0].slot==0 and loadout.weapons[0].macro=='weapon_gen_s_plasma_01_mk1_macro')
           assert(loadout.weapons[1].slot==1 and loadout.weapons[1].macro=='weapon_gen_s_laser_01_mk1_macro')
           assert(loadout.weapons[1].weaponsetting.ammomacroname=='')
           assert(loadout.numsoftware==1 and loadout.software[0].ware=='software_dockmk2')
           assert(loadout.thruster.macro=='thruster_gen_s_combat_01_mk3_macro');saved=true
          end
          GetComponentData=function(o) return ({[1]='ship_arg_s_fighter_01_a_macro',[101]='engine_arg_s_combat_01_mk1_macro',
           [201]='weapon_gen_s_plasma_01_mk1_macro',[202]='weapon_gen_s_laser_01_mk1_macro'})[o] end
          X4LAN_BUBBLE_API={id=function(x) return x end,paused=function() return false end,
           universe={ready=true,publish={{object=1}},frame=function() end}}
          AddUITriggeredEvent=function(screen,event,payload)
           assert(screen=='X4LAN_LoadoutRoundtrip' and event=='Create' and payload.preset=='local_owned')
           delivered=true
          end
        ''')
        lua.execute((Path(__file__).resolve().parents[1]/'dev/loadout_roundtrip_probe.lua').read_text())
        lua.execute('X4LAN_BUBBLE_API.universe.frame(1);assert(delivered)')
