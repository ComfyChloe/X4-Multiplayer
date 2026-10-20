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
          C.GetUpgradeSlotGroup=function() return {path='..',group=''} end
          C.GetNumUpgradeGroups=function() return 0 end
          C.GetUpgradeGroups=function() return 0 end
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
           assert(loadout.numshieldgroups==0 and loadout.numturretgroups==0)
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

    def full_group_runtime(self,foreign_component=False,serialized=False):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.globals().foreign_component=foreign_component
        lua.globals().serialized_mode=serialized
        lua.execute('''
          X4LAN_CONFIG={test_profile='45841',loadout_roundtrip=true,loadout_saved_exact=true,loadout_fill_groups=true,loadout_probe_id='owned_probe'}
          X4LAN_CONFIG.loadout_serialized=serialized_mode
          local C={};local ffi={C=C,cdef=function() end,typeof=function() return true end,string=function(x) return x end}
          ffi.new=function(kind,n)
           if kind=='UILoadout2' then return {thruster={}} end
           local a={};for i=0,(n or 1)-1 do a[i]={weaponsetting={}} end;return a
          end
          require=function() return ffi end
          logs={};DebugError=function(s) logs[#logs+1]=s end
          local callback;RegisterEvent=function(_,f) callback=f end
          removed={};local dead={};local saved=false
          C.IsComponentOperational=function(o) return not dead[o] end
          C.ActivateObject=function(o,active) assert(o==2 and not active) end
          C.GetPlayerContainerID=function() return 999 end
          C.GetContextByClass=function(o) return foreign_component and 1 or 2 end
          macro_layout_reads=0
          C.GetNumUpgradeSlots=function(o,m,k)
           if o==0 then assert(m=='ship_test_macro');macro_layout_reads=macro_layout_reads+1 end
           return ({engine=1,turret=4})[k] or 0
          end
          C.IsUpgradeMacroCompatible=function(o,module,m,construction,k,i,macro) assert(o==0 and m=='ship_test_macro');return true end
          C.GetUpgradeSlotCurrentComponent=function(o,k,i)
           if k=='engine' then return o*100+1 end
           if o==1 then return i==4 and 404 or 0 end
           return dead[500+i] and 0 or 500+i
          end
          C.GetUpgradeSlotGroup=function(o,m,k) return {path='..',group=k=='turret' and 'back' or ''} end
          C.GetNumUpgradeGroups=function() return 1 end
          C.GetUpgradeGroups=function(out) out[0]={path='..',group='back'};return 1 end
          C.GetUpgradeGroupInfo=function(o,m,p,g,k) return {currentmacro=k=='turret' and 'turret_test_macro' or '',count=k=='turret' and 1 or 0} end
          C.GetNumVirtualUpgradeSlots=function() return 1 end
          C.GetVirtualUpgradeSlotCurrentMacro=function() return 'thruster_test_macro' end
          C.GetNumSoftwareSlots=function() return 0 end;C.GetSoftwareSlots=function() return 0 end
          C.GetNumLoadoutsInfo=function() return saved and 1 or 0 end
          C.GetLoadoutsInfo=function(out) if saved then out[0]={id='owned',name='owned_probe_1'};return 1 end;return 0 end
          C.SaveLoadout2=function(m,l)
           assert(l.numturrets==0 and l.numturretgroups==1 and l.turretgroups[0].count==4)
           saved=true
          end
          C.RemoveLoadout=function(_,_,id) assert(id=='owned');saved=false;preset_removed=true;return true end
          C.RemoveComponent=function(o)
           assert(o==2 or o==501 or o==502 or o==503,'source or desired equipment must never be removed')
           dead[o]=true;removed[#removed+1]=o
          end
          GetComponentData=function(o,...)
           local out={}
           for _,field in ipairs({...}) do
            if field=='macro' then out[#out+1]=o<=2 and 'ship_test_macro' or (o==101 or o==201) and 'engine_test_macro' or 'turret_test_macro'
            elseif field=='idcode' then out[#out+1]=o==1 and 'SRC-001' or 'NEW-002'
            elseif field=='hullmax' then out[#out+1]=1000
            elseif field=='shieldmax' then out[#out+1]=0 end
            if field=='owner' then out[#out+1]='argon' end
           end
           return unpack(out)
          end
          X4LAN_BUBBLE_API={id=function(x) return x end,paused=function() return false end,
           cargo={object=function() return 2 end},coop={entity=function() return 0 end},
           universe={ready=true,publish={{object=1}},frame=function() end}}
          AddUITriggeredEvent=function(screen,event,payload)
           assert(payload.fillgroups and payload.preset=='owned');callback(nil,'1|0x2')
          end
        ''')
        lua.execute((Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui/entity_descriptor.lua').read_text())
        lua.execute((Path(__file__).resolve().parents[1]/'dev/loadout_roundtrip_probe.lua').read_text())
        return lua

    def test_full_group_prunes_only_surplus_clone_slots_then_reads_actual_capacity(self):
        lua=self.full_group_runtime()
        lua.execute('''
          local U=X4LAN_BUBBLE_API.universe
          U.frame(1);assert(#removed==3 and not preset_removed)
          U.frame(2);assert(#removed==3);U.frame(3)
          assert(#removed==4 and removed[4]==2 and preset_removed)
          local exact,native=false,false
          for _,s in ipairs(logs) do
           if s:find('result id=1 exactSlots=true',1,true) then exact=true end
           if s:find('input=3 unused=0 sourceHull=1000 cloneHull=1000 sourceShield=0 cloneShield=0',1,true) then native=true end
          end
          assert(exact and native)
        ''')

    def test_foreign_surplus_component_stops_before_any_removal(self):
        lua=self.full_group_runtime(foreign_component=True)
        lua.execute('''
          local ok,err=pcall(X4LAN_BUBBLE_API.universe.frame,1)
          assert(not ok and tostring(err):find('not owned by clone',1,true))
          assert(#removed==0)
        ''')

    def test_serialized_inputs_rebuild_from_macro_layout_before_native_preset(self):
        lua=self.full_group_runtime(serialized=True)
        lua.execute('''
          local U=X4LAN_BUBBLE_API.universe
          U.frame(1);U.frame(2);U.frame(3)
          assert(macro_layout_reads==4 and #removed==4 and preset_removed)
          local descriptor,identity,exact=false,false,false;local markers=0
          for _,s in ipairs(logs) do
           if s:find('serialized-mode=true',1,true) then markers=markers+1 end
           descriptor=descriptor or s:find('descriptor id=1 bytes=',1,true)
           identity=identity or s:find('sourceMacro=ship_test_macro cloneMacro=ship_test_macro sourceOwner=argon cloneOwner=argon',1,true)
           exact=exact or s:find('result id=1 exactSlots=true',1,true)
          end
          assert(descriptor and identity and exact and markers==1)
        ''')
