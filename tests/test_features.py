"""Feature visibility (dev rule): FEATURES summary lines and FEATURE <name> DISABLED reasons."""
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from x4lan.features import Features, config_features, relay_features, INTERNAL_OFF_REASONS
from x4lan.economy import EconomySession
from x4lan.monitor import Monitor, render

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'
FEATURES = (UI / 'features.lua').read_text()
TOKEN = 'a' * 32
SUMMARY = re.compile(r'^\[X4LAN\] FEATURES on=(\S+) off=(\S+) auto_disabled=(\S+)$')


def runtime(setup=''):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute('logs={};now=0;DebugError=function(v) logs[#logs+1]=v end;GetCurRealTime=function() return now end')
    lua.execute(setup)
    lua.execute(FEATURES)
    lua.execute('''F=X4LAN_FEATURES
      function count(pattern) local n=0;for _,v in ipairs(logs) do if v:find(pattern,1,true) then n=n+1 end end;return n end
      function summaries() local t={};for _,v in ipairs(logs) do if v:find('[X4LAN] FEATURES ',1,true) then t[#t+1]=v end end;return t end''')
    return lua


def lua_list(lua, name):
    table = lua.eval(name)
    return [table[i] for i in range(1, len(table) + 1)]


@unittest.skipUnless(LuaRuntime, 'lupa.luajit21 not installed')
class FeatureHelperTests(unittest.TestCase):
    def test_disable_logs_once_per_transition_and_resume_rearms(self):
        l = runtime()
        l.execute('''
          F.declare('npc_replicas',true)
          assert(F.disable('npc_replicas','baseline_stale age=15.0'))
          assert(not F.disable('npc_replicas','baseline_stale age=16.0'))
          assert(not F.disable('npc_replicas','other'))
          assert(not F.declare('npc_replicas',true))  -- a refusal is not hidden by a re-declaration
          assert(F.state('npc_replicas')=='auto_disabled')
          assert(F.resume('npc_replicas') and not F.resume('npc_replicas'))
          F.disable('npc_replicas','baseline_stale age=20.0')''')
        self.assertEqual([v for v in lua_list(l, 'logs') if 'FEATURE ' in v], [
            '[X4LAN] FEATURE npc_replicas DISABLED reason=baseline_stale age=15.0',
            '[X4LAN] FEATURE npc_replicas RESUMED',
            '[X4LAN] FEATURE npc_replicas DISABLED reason=baseline_stale age=20.0'])

    def test_summary_format_is_sorted_and_machine_readable(self):
        l = runtime('X4LAN_CONFIG={enabled=true}')
        l.execute('''
          F.declare('trading',true);F.declare('walking_avatars',false)
          F.declare('shot_undo',false,'diagnostic; delays shield recharge')
          F.gate('npc_combat',true,{},'world_sync adapter',nil,'shot_capture adapter')
          F.disable('cargo','x4lan.lua:12: attempt to index (a nil value), retry')''')
        self.assertEqual(l.eval('F.summary()'),
                         'FEATURES on=enabled,trading '
                         'off=shot_undo(diagnostic;_delays_shield_recharge),walking_avatars(config) '
                         'auto_disabled=cargo(x4lan.lua:12:_attempt_to_index_a_nil_value_retry),'
                         'npc_combat(missing_prerequisite_shot_capture_adapter)')
        self.assertEqual(l.eval('count("FEATURE npc_combat DISABLED reason=missing prerequisite shot_capture adapter")'), 1)
        self.assertEqual(l.eval('summaries()[1]'), None)  # nothing logged before the first frame

    def test_first_frame_logs_summary_and_changes_are_rate_limited(self):
        l = runtime('X4LAN_CONFIG={enabled=true}')
        l.execute('''
          F.declare('coop',true)
          assert(F.tick(5) and not F.tick(5.1))           -- first frame: one summary
          now=6;F.disable('coop','adapter stopped')
          assert(not F.tick(6) and not F.tick(14.9))      -- changed, but within 10 s
          F.resume('coop');F.disable('coop','again')      -- coalesced into one later summary
          assert(F.tick(15) and not F.tick(30))''')
        lines = lua_list(l, 'summaries()')
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], '[X4LAN] FEATURES on=coop,enabled off=- auto_disabled=-')
        self.assertEqual(lines[1], '[X4LAN] FEATURES on=enabled off=- auto_disabled=coop(again)')

    def test_registers_its_own_frame_and_sweeps_undeclared_config_flags(self):
        l = runtime('''frames={};SetScript=function(name,fn) frames[name]=fn end
          X4LAN_CONFIG={enabled=true,coop=true,shot_undo=false,npc_motion_diagnostics=1,port=45841,slot='p2',seed_verified=false}''')
        l.execute('F.declare("coop",true);now=3;frames.onUpdate()')
        self.assertEqual(lua_list(l, 'summaries()'), [
            '[X4LAN] FEATURES on=coop,enabled off=shot_undo(config) '
            'auto_disabled=npc_motion_diagnostics(not_declared_by_any_loaded_adapter)'])
        self.assertEqual(l.eval('count("FEATURE npc_motion_diagnostics DISABLED reason=not declared by any loaded adapter")'), 1)

    def test_load_order_features_after_config_before_adapters(self):
        files = re.findall(r'<file name="ui/([^"]+)"', (ROOT / 'extension/x4_lan_lab/ui.xml').read_text())
        self.assertEqual(files[:2], ['config.lua', 'features.lua'])
        self.assertEqual(files[-1], 'frame_profile_install.lua')
        self.assertLess(files.index('features.lua'), files.index('frame_profile.lua'))
        self.assertEqual(sorted(files), sorted(p.name for p in UI.glob('*.lua')))


