"""Dev-only Windows projectile-switch readiness probe (dev/visual_switch_probe.lua) on doubles."""
import unittest
from pathlib import Path
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

DEV = Path(__file__).resolve().parents[1] / 'dev'
SETUP = '''
 X4LAN_CONFIG.visual_switch_probe=true;X4LAN_CONFIG.test_profile='45841'
 local base=require;require=function(n) local m=base(n)
  if n=='ffi' then m.new=function(name,x) local t={} if type(x)=='number' then for i=0,x-1 do t[i]={} end end return t end end
  return m end
 O,Z='bullet_ter_s_plasma_01_mk1_macro','x4lan_visual_plasma_bullet_macro'
 ship=11;slots={17,0,18,19};macros={['17']='weapon_ter_s_plasma_01_mk1_macro',['18']='weapon_ter_s_plasma_01_mk1_macro',
  ['19']='weapon_gen_s_laser_01_mk1_macro'}
 launched={[17]=O,[18]=O,[19]='bullet_gen_s_laser_01_mk1_macro'};mods={[17]=true};writes={}
 grp={[17]={{primary=true,idx=1}},[18]={{primary=true,idx=1},{primary=false,idx=3}},[19]={}}
 ConvertStringTo64Bit=function(v) return v end
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
 api.SetWeaponLaunchedMacro=function(id,macro) writes[#writes+1]=id..'='..macro;setter(id,macro) end
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
         tick(5);assert(logged('PASS') and logged('slot=3') and logged('groups=P1,S3'))
         assert(#writes==2 and writes[1]=='18='..Z and writes[2]=='18='..O and launched[18]==O)
         assert(launched[17]==O and launched[19]=='bullet_gen_s_laser_01_mk1_macro')
        ''')

    def test_a_refused_switch_fails_and_never_touches_another_gun(self):
        self.make('setter=function() end').execute('''
         tick(3);assert(logged('FAIL stage=1') and logged('CLEANUP stock restored') and not logged('PASS'))
         for _,w in ipairs(writes) do assert(w:sub(1,3)=='18=') end
        ''')

    def test_any_changed_identity_mod_or_group_fails_and_restores_stock(self):
        for change in ('grp[18]={{primary=true,idx=1}}', 'mods[18]=true', 'slots[3]=20;launched[20]=Z;macros["20"]=macros["18"]',
                       'launched[19]=Z', 'ship=12'):
            with self.subTest(change=change):
                self.make().execute('tick(1);' + change + ''';tick(3)
                 assert(logged('FAIL stage=2') and not logged('PASS') and launched[18]==O)
                 assert(logged('CLEANUP stock restored') and writes[#writes]=='18='..O)
                ''')

    def test_a_reverted_switch_is_caught_one_tick_later(self):
        self.make().execute('''
         tick(1);launched[18]=O;tick(1)
         assert(logged('FAIL stage=2') and logged('CLEANUP stock restored') and #writes==1)
        ''')

    def test_no_eligible_gun_writes_nothing(self):
        self.make('mods[18]=true;launched[19]="x4lan_visual_pulse_mk1_bullet_macro"').execute('''
         tick(2);assert(logged('FAIL stage=1 ') and logged('no unmodified original stock gun') and #writes==0)
         assert(not logged('CLEANUP'))
        ''')

    def test_inert_without_its_flag_or_outside_the_test_profile(self):
        for before in ('X4LAN_CONFIG.visual_switch_probe=false', 'X4LAN_CONFIG.test_profile="1"'):
            with self.subTest(before=before):
                self.make(before).execute("assert(events['X4LAN_VisualSwitchProbe.Tick']==nil and #writes==0)")


if __name__ == '__main__':
    unittest.main()
