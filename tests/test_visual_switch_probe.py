"""Dev-only Windows projectile-switch readiness probe (tools/dev/visual_switch_probe.lua) on doubles."""
import re
import unittest
from pathlib import Path
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / 'tools/dev'
SETUP = '''
 X4LAN_CONFIG.visual_switch_probe=true;X4LAN_CONFIG.test_profile='45841'
 local base=require;require=function(n) local m=base(n)
  if n=='ffi' then m.new=function(name,x) local t={} if type(x)=='number' then for i=0,x-1 do t[i]={} end end return t end
   m.typeof=function(name) assert(name~=badtype,'undeclared '..name);return name end end
  return m end
 O,Z='bullet_ter_s_plasma_01_mk1_macro','x4lan_visual_plasma_bullet_macro'
 -- Native-shaped UniverseIDs: uint64 cdata print as '18ULL'; GetComponentData takes Lua IDs only.
 I={};for n=17,20 do I[n]=setmetatable({n=n},{__tostring=function(s) return s.n..'ULL' end}) end
 ship=11;slots={I[17],0,I[18],I[19]};macros={L17='weapon_ter_s_plasma_01_mk1_macro',L18='weapon_ter_s_plasma_01_mk1_macro',
  L19='weapon_gen_s_laser_01_mk1_macro'}
 launched={[I[17]]=O,[I[18]]=O,[I[19]]='bullet_gen_s_laser_01_mk1_macro'};mods={[I[17]]=true};writes={}
 grp={[I[17]]={{primary=true,idx=1}},[I[18]]={{primary=true,idx=1},{primary=false,idx=3}},[I[19]]={}}
 ConvertStringTo64Bit=function() error('C-ID bridge used for a Lua ID') end
 ConvertStringToLuaID=function(s) assert(s:match('^%d+$'),'not a decimal ID '..s);return 'L'..s end
 GetComponentData=function(id,key) assert(key=='macro');return macros[id] end
 api.GetPlayerControlledShipID=function() return ship end
 api.GetNumUpgradeSlots=function(s,m,kind) assert(s==11 and kind=='weapon');return #slots end
 api.GetUpgradeSlotCurrentComponent=function(s,kind,slot) return slots[slot] end
 api.IsComponentOperational=function() return true end
 api.GetInstalledWeaponMod=function(id) return mods[id] or false end
 api.GetNumWeaponGroupsByWeapon=function(s,id) return #grp[id] end
 api.GetWeaponGroupsByWeapon=function(raw,n,s,id) for i,g in ipairs(grp[id]) do raw[i-1]=g end;return #grp[id] end
 api.GetWeaponLaunchedMacro=function(id) return launched[id] end
 setter=function(id,macro) launched[id]=macro end
 api.SetWeaponLaunchedMacro=function(id,macro) writes[#writes+1]=tostring(id)..'='..macro;setter(id,macro) end
 function tick(n) for _=1,n do events['X4LAN_VisualSwitchProbe.Tick']() end end
 function logged(s) for _,l in ipairs(logs) do if l:find(s,1,true) then return true end end end
'''


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class VisualSwitchProbeTests(unittest.TestCase):
    def make(self, before=''):
        l = LuaRuntime(unpack_returned_tuples=True); l.execute(MOCK + EXTRA); l.execute(SETUP + before)
        l.execute((DEV / 'visual_switch_probe.lua').read_text()); return l

    def test_one_unmodified_original_gun_goes_stock_harmless_stock(self):
        self.make().execute('''
         tick(5);assert(logged('PASS') and logged('slot=3 id=18ULL') and logged('groups=P1,S3'))
         assert(#writes==2 and writes[1]=='18ULL='..Z and writes[2]=='18ULL='..O and launched[I[18]]==O)
         assert(launched[I[17]]==O and launched[I[19]]=='bullet_gen_s_laser_01_mk1_macro')
        ''')

    def test_a_refused_switch_fails_and_never_touches_another_gun(self):
        self.make('setter=function() end').execute('''
         tick(3);assert(logged('FAIL stage=1') and logged('CLEANUP stock restored') and not logged('PASS'))
         for _,w in ipairs(writes) do assert(w:sub(1,6)=='18ULL=') end
        ''')

    def test_any_changed_identity_mod_or_group_fails_and_restores_stock(self):
        for change in ('grp[I[18]]={{primary=true,idx=1}}', 'mods[I[18]]=true', 'slots[3]=I[20];launched[I[20]]=Z;macros.L20=macros.L18',
                       'launched[I[19]]=Z', 'ship=12'):
            with self.subTest(change=change):
                self.make().execute('tick(1);' + change + ''';tick(3)
                 assert(logged('FAIL stage=2') and not logged('PASS') and launched[I[18]]==O)
                 assert(logged('CLEANUP stock restored') and writes[#writes]=='18ULL='..O)
                ''')

    def test_a_reverted_switch_is_caught_one_tick_later(self):
        self.make().execute('''
         tick(1);launched[I[18]]=O;tick(1)
         assert(logged('FAIL stage=2') and logged('CLEANUP stock restored') and #writes==1)
        ''')

    def test_no_eligible_gun_writes_nothing(self):
        self.make('mods[I[18]]=true;launched[I[19]]="x4lan_visual_pulse_mk1_bullet_macro"').execute('''
         tick(2);assert(logged('FAIL stage=1 ') and logged('no unmodified original stock gun') and #writes==0)
         assert(not logged('CLEANUP'))
        ''')

    def test_the_actual_save_folder_must_be_the_isolated_profile(self):
        self.make('api.GetSaveFolderPath=function() return [[C:\\Users\\t\\X4\\45841\\save\\]] end').execute("tick(5);assert(logged('PASS'))")
        for path in ('C:\\Users\\t\\X4\\70000001\\save\\', '/X4/145841/save/', '/X4/45841/save/extra/'):
            with self.subTest(path=path):
                self.make('api.GetSaveFolderPath=function() return [[%s]] end' % path).execute("""
                 assert(logged('NOT ARMED') and logged('isolated profile 45841'))
                 assert(events['X4LAN_VisualSwitchProbe.Tick']==nil and #writes==0)
                """)

    def test_a_missing_native_function_or_ffi_type_never_arms(self):
        for before, reason in (('api.GetWeaponLaunchedMacro=nil', 'missing native GetWeaponLaunchedMacro'),
                               ('api.GetInstalledWeaponMod=nil', 'missing native GetInstalledWeaponMod'),
                               ("badtype='UIWeaponMod'", 'missing FFI type UIWeaponMod')):
            with self.subTest(before=before):
                self.make(before).execute("assert(logged('NOT ARMED') and logged('%s') and events['X4LAN_VisualSwitchProbe.Tick']==nil)" % reason)

    def test_its_declarations_parse_in_real_luajit_ffi_with_the_stock_layouts(self):
        src = (DEV / 'visual_switch_probe.lua').read_text()
        decls = re.findall(r"'([^']*;)'", src[src.index('for _,decl in ipairs({'):src.index('do pcall(ffi.cdef,decl)')])
        self.assertEqual(len(decls), 12)
        l = LuaRuntime(unpack_returned_tuples=True)
        l.execute("ffi=require('ffi');ffi.cdef('typedef uint64_t UniverseID;')")
        for d in decls:
            l.globals().decl = d; l.execute('ffi.cdef(decl)')
        l.execute("assert(ffi.sizeof('UIWeaponGroup')==8 and ffi.new('UIWeaponGroup[?]',2)[1].idx==0 and ffi.typeof('UIWeaponMod'))")
        shot = (ROOT / 'src/extension/x4_lan_lab/ui/shot_game.lua').read_text()
        stock = ' '.join(re.search(r'typedef struct \{[^}]*\} UIWeaponMod;', shot)[0].split())
        self.assertIn(stock, [' '.join(d.split()) for d in decls])

    def test_inert_without_its_flag_or_outside_the_test_profile(self):
        for before in ('X4LAN_CONFIG.visual_switch_probe=false', 'X4LAN_CONFIG.test_profile="1"'):
            with self.subTest(before=before):
                self.make(before).execute("assert(events['X4LAN_VisualSwitchProbe.Tick']==nil and #writes==0)")


if __name__ == '__main__':
    unittest.main()
