"""Puppet client: role gating, load-time parsing, MD schema and the vanilla cue audit.

The schema and audit tests need extracted vanilla scripts (read-only reference, never committed):
set X4LAN_VANILLA_SCRIPTS to the folder holding base/ and ego_dlc_*/ (each with md/ and
libraries/), or keep x4-re/gamescripts/resolved next to this checkout. They skip loudly otherwise.
"""
import os
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:  # pragma: no cover
    LuaRuntime = None
try:
    from lxml import etree
except ImportError:  # pragma: no cover
    etree = None

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / 'extension/x4_lan_lab'
UI = EXT / 'ui'
PUPPET_MD = EXT / 'md/x4lan_puppet.xml'


def vanilla_root():
    candidates = [os.environ.get('X4LAN_VANILLA_SCRIPTS'),
                  ROOT.parent / 'x4-re/gamescripts/resolved',
                  ROOT.parent.parent / 'x4-re/gamescripts/resolved']
    for c in candidates:
        if c and (Path(c) / 'base/md').is_dir() and (Path(c) / 'base/libraries/md.xsd').is_file():
            return Path(c)
    return None


VANILLA = vanilla_root()


def suppressed_cues():
    """(script, cue) pairs that x4lan_puppet.xml cancels, read from the shipped MD."""
    text = PUPPET_MD.read_text(encoding='utf-8')
    block = re.search(r'<set_value name="\$cues" exact="\[(.*?)\]"/>', text, re.S)
    assert block, 'cancel list missing'
    return re.findall(r'md\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)', block.group(1))


def vanilla_md_cues(root):
    """{mdscript name: set of cue/library names} over base and DLC md/ (full scripts and diffs)."""
    scripts = {}
    for path in sorted(root.glob('*/md/*.xml')):
        if not (path.parent.parent.name == 'base' or path.parent.parent.name.startswith('ego_dlc_')):
            continue
        tree = ET.parse(path).getroot()
        if tree.tag != 'mdscript':
            continue
        names = scripts.setdefault(tree.get('name'), set())
        for node in tree.iter():
            if node.tag in ('cue', 'library') and node.get('name'):
                names.add(node.get('name'))
    return scripts


MOCK = '''
logs, events, sent, frames = {}, {}, {}, {}
now, valid, saveFolder = 0, true, "/home/u/.config/EgoSoft/X4/45841/save/"
factions = {"argon","player","teladi"}
fleet = {argon=10, player=3, teladi=5}
api = {
  IsPlayerValid=function() return valid end,
  GetSaveFolderPath=function() return saveFolder end,
  GetAllFactions=function(out,cap,hidden) for i,f in ipairs(factions) do out[i-1]=f end return #factions end,
  GetNumAllFactionShips=function(f) return fleet[f] or 0 end,
}
local realRequire=require
require=function(name)
  if name=="ffi" then return {C=api, cdef=function() end, string=function(s) return s end,
    new=function() return {} end} end
  return realRequire(name)
end
DebugError=function(v) logs[#logs+1]=v end
GetCurRealTime=function() return now end
RegisterEvent=function(name,fn) events[name]=fn end
AddUITriggeredEvent=function(screen,control) sent[#sent+1]=screen.."."..control end
SetScript=function(name,fn) frames[#frames+1]=fn end
function tick(dt) now=now+dt for _,f in ipairs(frames) do f() end end
function count(name) local n=0 for _,v in ipairs(sent) do if v==name then n=n+1 end end return n end
function has(pattern) for _,v in ipairs(logs) do if v:find(pattern) then return v end end return nil end
'''


