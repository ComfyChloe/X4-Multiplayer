"""Offline checks of the combat load scenario fixture (dev/combat_scenario); never launches X4.

The MD syntax itself is checked against the game's md.xsd by dev/combat_scenario/validate.py
(needs the extracted schema outside this repository). These tests cover the structure the
schema cannot see: cross references, variant parameters, the installer's path guard and the
marker/profile parser.
"""
import collections
import contextlib
import importlib.util
import io
from pathlib import Path
import re
import tempfile
import unittest
import xml.etree.ElementTree as E

ROOT = Path(__file__).resolve().parents[1]
SCN = ROOT / 'dev' / 'combat_scenario'
MAIN = SCN / 'extension' / 'md' / 'x4mp_combat_scenario.xml'


def load(name):
    spec = importlib.util.spec_from_file_location('combat_' + name, SCN / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load('install')
markers = load('markers')


def library_values(path: Path, library: str) -> dict:
    tree = E.parse(path)
    node = next(e for e in tree.iter('library') if e.get('name') == library)
    return {s.get('name'): s.get('exact') for s in node.iter('set_value')}


def list_items(expr: str) -> list[str]:
    assert expr.startswith('[') and expr.endswith(']'), expr
    return [item.strip() for item in expr[1:-1].split(',') if item.strip()]


class ScenarioStructure(unittest.TestCase):
    def setUp(self):
        self.tree = E.parse(MAIN)
        self.libraries = {e.get('name') for e in self.tree.iter('library')}
        self.cues = [e.get('name') for e in self.tree.iter('cue')]

    def test_script_identity_and_unique_cues(self):
        root = self.tree.getroot()
        self.assertEqual(root.tag, 'mdscript')
        self.assertEqual(root.get('name'), 'X4MP_CombatScenario')
        self.assertEqual(len(self.cues), len(set(self.cues)))
        self.assertEqual(len(self.libraries & set(self.cues)), 0)

    def test_include_actions_resolve(self):
        for node in self.tree.iter('include_actions'):
            ref = node.get('ref')
            if ref.startswith('md.'):
                self.assertEqual(ref, 'md.X4MP_CombatVariant.Config')
            else:
                self.assertIn(ref, self.libraries)

    def test_cue_references_resolve(self):
        for tag in ('cancel_cue', 'signal_cue', 'reset_cue'):
            for node in self.tree.iter(tag):
                self.assertIn(node.get('cue'), self.cues + ['this'])

    def test_gated_on_fresh_creative_world(self):
        start = next(e for e in self.tree.iter('cue') if e.get('name') == 'Start')
        signalled = start.find('conditions/event_cue_signalled')
        self.assertEqual(signalled.get('cue'), 'md.Setup.Start')
        allowed = [s.get('exact') for s in start.iter('set_value') if s.get('name') == '$Allowed']
        self.assertEqual(allowed, ["player.module == 'custom_creative'"])
        stage = [s.get('exact') for s in start.find('actions').iter('set_value') if s.get('name') == '$Stage']
        self.assertEqual(stage, ["if $Allowed then 'loaded' else 'refused'"])
        arm = next(e for e in self.tree.iter('cue') if e.get('name') == 'Arm')
        self.assertIn("$Stage == 'loaded'", arm.find('conditions/check_value').get('value'))

    def test_polled_cues_have_literal_check_intervals(self):
        # Native finding (lane smoke-002): non-event conditions need checkinterval/checktime,
        # and checkinterval must be a literal time.
        non_event = {'check_value', 'check_any', 'check_all', 'check_age'}
        for cue in self.tree.iter('cue'):
            conditions = cue.find('conditions')
            if conditions is None or not any(child.tag in non_event for child in conditions):
                continue
            with self.subTest(cue=cue.get('name')):
                interval = cue.get('checkinterval')
                self.assertTrue(interval or cue.get('checktime'), 'non-event condition without checkinterval')
                if interval:
                    self.assertRegex(interval, r'^\d+(\.\d+)?(ms|s|min|h)$')

    def test_steps_are_ordered_by_stage_not_by_nesting_or_delay(self):
        # Native finding (lane smoke-002): sub-cues are enabled when the parent triggers,
        # before its delay or actions, so a step may not rely on nesting or <delay>.
        self.assertEqual(list(self.tree.iter('delay')), [])
        start = next(e for e in self.tree.iter('cue') if e.get('name') == 'Start')
        steps = start.find('cues')
        self.assertEqual([c.find('cues') for c in steps], [None] * len(steps))
        for cue in steps:
            if cue.get('name') == 'Tick':
                continue
            with self.subTest(cue=cue.get('name')):
                self.assertIn('$Stage ==', cue.find('conditions/check_value').get('value'))

    def test_every_marker_is_known_to_the_parser(self):
        texts = [n.get('text') for n in self.tree.iter('debug_text')]
        kinds = {re.match(r"'COMBATSCN (\w+)", t).group(1) for t in texts}
        self.assertTrue(kinds <= markers.KINDS, kinds - markers.KINDS)
        self.assertEqual(kinds, markers.KINDS)

    def test_defaults_are_consistent(self):
        self.check_config(library_values(MAIN, 'Defaults'), 'battle')


class Variants(unittest.TestCase):
    def test_variants_exist(self):
        self.assertEqual(set(install.variants()), {'battle', 'idle', 'capitals', 'smoke'})

    def test_variant_libraries_override_known_values_only(self):
        defaults = library_values(MAIN, 'Defaults')
        for name, path in install.variants().items():
            with self.subTest(variant=name):
                root = E.parse(path).getroot()
                self.assertEqual(root.get('name'), 'X4MP_CombatVariant')
                values = library_values(path, 'Config')
                self.assertEqual(values['$Variant'], f"'{name}'")
                self.assertTrue(set(values) <= set(defaults), set(values) - set(defaults))
                config = dict(defaults)
                config.update(values)
                ScenarioStructure.check_config(self, config, name)

    def test_idle_variant_is_passive_and_mirrored(self):
        config = dict(library_values(MAIN, 'Defaults'))
        config.update(library_values(install.variants()['idle'], 'Config'))
        self.assertEqual(config['$Hostile'], 'false')
        self.assertEqual(config['$B_Faction'], config['$A_Faction'])
        self.assertEqual(config['$B_Counts'], config['$A_Counts'])


def check_config(case, config, name):
    lengths = {key: len(list_items(config[key])) for key in
               ('$A_Macros', '$A_Counts', '$B_Macros', '$B_Counts', '$Columns', '$Spacing', '$Depth')}
    case.assertEqual(len(set(lengths.values())), 1, f'{name}: {lengths}')
    for key in ('$A_Macros', '$B_Macros'):
        for macro in list_items(config[key]):
            case.assertRegex(macro, r'^macro\.ship_[a-z]{3}_(s|m|l|xl)_[a-z0-9_]+_macro$')
    for key in ('$A_Counts', '$B_Counts', '$Columns'):
        for value in list_items(config[key]):
            case.assertGreater(int(value), 0)
    total = sum(int(v) for v in list_items(config['$A_Counts'])) + sum(int(v) for v in list_items(config['$B_Counts']))
    case.assertLessEqual(total, 120, f'{name}: {total} ships')


ScenarioStructure.check_config = check_config


class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.lanes = base / 'lanes'
        self.extensions = self.lanes / 'b1' / 'game-view' / 'extensions'
        self.extensions.mkdir(parents=True)
        self.outside = base / 'steam' / 'X4 Foundations' / 'extensions'
        self.outside.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_status_uninstall(self):
        folder = install.install(self.extensions, 'idle', self.lanes)
        self.assertEqual(folder, self.extensions.resolve() / 'x4mp_combat_scenario')
        self.assertTrue((folder / 'content.xml').is_file())
        self.assertTrue((folder / 'md' / 'x4mp_combat_scenario.xml').is_file())
        variant = E.parse(folder / 'md' / 'x4mp_combat_variant.xml')
        self.assertIn("'idle'", [s.get('exact') for s in variant.iter('set_value')])
        self.assertIn('variant=idle', install.status(self.extensions, self.lanes))
        install.install(self.extensions, 'battle', self.lanes)
        self.assertIn('variant=battle', install.status(self.extensions, self.lanes))
        self.assertTrue(install.uninstall(self.extensions, self.lanes))
        self.assertEqual(install.status(self.extensions, self.lanes), 'not installed')
        self.assertFalse(install.uninstall(self.extensions, self.lanes))

    def test_refuses_outside_lanes_root(self):
        with self.assertRaises(install.Refused):
            install.install(self.outside, 'battle', self.lanes)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_refuses_non_extensions_folder(self):
        other = self.lanes / 'b1' / 'game-view' / 'md'
        other.mkdir()
        with self.assertRaises(install.Refused):
            install.install(other, 'battle', self.lanes)

    def test_refuses_unknown_variant_and_foreign_folder(self):
        with self.assertRaises(install.Refused):
            install.install(self.extensions, 'nope', self.lanes)
        foreign = self.extensions / 'x4mp_combat_scenario'
        foreign.mkdir()
        (foreign / 'keep.txt').write_text('user data')
        with self.assertRaises(install.Refused):
            install.install(self.extensions, 'battle', self.lanes)
        with self.assertRaises(install.Refused):
            install.uninstall(self.extensions, self.lanes)
        self.assertEqual((foreign / 'keep.txt').read_text(), 'user data')

    def test_stage_writes_a_complete_extension_folder(self):
        out = Path(self.tmp.name) / 'stage'
        folder = install.stage(out, 'smoke')
        self.assertEqual(folder.name, 'x4mp_combat_scenario')
        self.assertEqual(E.parse(folder / 'content.xml').getroot().get('id'), 'x4mp_combat_scenario')
        variant = E.parse(folder / 'md' / 'x4mp_combat_variant.xml')
        self.assertIn("'smoke'", [s.get('exact') for s in variant.iter('set_value')])
        install.stage(out, 'battle')  # restaging over our own folder is allowed
        (folder / 'INSTALL.json').unlink()
        with self.assertRaises(install.Refused):
            install.stage(out, 'battle')

    def test_cli_exit_code_on_refusal(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = install.main(['install', '--extensions', str(self.outside), '--lanes-root', str(self.lanes)])
        self.assertEqual(code, 3)
        self.assertIn('REFUSED', error.getvalue())


LOG = """\
[General] 0.00 ======================================
[Scripts] 0.52 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN loaded version=1 variant=battle module=custom_creative t=0.52
[X4LAN] SIMPROF window seconds=10.00 samples=10000 rateHz=1000 overheadPct=2.0
[X4LAN] SIMPROF fold slot3;ef4400;9fdef0 100
[Scripts] 45.10 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN armed t=45.1 variant=battle sector=cluster_14_sector001_macro zone=tempzone center=0,10000,0
[Scripts] 48.20 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN spawned side=A count=44 requested=44 faction=argon t=48.2
[Scripts] 48.30 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN spawned side=B count=43 requested=43 faction=xenon t=48.3
[Scripts] 50.30 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN engaged t=50.3 hostile=true a=44 b=43
[X4LAN] SIMPROF window seconds=10.00 samples=10000 rateHz=1000 overheadPct=2.0
[X4LAN] SIMPROF fold slot3;ef4400;9fdef0 200
[Scripts] 70.30 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN window begin t=70.3 a=44 b=43
[X4LAN] SIMPROF window seconds=10.00 samples=10000 rateHz=1000 overheadPct=2.0
[X4LAN] SIMPROF fold slot3;ef4400;9fdef0;8132d0 300
[Scripts] 80.30 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN tick t=80.3 a=40 b=39 ahull=3800.5 bhull=3500
[X4LAN] SIMPROF window seconds=10.00 samples=10000 rateHz=1000 overheadPct=2.5
[X4LAN] SIMPROF fold slot3;ef4400;9fdef0;8132d0 400
[X4LAN] SIMPROF fold slot3;ef4400;9fdef0;d4f090 50
[X4LAN] SIMPROF window seconds=10.00 samples=10000 rateHz=1000 overheadPct=2.5
[X4LAN] SIMPROF fold slot3;ef4400;9fdef0;8132d0 500
[Scripts] 95.00 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN window end t=95 a=30 b=9 reason=decided
[Scripts] 95.00 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN done t=95 a=30 b=9
[X4LAN] SIMPROF window seconds=10.00 samples=10000 rateHz=1000 overheadPct=2.5
[X4LAN] SIMPROF fold slot3;ef4400;9fdef0 600
[Scripts] 100.00 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN cleaned t=100
"""


class Markers(unittest.TestCase):
    def test_summary(self):
        result = markers.summary(markers.parse_markers(LOG.splitlines()))
        self.assertTrue(result['complete'])
        self.assertEqual(result['loaded']['variant'], 'battle')
        self.assertEqual(result['spawned']['A']['count'], 44)
        self.assertEqual(result['spawned']['B']['faction'], 'xenon')
        self.assertEqual(result['window_end']['reason'], 'decided')
        self.assertEqual(result['window_seconds'], 24.7)
        self.assertEqual(result['ticks'][0]['ahull'], 3800.5)
        self.assertEqual(result['armed']['center'], '0,10000,0')

    def test_refused_is_not_complete(self):
        log = "[Scripts] 1.0 *** Context:md.X4MP_CombatScenario.Start: COMBATSCN refused reason=module module=x4ep1_gamestart_trade\n"
        result = markers.summary(markers.parse_markers(log.splitlines()))
        self.assertFalse(result['complete'])
        self.assertEqual(result['refused']['reason'], 'module')

    def test_units_are_stripped(self):
        parsed = markers.parse_markers(['COMBATSCN tick t=80.5s a=3 b=4 ahull=12m bhull=x'])
        self.assertEqual(parsed[0].fields, dict(t=80.5, a=3, b=4, ahull=12, bhull='x'))

    def test_profile_keeps_only_windows_inside_the_measurement(self):
        stacks, windows = markers.combat_profile(LOG.splitlines())
        # Windows 3 (folds 400/50) and 4 (fold 500) opened after "window begin" and closed before
        # "window end". Window 2 (300) opened before the begin marker, window 5 (600) closed after
        # the end marker; windows 0/1 belong to warmup and spawning.
        self.assertEqual(dict(stacks), {'slot3;ef4400;9fdef0;8132d0': 900, 'slot3;ef4400;9fdef0;d4f090': 50})
        self.assertEqual(len(windows), 2)

    def test_open_end_profile_runs_to_the_end_of_the_log(self):
        lines = [l for l in LOG.splitlines() if 'window end' not in l]
        self.assertEqual(markers.combat_profile(lines), (collections.Counter(), []))
        stacks, windows = markers.combat_profile(lines, open_end=True)
        self.assertEqual(stacks['slot3;ef4400;9fdef0;8132d0'], 900)
        self.assertEqual(stacks['slot3;ef4400;9fdef0'], 600)
        self.assertEqual(len(windows), 3)

    def test_profile_without_window_is_empty(self):
        stacks, windows = markers.combat_profile(LOG.splitlines()[:6])
        self.assertEqual((dict(stacks), windows), ({}, []))


if __name__ == '__main__':
    unittest.main()
