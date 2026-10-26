import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_dynload", ROOT/"dev/build_dynload_station_probe.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class StationProbeTests(unittest.TestCase):
    def runtime(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''
          now=3; path='/test/45850/save/'; logs={}; events={}; requests=0
          moduleIds={[0]=201,[1]=202}; moduleMacros={[201]='module_a',[202]='module_b',[203]='module_b'}
          moduleCount=2; operational=true
          package.loaded.ffi={cdef=function()end, string=function(s)return s end,
            new=function()return moduleIds end, C={
              GetSaveFolderPath=function()return path end, IsPlayerValid=function()return true end,
              GetNumStationModules=function()return moduleCount end,
              GetStationModules=function()return moduleCount end,
              IsComponentOperational=function()return operational end}}
          GetCurTime=function()return 10 end; GetCurRealTime=function()return now end
          ConvertStringTo64Bit=function(s)return tonumber(s)end
          GetComponentData=function(id,key)return moduleMacros[id]end
          DebugError=function(s)logs[#logs+1]=s end
          RegisterEvent=function(name,fn)events[name]=fn end
          SetScript=function(name,fn)tick=fn end
          AddUITriggeredEvent=function()requests=requests+1 end
          DLCOR_EXPECTED={{plan='test_plan',macros={module_a=1,module_b=1}}}
        ''')
        lua.execute((ROOT/"dev/dynload_station_probe.lua").read_text())
        return lua

    def start(self, lua):
        lua.execute("tick();events['DLCOR.Station'](nil,{index=1,plan='test_plan',object='101'});now=3.2;tick()")

    def log(self, lua):
        return lua.eval("table.concat(logs,'\\n')")

    def test_two_correlated_samples_finish(self):
        lua=self.runtime();self.start(lua)
        lua.execute('now=8.3;tick()')
        text=self.log(lua)
        self.assertIn('station=101 phase=early',text)
        self.assertIn('station=101 phase=late',text)
        self.assertIn('DONE stations=1',text)

    def test_same_count_wrong_macro_is_failure(self):
        lua=self.runtime();lua.execute("moduleMacros[202]='module_c'");self.start(lua)
        self.assertIn('FAIL',self.log(lua));self.assertNotIn('DONE',self.log(lua))

    def test_module_identity_change_is_failure(self):
        lua=self.runtime();self.start(lua)
        lua.execute('moduleIds[1]=203;now=8.3;tick()')
        self.assertIn('identities changed',self.log(lua))

    def test_protected_profile_never_requests_creation(self):
        for profile in ('45841','45842','45843','45844','70000001'):
            lua=self.runtime();lua.globals().path=f'/test/{profile}/save/'
            lua.execute('tick()')
            self.assertEqual(lua.globals().requests,0)
            self.assertIn('FAIL',self.log(lua))

    def test_duplicate_station_and_missing_payload_refuse(self):
        for payload in ("{index=1,plan='test_plan',object='101'}", "'bad'"):
            lua=self.runtime();self.start(lua)
            lua.execute("events['DLCOR.Station'](nil,"+payload+")")
            self.assertIn('FAIL',self.log(lua))

    def test_missing_station_timeout(self):
        lua=self.runtime();lua.execute('tick();now=64;tick()')
        self.assertIn('evidence timeout',self.log(lua))

    def test_generator_preserves_existing_output_and_validates_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); manifest=p/'manifest.json';plans=p/'plans.xml';out=p/'ext'
            manifest.write_text(json.dumps({'stations':[{'plan':'a','macro':'station_a','owner':'argon','entries':2}]}))
            plans.write_text('<diff><add><plan id="a"><entry macro="module_a"/><entry macro="module_b"/></plan></add></diff>')
            BUILD.build(manifest,plans,out)
            xml=(out/'md/correlation.xml').read_text()
            self.assertIn('$object=$s',xml)
            self.assertNotIn('param2=',xml)
            self.assertIn('repeated_start UI_reload',xml)
            with self.assertRaisesRegex(ValueError,'output exists'):BUILD.build(manifest,plans,out)
            manifest.write_text(manifest.read_text().replace('"entries": 2','"entries": 3'))
            with self.assertRaisesRegex(ValueError,'count differ'):BUILD.build(manifest,plans,p/'other')


if __name__=='__main__':unittest.main()