@unittest.skipUnless(LuaRuntime, 'lupa.luajit21 not installed')
class PuppetRoleTests(unittest.TestCase):
    def run_lua(self, config, setup=''):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK)
        lua.execute('X4LAN_CONFIG=' + config)
        lua.execute(setup)
        lua.execute((UI / 'puppet_game.lua').read_text(encoding='utf-8'))
        return lua

    def logs(self, lua):
        return list(lua.globals().logs.values())

    def test_decide_never_makes_the_host_a_puppet(self):
        lua = self.run_lua('{enabled=true,slot="p2",puppet_client=false}')
        decide = lua.eval('X4LAN_PUPPET.decide')
        cases = [('{slot="p1",puppet_client=true}', (False, 'host_role')),
                 ('{slot="p1",puppet_client=false}', (False, 'host_role')),
                 ('{slot="p1"}', (False, 'host_role')),
                 ('{slot="p2",puppet_client=true}', (True, None)),
                 ('{slot="p2",puppet_client=false}', (False, 'config')),
                 ('{slot="p2",puppet_client=1}', (False, 'invalid_value')),
                 ('{slot="p3",puppet_client=true}', (False, 'unknown_role')),
                 ('{puppet_client=true}', (False, 'unknown_role'))]
        for config, expected in cases:
            self.assertEqual(decide(lua.eval(config)), expected, config)

    def test_host_with_flag_set_never_activates(self):
        lua = self.run_lua('{enabled=true,slot="p1",puppet_client=true}')
        for _ in range(200):
            lua.execute('tick(0.1)')
        self.assertEqual(lua.eval('count("X4LAN_Puppet.Activate")'), 0)
        logs = self.logs(lua)
        self.assertIn('[X4LAN] FEATURE puppet_client DISABLED reason=host_role role=host', logs)
        self.assertTrue(any('ERROR puppet_client=true refused on the host' in v for v in logs))
        self.assertFalse(lua.eval('X4LAN_PUPPET.active'))

    def test_guest_activates_until_acknowledged_then_census(self):
        lua = self.run_lua('{enabled=true,slot="p2",puppet_client=true}')
        self.assertTrue(lua.eval('has("PUPPET ARMED role=guest")'))
        lua.execute('valid=false;tick(1);tick(1)')
        self.assertEqual(lua.eval('count("X4LAN_Puppet.Activate")'), 0)  # waits for a valid player
        lua.execute('valid=true;tick(0.1);tick(1.0);tick(1.0)')
        self.assertEqual(lua.eval('count("X4LAN_Puppet.Activate")'), 3)
        lua.execute('events["X4LAN_Puppet.Active"](nil,"1|812|15");tick(1);tick(1)')
        self.assertEqual(lua.eval('count("X4LAN_Puppet.Activate")'), 3)
        self.assertTrue(lua.eval('has("PUPPET ACTIVE activation=1 jobs_off_total=812 cues_cancelled_total=15")'))
        lua.execute('events["X4LAN_Puppet.Status"](nil,"1|2|2|812|15");tick(5)')
        row = lua.eval('has("PUPPET v=1")')
        self.assertIn('role=guest puppet=1 npc_ships=15 replicas=0 local_npc_ships=15 factions=2', row)
        self.assertIn('baseline=15 delta=+0 md_active=1 job_spawns=2 removed=2 jobs_off=812 cues_cancelled=15', row)
        lua.execute('fleet.argon=13;tick(30)')
        self.assertTrue(lua.eval('has("local_npc_ships=18 factions=2 baseline=15 delta=%+3")'))

    def test_replicas_are_not_counted_as_local_ships(self):
        lua = self.run_lua('{enabled=true,slot="p2",puppet_client=true}',
                           'X4LAN_BUBBLE_API={universe={replicas={a={},b={},c={}}}}')
        lua.execute('tick(0.1);tick(6)')
        self.assertTrue(lua.eval('has("npc_ships=15 replicas=3 local_npc_ships=12")'))

    def test_source_config_logs_nothing_and_disabled_guest_is_loud(self):
        lua = self.run_lua('{enabled=false,slot="p2",puppet_client=true}')
        self.assertEqual(self.logs(lua), [])
        self.assertIsNone(lua.eval('X4LAN_PUPPET'))
        lua = self.run_lua('{enabled=true,slot="p2",puppet_client=false}')
        self.assertIn('[X4LAN] FEATURE puppet_client DISABLED reason=config role=guest', self.logs(lua))
        lua.execute('tick(0.1);tick(10)')
        self.assertEqual(lua.eval('count("X4LAN_Puppet.Activate")'), 0)

    def test_host_warns_when_a_loaded_save_carries_puppet_state(self):
        lua = self.run_lua('{enabled=true,slot="p1",puppet_client=false}')
        lua.execute('tick(0.1);events["X4LAN_Puppet.Status"](nil,"1|0|0|10|3")')
        self.assertTrue(lua.eval('has("ERROR FEATURE puppet_client DISABLED reason=puppet_state_in_loaded_save role=host")'))

    def test_md_refusal_stops_activation(self):
        lua = self.run_lua('{enabled=true,slot="p2",puppet_client=true}')
        lua.execute('tick(0.1);events["X4LAN_Puppet.Refused"](nil,"host_role_md");tick(2);tick(2)')
        self.assertEqual(lua.eval('count("X4LAN_Puppet.Activate")'), 1)
        self.assertTrue(lua.eval('has("FEATURE puppet_client DISABLED reason=host_role_md")'))

    def test_features_helper_receives_declaration_when_present(self):
        lua = self.run_lua('{enabled=true,slot="p1",puppet_client=false}',
                           'declared={};X4LAN_FEATURES={declare=function(n,on,r) declared[n]={on,r} end}')
        self.assertEqual(tuple(lua.eval('declared.puppet_client').values()), (False, 'host_role'))

    def test_progressbar_parsing_and_loadtime_row(self):
        lua = self.run_lua('{enabled=true,slot="p2",puppet_client=false}')
        parse = lua.eval('X4LAN_PUPPET.parseProgress')
        samples = [
            '<?xml version="1.0"?><progressbar><step id="init" time="8.3"/><step id="map" time="4.9"/>'
            '<!-- <step id="x" time="1"/> --><step id="jobs" time="11.2"/></progressbar>',
            "<progress><stage name='init' duration='8.3'/><stage name='map' duration='4.9'/>"
            "<stage name='jobs' duration='11.2'/></progress>",
            '<progressbar><init time="8.3"/><map time="4.9"/><jobs time="11.2"/></progressbar>',
            '<progressbar><step name="init">8.3</step><step name="map">4.9</step><step name="jobs">11.2</step></progressbar>',
        ]
        for text in samples:
            stages = parse(text)
            got = [(s['name'], round(s['value'], 3)) for s in stages.values()]
            self.assertEqual(got, [('init', 8.3), ('map', 4.9), ('jobs', 11.2)], text)
        self.assertEqual(parse(''), (None, 'empty'))
        self.assertEqual(parse('<progressbar version="2"><x/></progressbar>'), (None, 'no_stages'))
        row = lua.eval('X4LAN_PUPPET.loadtimeRow("guest",true,X4LAN_PUPPET.parseProgress(...),120)', samples[0])
        self.assertEqual(row, 'LOADTIME v=1 role=guest puppet=1 source=progressbar.xml bytes=120 key=time '
                              'stages=init:8.3,map:4.9,jobs:11.2 sum=24.4')

    def test_loadtime_reads_profile_progressbar_on_first_valid_frame(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / '45841'
            (profile / 'save').mkdir(parents=True)
            (profile / 'progressbar.xml').write_text('<progressbar><step id="jobs" time="11.2"/></progressbar>')
            lua = self.run_lua('{enabled=true,slot="p1",puppet_client=false}',
                               'saveFolder=[[%s]]' % (str(profile / 'save') + os.sep))
            lua.execute('tick(0.1)')
            self.assertTrue(lua.eval('has("LOADTIME v=1 role=host puppet=0 .* stages=jobs:11.2 sum=11.2")'))
            (profile / 'progressbar.xml').write_text('<progressbar><step id="jobs" time="3.5"/></progressbar>')
            lua.execute('tick(121)')
            self.assertTrue(lua.eval('has("stages=jobs:3.5 sum=3.5 reread=1")'))
        lua = self.run_lua('{enabled=true,slot="p2",puppet_client=true}', 'saveFolder="/nowhere/45841/save"')
        lua.execute('tick(0.1)')
        self.assertTrue(lua.eval('has("LOADTIME unavailable reason=file_not_found")'))


class PuppetPackagingTests(unittest.TestCase):
    def test_source_config_inert_and_internal_packages_gate_by_role(self):
        self.assertIn('puppet_client = false', (UI / 'config.lua').read_text(encoding='utf-8'))
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location('x4lan_session_launcher_puppet', ROOT / 'distribution/session_launcher.py')
        sys.path.insert(0, str(ROOT / 'distribution'))
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(ROOT / 'distribution'))
        session = {'token': 'a' * 32, 'bubble': 'bubble-01'}
        host = module.session_config(session, 'p1', '127.0.0.1')
        guest = module.session_config(session, 'p2', '10.0.0.1')
        self.assertIn('    puppet_client = false,\n', host)
        self.assertIn('    puppet_client = true,\n', guest)

    def test_ui_and_md_registration(self):
        ui = (EXT / 'ui.xml').read_text(encoding='utf-8')
        self.assertIn('<file name="ui/puppet_game.lua" />', ui)
        self.assertLess(ui.index('ui/universe_game.lua'), ui.index('ui/puppet_game.lua'))
        self.assertLess(ui.index('ui/puppet_game.lua'), ui.index('ui/frame_profile_install.lua'))
        root = ET.parse(PUPPET_MD).getroot()
        self.assertEqual(root.get('name'), 'X4LAN_Puppet')

    def test_no_library_diffs_for_puppet_mode(self):
        # Puppet mode is a runtime switch; libraries apply to the host too and must stay untouched.
        self.assertFalse((EXT / 'libraries/jobs.xml').exists())
        self.assertFalse((EXT / 'libraries/parameters.xml').exists())
        self.assertFalse((EXT / 'libraries/god.xml').exists())
        for path in sorted((EXT / 'libraries').glob('*.xml')) + sorted((EXT / 'md').glob('*.xml')):
            ET.parse(path)  # well-formed

    def test_activation_is_gated_by_the_ui_event_only(self):
        root = ET.parse(PUPPET_MD).getroot()
        cues = {c.get('name'): c for c in root.iter('cue')}
        self.assertIsNone(cues['State'].find('conditions'))
        state_actions = ET.tostring(cues['State'].find('actions'), encoding='unicode')
        self.assertNotIn('Suppress', state_actions)
        self.assertNotIn('cancel_cue', state_actions)
        activate = cues['Activate'].find('conditions/event_ui_triggered')
        self.assertEqual((activate.get('screen'), activate.get('control')), ("'X4LAN_Puppet'", "'Activate'"))
        guard = cues['JobSpawnGuard'].find('conditions/check_value')
        self.assertEqual(guard.get('value'), 'State.$active')
        text = PUPPET_MD.read_text(encoding='utf-8')
        self.assertEqual(text.count('<include_actions ref="Suppress"/>'), 1)
        self.assertIn('@md.X4LAN_Bubble.Arena.$host', text)


