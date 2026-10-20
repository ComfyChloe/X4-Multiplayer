from pathlib import Path
import unittest
from lupa.luajit21 import LuaRuntime
from dev.validate_dock_registry import validate

SEED='b'*32
SOURCE=Path(__file__).resolve().parents[1]/'dev/dock_registry_probe.lua'


def trace(mode):
    finish=f'SEED_PROBE saved success=true id={SEED}' if mode=='capture' else f'SEED_PROBE loaded id={SEED}'
    return '\n'.join([f'SEED_DOCKS begin mode={mode} id={SEED} count=2',
        'SEED_DOCKS row index=1 object=0x11 container=0x10 macro=dock_a_macro containerMacro=ship_a_macro external=1 storage=0 capacity=0 reverse=1',
        'SEED_DOCKS row index=2 object=0x12 container=0x10 macro=dock_b_macro containerMacro=ship_a_macro external=0 storage=1 capacity=40 reverse=1',
        f'SEED_DOCKS end mode={mode} id={SEED} count=2',f'SEED_DOCKS complete mode={mode} id={SEED} count=2',finish])


class DockRegistryTests(unittest.TestCase):
    def make(self):
        l=LuaRuntime(unpack_returned_tuples=True)
        l.execute("""
X4LAN_CONFIG={dock_registry_probe=true,enabled=false,test_profile='45841'}
events={};requests={};done=0;now=0;seed=string.rep('b',32)
RegisterEvent=function(n,f) events[n]=f end
AddUITriggeredEvent=function(s,c,v) requests[#requests+1]={s,c,v} end
GetCurRealTime=function() return now end
DebugError=function() end
function finish() done=done+1 end
function reject(f) assert(not pcall(f)) end
""")
        l.execute(SOURCE.read_text());return l

    def test_capture_waits_for_native_registry_then_dump_before_saving(self):
        self.make().execute("""
D=X4LAN_SEED_DOCKS;D.start('capture',seed,finish)
assert(requests[1][2]=='Mark' and done==0)
events['X4LAN_DockRegistry.Ready'](nil,seed..'|2')
assert(requests[2][2]=='Dump' and requests[2][3].mode=='capture' and done==0)
events['X4LAN_DockRegistry.Done'](nil,seed..'|2|1');assert(done==1)
reject(function() events['X4LAN_DockRegistry.Done'](nil,seed..'|2|1') end)
assert(done==1)
""")

    def test_reload_never_rebuilds_registry_and_bad_ack_or_timeout_never_finishes(self):
        self.make().execute("""
D=X4LAN_SEED_DOCKS;D.start('load',seed,finish)
assert(#requests==1 and requests[1][2]=='Dump')
reject(function() events['X4LAN_DockRegistry.Done'](nil,string.rep('c',32)..'|2|1') end)
reject(function() events['X4LAN_DockRegistry.Done'](nil,seed..'|0|1') end)
reject(function() events['X4LAN_DockRegistry.Done'](nil,seed..'|2|0') end)
reject(function() D.frame(61) end);assert(done==0 and D.stage=='failed')
""")

    def test_matching_native_capture_reload(self):
        self.assertEqual(validate(trace('capture'),trace('load'),SEED)['saved_docks'],2)

    def test_missing_unresolved_changed_or_reordered_native_evidence_rejected(self):
        capture=trace('capture');loaded=trace('load');lines=loaded.splitlines()
        bad=[loaded.replace('SEED_DOCKS begin','unavailable'),loaded.replace('object=0x11','object=unavailable'),
             loaded.replace('object=0x12','object=0x11'),loaded.replace('index=2','index=1'),
             loaded.replace('container=0x10','container=0x20'),loaded.replace('macro=dock_b_macro','macro=dock_c_macro'),
             loaded.replace('reverse=1','reverse=0'),loaded.replace('capacity=40','capacity=41'),
             loaded.replace('SEED_PROBE loaded','unavailable'),loaded+'\nSEED_DOCKS row unavailable',
             '\n'.join([lines[0],lines[2],lines[1]]+lines[3:]),
             loaded+'\nError in MD cue md.X4LAN_DockRegistry.Dump',
             loaded+'\nextensions/x4_lan_lab/md/dock_registry_probe.xml(5): bad attribute']
        for value in bad:
            with self.subTest(value=value[-80:]),self.assertRaises(ValueError):validate(capture,value,SEED)