def adapter(name, setup):
    lua = runtime(setup)
    lua.execute((UI / name).read_text())
    return lua


@unittest.skipUnless(LuaRuntime, 'lupa.luajit21 not installed')
class AdapterReasonTests(unittest.TestCase):
    def test_inert_source_config_reports_every_flag_off_by_config(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute('logs={};DebugError=function(v) logs[#logs+1]=v end')
        lua.execute((UI / 'config.lua').read_text())
        lua.execute(FEATURES)
        for name in ('game.lua', 'frame_profile.lua', 'net_rtt.lua', 'bubble_game.lua', 'coop_game.lua',
                     'health_game.lua', 'shot_game.lua', 'shot_visual.lua', 'shot_capture.lua',
                     'projectile_game.lua', 'npc_combat_game.lua', 'npc_target_diag.lua', 'trade_game.lua',
                     'mission_game.lua', 'walk_game.lua'):
            lua.execute((UI / name).read_text())
        lua.execute('X4LAN_FEATURES.tick(0)')
        line = [v for v in lua_list(lua, 'logs') if 'FEATURES' in v][0]
        on, off, auto = SUMMARY.match(line).groups()
        self.assertEqual((on, auto), ('-', '-'))
        flags = re.findall(r'^\s+(\w+) = (?:true|false),', (UI / 'config.lua').read_text(), re.M)
        for flag in flags:
            self.assertIn(flag + '(', off)
        self.assertNotIn('DISABLED', '\n'.join(lua_list(lua, 'logs')))

    def test_projectile_visuals_names_the_missing_adapter(self):
        l = adapter('projectile_game.lua', '''X4LAN_CONFIG={projectile_visuals=true,shot_visual=true,shot_capture=true,
          shot_claims=true,pilot_health=true,fire2=true};X4LAN_BUBBLE_API={coop={},health={},shot={}}''')
        self.assertEqual(l.eval('count("FEATURE projectile_visuals DISABLED reason=missing prerequisite shot_visual adapter")'), 1)
        self.assertEqual(l.eval('X4LAN_BUBBLE_API.projectiles'), None)  # behaviour unchanged: still inert

    def test_shot_visual_needs_fire2_from_config(self):
        l = adapter('shot_visual.lua', 'X4LAN_CONFIG={shot_visual=true,shot_claims=true,pilot_health=true}')
        self.assertEqual(l.eval('count("FEATURE shot_visual DISABLED reason=missing prerequisite fire2")'), 1)

    def test_frame_profile_without_measure_clock(self):
        l = adapter('frame_profile.lua', 'X4LAN_CONFIG={enabled=true,frame_profile=true}')
        self.assertEqual(l.eval('count("FEATURE frame_profile DISABLED reason=missing prerequisite measure_clock")'), 1)
        self.assertEqual(l.eval('X4LAN_FRAME_PROFILE'), None)

    def test_legacy_adapter_refuses_seed_admission(self):
        l = adapter('game.lua', "X4LAN_CONFIG={enabled=true,seed_id='abc',interaction_test=true}")
        self.assertEqual(l.eval('count("FEATURE legacy_adapter DISABLED reason=seed admission requires bubble_actions")'), 1)
        self.assertEqual(l.eval('count("ADAPTER STOPPED: seed admission requires cooperative bubble mode")'), 1)
        self.assertEqual(l.eval('F.state("interaction_test")'), ('off', 'legacy adapter inactive'))

    def test_walking_avatars_off_and_bubble_actions_superseding_legacy(self):
        l = adapter('walk_game.lua', 'X4LAN_CONFIG={enabled=true,trading=true,walking_avatars=false}')
        l.execute((UI / 'game.lua').read_text().replace('X4LAN_CONFIG', 'X4LAN_CONFIG2'))  # cfg nil: master off
        self.assertEqual(l.eval('F.state("walking_avatars")'), ('off', 'config'))
        self.assertEqual(l.eval('select(2,F.state("legacy_adapter"))'), 'config')

    def universe(self):
        import test_universe_adapter as ua
        return ua.UniverseAdapterTests().make('p2', setup=FEATURES + "\nX4LAN_CONFIG.npc_motion_diagnostics='often'")

    def test_universe_stale_baseline_disables_and_resumes_npc_replicas_once(self):
        l = self.universe()
        l.execute('''
          F=X4LAN_FEATURES
          function count(p) local n=0;for _,v in ipairs(logs) do if v:find(p,1,true) then n=n+1 end end;return n end
          tick(1);baseline(1,{testRow},true);tick(320)
          assert(count('FEATURE npc_replicas DISABLED reason=baseline_stale')==1)
          assert(count('FEATURE npc_motion_diagnostics DISABLED reason=invalid_value')==1)
          baseline(2,{testRow},true);tick(2)
          assert(count('FEATURE npc_replicas RESUMED')==1 and F.state('npc_replicas')=='on')''')
        lines = [v for v in lua_list(l, 'logs') if v.startswith('[X4LAN] FEATURES ')]
        self.assertGreaterEqual(len(lines), 2)
        on, off, auto = SUMMARY.match(lines[0]).groups()
        for name in ('bubble_actions', 'coop', 'world_sync', 'npc_replicas'):
            self.assertIn(name, on.split(','))
        self.assertIn('npc_motion_diagnostics(invalid_value)', auto)
        self.assertTrue(any('npc_replicas(baseline_stale' in v for v in lines[1:]))

    def test_npc_combat_failure_path_records_the_reason_once(self):
        import test_universe_adapter as ua
        import test_npc_combat as tn
        original = ua.UniverseAdapterTests.make
        with patch.object(ua.UniverseAdapterTests, 'make', lambda self, slot='p1', mode='replica', setup='':
                          original(self, slot, mode, FEATURES)):
            l = tn.NpcAdapterTests().make()
        l.execute('''
          F=X4LAN_FEATURES;assert(F.state('npc_combat')=='on')
          N.fail('binding limit; reconnect required');N.fail('second')
          local n=0;for _,v in ipairs(logs) do if v:find('FEATURE npc_combat DISABLED',1,true) then n=n+1;last=v end end
          assert(n==1,n)''')
        self.assertEqual(l.eval('last'), '[X4LAN] FEATURE npc_combat DISABLED reason=binding limit; reconnect required')
        self.assertEqual(l.eval('(F.state("npc_combat"))'), 'auto_disabled')


class PythonFeatureTests(unittest.TestCase):
    def test_features_class_logs_once_and_formats_summary(self):
        logged = []
        f = Features(logged.append)
        f.declare('netprobe', True)
        f.declare('cargo', False, 'requires world_sync')
        self.assertTrue(f.disable('npc_combat', 'binding limit')); self.assertFalse(f.disable('npc_combat', 'x'))
        self.assertEqual(logged, ['FEATURE npc_combat DISABLED reason=binding limit'])
        self.assertEqual(f.summary(), 'FEATURES on=netprobe off=cargo(requires_world_sync) '
                                      'auto_disabled=npc_combat(binding_limit)')
        self.assertTrue(f.resume('npc_combat')); self.assertEqual(logged[-1], 'FEATURE npc_combat RESUMED')

    def test_relay_features_follow_the_session_type(self):
        from x4lan.universe import UniverseSession
        world = relay_features(UniverseSession(TOKEN, 'world1-test', 'bubble'), {'coop': True, 'world_sync': True})
        self.assertEqual({v['state'] for v in world.as_dict().values()}, {'on'})
        for name in ('netprobe', 'coop', 'world_sync', 'npc_combat', 'cargo', 'missions', 'coop_docks'):
            self.assertIn(name, world.as_dict())
        economy = relay_features(EconomySession(TOKEN, 'build', 'bubble'), {})
        self.assertEqual(economy.as_dict()['coop'], {'state': 'off', 'reason': 'config coop=false'})
        self.assertEqual(economy.as_dict()['cargo'], {'state': 'off', 'reason': 'requires world_sync'})
        self.assertRegex(economy.summary(), r'^FEATURES on=\S+ off=\S+ auto_disabled=-$')

    def monitor(self, session=None):
        session = session or EconomySession(TOKEN, 'build', 'bubble')
        relay = SimpleNamespace(session=session, socket=SimpleNamespace(getsockname=lambda: ('127.0.0.1', 45841)),
                                received_packets=0, sent_packets=0, send_errors=0)
        return Monitor(relay, 0)

    def test_status_json_and_dashboard_show_disabled_features_with_reasons(self):
        m = self.monitor()
        m.features.disable('netprobe', 'socket error')
        snap = json.loads(json.dumps(m.snapshot(1)))
        self.assertEqual(snap['features']['netprobe'], {'state': 'auto_disabled', 'reason': 'socket error'})
        self.assertEqual(snap['features']['world_sync']['state'], 'off')
        self.assertTrue(any(e['message'].startswith('FEATURES ') for e in snap['events']))
        text = render(snap, 120)
        self.assertIn('AUTO-DISABLED netprobe: socket error', text)
        self.assertIn('off           world_sync: session type EconomySession', text)

    def test_server_logs_one_features_line_at_startup(self):
        from x4lan import monitor
        config = {'port': 0, 'token': TOKEN, 'build': 'world1-test', 'bubble': 'bubble', 'coop': True, 'world_sync': True}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out), \
                patch.object(monitor, 'Screen', lambda *a, **k: SimpleNamespace(show=lambda s: None, close=lambda: None)), \
                patch.object(monitor, 'addresses', lambda: []), \
                patch.object(monitor.time, 'sleep', side_effect=KeyboardInterrupt):
            monitor.run(config, Path(d))
            first = json.loads(next(Path(d).glob('server-*.jsonl')).read_text().splitlines()[0])
            status = json.loads((Path(d) / 'server-status.json').read_text())
        lines = [l for l in out.getvalue().splitlines() if l.startswith('FEATURES ')]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^FEATURES on=\S*netprobe\S* off=- auto_disabled=-$')
        self.assertEqual(first, {'log': lines})
        self.assertEqual(status['features']['npc_combat'], {'state': 'on', 'reason': None})

    def test_internal_config_audit_every_false_flag_has_a_reason(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('session_launcher_features', ROOT / 'distribution/session_launcher.py')
        launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
        text = launcher.session_config({'token': TOKEN, 'bubble': 'b' * 32}, 'p1', '127.0.0.1')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            launcher.print_config_features(text)
        printed = out.getvalue()
        self.assertIn('Generated internal config: FEATURES on=', printed)
        self.assertNotIn('WARNING', printed)
        features = config_features({m[1]: json.loads(m[2]) for m in re.finditer(r'^    (\w+) = (.*),$', text, re.M)})
        off = {k for k, v in features.as_dict().items() if v['state'] == 'off'}
        self.assertEqual(off, set(INTERNAL_OFF_REASONS))
        self.assertNotIn('seed_verified', features.as_dict())


if __name__ == '__main__':
    unittest.main()