@unittest.skipUnless(VANILLA, 'vanilla scripts not found: set X4LAN_VANILLA_SCRIPTS (schema and cue audit SKIPPED)')
class PuppetVanillaAuditTests(unittest.TestCase):
    @unittest.skipUnless(etree, 'lxml not installed (md.xsd validation SKIPPED)')
    def test_puppet_md_validates_against_game_md_xsd(self):
        schema = etree.XMLSchema(etree.parse(str(VANILLA / 'base/libraries/md.xsd')))
        document = etree.parse(str(PUPPET_MD))
        self.assertTrue(schema.validate(document), str(schema.error_log))

    def test_every_cancelled_vanilla_cue_exists(self):
        scripts = vanilla_md_cues(VANILLA)
        cues = suppressed_cues()
        self.assertEqual(len(cues), 15)
        missing = [f'md.{s}.{c}' for s, c in cues if c not in scripts.get(s, set())]
        self.assertEqual(missing, [], 'vanilla renamed or removed a cue the puppet client cancels')

    def test_vanilla_switches_used_by_puppet_mode_still_exist(self):
        md = '\n'.join(p.read_text(encoding='utf-8', errors='replace') for p in sorted((VANILLA / 'base/md').glob('*.xml')))
        self.assertIn('md.GenericMissions.Manager.$Disabled', md)  # vanilla's own off switch
        self.assertIn('<event_job_ship_activated', md)
        self.assertIn('<get_suitable_job', md)
        xsd = (VANILLA / 'base/libraries/common.xsd').read_text(encoding='utf-8') + \
            (VANILLA / 'base/libraries/md.xsd').read_text(encoding='utf-8')
        for action in ('set_job_active', 'get_suitable_job', 'set_space_god_allowed',
                       'set_space_faction_logic_allowed', 'event_job_ship_activated', 'destroy_object'):
            self.assertIn(f'<xs:element name="{action}"', xsd, action)
        # Every cancelled script is a base-game script (no DLC dependency in the cancel list).
        base = {ET.parse(p).getroot().get('name') for p in (VANILLA / 'base/md').glob('*.xml')}
        self.assertEqual(sorted({s for s, _ in suppressed_cues()} - base), [])


if __name__ == '__main__':
    unittest.main()
