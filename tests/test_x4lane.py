"""Offline tests of the native test lane tool (dev/x4lane.py, dev/lane/). No X4 is started."""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dev' / 'lane'))
sys.path.insert(0, str(ROOT / 'dev'))
import lanecore as core  # noqa: E402
import x4lane  # noqa: E402

AUTOSTART = ROOT / 'dev' / 'lane' / 'x4lane_autostart' / 'ui' / 'autostart.lua'
UI_XML = '''<?xml version="1.0" encoding="utf-8"?>
<addon name="x4_lan_lab">
  <environment type="menus">
    <dependency name="ego_detailmonitor" />
    <file name="ui/config.lua" />
    <file name="ui/game.lua" />
  </environment>
</addon>
'''
CONFIG = 'X4LAN_CONFIG = {\n    enabled = false,\n    test_profile = "45841",\n    host = "127.0.0.1",\n    token = "",\n}\n'


def make_link(link: Path, target: Path):
    """A directory junction on Windows (no privilege needed), a symlink elsewhere."""
    if os.name == 'nt':
        subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(target)], check=True,
                       capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


def make_lab(root: Path) -> Path:
    lab = root / 'x4_lan_lab'
    (lab / 'ui').mkdir(parents=True)
    (lab / 'md').mkdir()
    (lab / 'content.xml').write_text('<content id="x4_lan_lab" />\n')
    (lab / 'ui.xml').write_text(UI_XML)
    (lab / 'ui' / 'config.lua').write_text(CONFIG)
    (lab / 'ui' / 'game.lua').write_text('-- game\n')
    (lab / 'md' / 'x4lan.xml').write_text('<mdscript />\n')
    return lab


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self):
        # links first, so cleanup never walks into a link target
        for path in sorted(self.tmp.rglob('*'), key=lambda p: -len(p.parts)):
            if core.is_link(path):
                if os.name == 'nt':
                    os.rmdir(path)
                else:
                    os.unlink(path)
        self._tmp.cleanup()


class OverlayTests(Temp):
    def overlay(self, name, files):
        root = self.tmp / name
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return root

    def test_overlays_copy_append_and_load_order(self):
        lab = make_lab(self.tmp / 'lane')
        first = self.overlay('probe', {
            'native/x.dll': b'MZ\x00@X4LANE_PROFILE@\xff',
            'ui/probe.lua': "PROFILE='@X4LANE_PROFILE@' LANE='@X4LANE_LANE@'\n",
            'ui.xml.files': '# comment\nui/probe.lua\n',
            'ui/config.lua.append': "X4LAN_FIXEDTICK_MODE = 'profile'\n",
            'x4lane-overlay.txt': 'notes only\n'})
        second = self.overlay('attn', {
            'ui.xml.files': 'ui/second.lua\nui/probe.lua\n',
            'ui/second.lua': '-- second\n',
            'md/extra.xml': '<mdscript />\n',
            'ui/config.lua.append': 'X4LAN_CONFIG.attention_load_profile = "@X4LANE_PROFILE@"'})
        tokens = {'@X4LANE_PROFILE@': '45850', '@X4LANE_LANE@': 'b1'}
        manifest = core.apply_overlays(lab, [first, second], self.tmp / 'lane', tokens,
                                       ['ui/cli.lua'], ["X4LAN_EXTRA = 1"])
        self.assertEqual((lab / 'native/x.dll').read_bytes(), b'MZ\x00@X4LANE_PROFILE@\xff')  # binary untouched
        self.assertEqual((lab / 'ui/probe.lua').read_text(), "PROFILE='45850' LANE='b1'\n")
        self.assertFalse((lab / 'x4lane-overlay.txt').exists())
        self.assertTrue((lab / 'md/extra.xml').is_file())
        ui = (lab / 'ui.xml').read_text()
        order = [ui.index(f'name="{n}"') for n in ('ui/config.lua', 'ui/probe.lua', 'ui/second.lua',
                                                   'ui/cli.lua', 'ui/game.lua')]
        self.assertEqual(order, sorted(order))
        self.assertEqual(ui.count('ui/probe.lua'), 1)
        self.assertIn('    <file name="ui/probe.lua" />', ui)
        config = (lab / 'ui/config.lua').read_text()
        self.assertLess(config.index("MODE = 'profile'"), config.index('attention_load_profile = "45850"'))
        self.assertLess(config.index('attention_load_profile'), config.index('X4LAN_EXTRA = 1'))
        self.assertTrue(config.endswith('X4LAN_EXTRA = 1\n'))
        self.assertIn('probe:ui/probe.lua', manifest['copied'])
        self.assertEqual(manifest['ui_files'], ['ui/probe.lua', 'ui/second.lua', 'ui/probe.lua', 'ui/cli.lua'])

    def test_ui_entries_are_validated_and_need_config_entry(self):
        with self.assertRaises(core.OverlayError):
            core.insert_ui_files(UI_XML, ['ui/../evil.lua'])
        with self.assertRaises(core.OverlayError):
            core.insert_ui_files(UI_XML, ['ui/x.txt'])
        with self.assertRaises(core.OverlayError):
            core.insert_ui_files(UI_XML.replace('ui/config.lua', 'ui/other.lua'), ['ui/x.lua'])
        self.assertEqual(core.insert_ui_files(UI_XML, ['ui/game.lua']), UI_XML)

    def test_append_needs_existing_target(self):
        lab = make_lab(self.tmp / 'lane')
        overlay = self.overlay('o', {'ui/missing.lua.append': 'x = 1\n'})
        with self.assertRaises(core.OverlayError):
            core.apply_overlays(lab, [overlay], self.tmp / 'lane')

    def test_overlay_with_link_is_refused(self):
        lab = make_lab(self.tmp / 'lane')
        overlay = self.overlay('o', {'ui/a.lua': '-- a\n'})
        (self.tmp / 'elsewhere').mkdir()
        make_link(overlay / 'linked', self.tmp / 'elsewhere')
        with self.assertRaises(core.UnsafeError):
            core.apply_overlays(lab, [overlay], self.tmp / 'lane')

    def test_offline_config_guard(self):
        core.check_offline_config(CONFIG)
        core.check_offline_config(CONFIG + 'X4LAN_CONFIG.engine_hooks = true\n'
                                  'X4LAN_CONFIG.engine_hash_enabled = false\nX4LAN_CONFIG.attention_load_test = true\n')
        for bad in (CONFIG.replace('enabled = false', 'enabled = true'),
                    CONFIG + 'X4LAN_CONFIG.enabled = true\n',
                    CONFIG.replace('token = ""', 'token = "abc"'),
                    CONFIG.replace('127.0.0.1', '76.13.109.115'),
                    CONFIG + 'X4LAN_CONFIG.host = "10.0.0.2"\n',
                    CONFIG.replace('enabled = false,', '')):
            with self.assertRaises(core.OverlayError, msg=bad):
                core.check_offline_config(bad)

    def test_base_lab_check(self):
        lab = make_lab(self.tmp)
        core.check_base_lab(lab)
        (lab / 'ui' / 'config.lua').write_text(CONFIG.replace('token = ""', 'token = "secret"'))
        with self.assertRaises(core.OverlayError):
            core.check_base_lab(lab)


class SafetyTests(Temp):
    def test_rmtree_refuses_links_and_keeps_target(self):
        root = self.tmp / 'lane'
        victim = self.tmp / 'install'
        victim.mkdir()
        (victim / 'precious.dat').write_text('keep')
        tree = root / 'ext'
        tree.mkdir(parents=True)
        (tree / 'own.txt').write_text('x')
        make_link(tree / 'dlc', victim)
        with self.assertRaises(core.UnsafeError):
            core.safe_rmtree(tree, root)
        self.assertEqual((victim / 'precious.dat').read_text(), 'keep')
        self.assertTrue((tree / 'own.txt').exists())
        with self.assertRaises(core.UnsafeError):
            core.safe_rmtree(tree / 'dlc', root)                 # the link itself resolves outside
        self.assertEqual((victim / 'precious.dat').read_text(), 'keep')

    def test_hardlinked_files_are_never_written_or_deleted(self):
        root = self.tmp / 'lane'
        root.mkdir()
        original = self.tmp / 'X4.exe'
        original.write_bytes(b'original')
        os.link(original, root / 'X4.exe')
        with self.assertRaises(core.UnsafeError):
            core.safe_write(root / 'X4.exe', b'changed', root)
        with self.assertRaises(core.UnsafeError):
            core.safe_unlink(root / 'X4.exe', root)
        with self.assertRaises(core.UnsafeError):
            core.safe_rmtree(root, self.tmp)
        self.assertEqual(original.read_bytes(), b'original')

    def test_paths_must_stay_inside_root(self):
        root = self.tmp / 'lane'
        root.mkdir()
        with self.assertRaises(core.UnsafeError):
            core.safe_write(self.tmp / 'outside.txt', 'x', root)
        with self.assertRaises(core.UnsafeError):
            core.safe_rmtree(root, root)
        (self.tmp / 'target').mkdir()
        make_link(root / 'jump', self.tmp / 'target')
        with self.assertRaises(core.UnsafeError):
            core.safe_write(root / 'jump' / 'file.txt', 'x', root)
        self.assertFalse((self.tmp / 'target' / 'file.txt').exists())
        core.safe_write(root / 'ok.txt', 'fine', root)
        self.assertEqual((root / 'ok.txt').read_text(), 'fine')

    def test_copy_tree_substitutes_text_only(self):
        src = self.tmp / 'src'
        (src / 'ui').mkdir(parents=True)
        (src / 'ui' / 'a.lua').write_text('run=@X4LANE_RUN@')
        (src / 'b.bin').write_bytes(b'@X4LANE_RUN@')
        dst = self.tmp / 'lane' / 'dst'
        (self.tmp / 'lane').mkdir()
        core.copy_tree(src, dst, self.tmp / 'lane', {'@X4LANE_RUN@': 'r1'})
        self.assertEqual((dst / 'ui' / 'a.lua').read_text(), 'run=r1')
        self.assertEqual((dst / 'b.bin').read_bytes(), b'@X4LANE_RUN@')
        with self.assertRaises(core.UnsafeError):
            core.copy_tree(src, dst, self.tmp / 'lane')

    def test_outdir_rules(self):
        paths = core.Paths(self.tmp, self.tmp / 'lanes', self.tmp / 'coord', self.tmp / 'profiles',
                           self.tmp / 'install', ROOT)
        for bad in (self.tmp / 'install' / 'x', self.tmp / 'profiles' / '45850', self.tmp / 'lanes' / 'b1' / 'r'):
            with self.assertRaises(core.UsageError):
                core.check_outdir(bad, paths)
        ok = core.check_outdir(self.tmp / 'lanes' / 'runs' / 'r1', paths)
        ok.mkdir(parents=True)
        (ok / 'result.json').write_text('{}')
        with self.assertRaises(core.UsageError):
            core.check_outdir(ok, paths)
        core.check_outdir(ok, paths, overwrite=True)


class ProfileTests(Temp):
    SOURCE = ('\ufeff<?xml version="1.0" encoding="UTF-8"?>\n<root>\n  <fullscreen>false</fullscreen>\n'
              '  <borderless>true</borderless>\n  <res_width>1536</res_width>\n  <res_height>864</res_height>\n'
              '  <gfxquality>1</gfxquality>\n  <autosave>true</autosave>\n  <module>custom_creative</module>\n'
              '  <seed>5</seed>\n</root>\n')

    def test_lane_config(self):
        text = core.lane_config_xml(self.SOURCE)
        core.check_lane_config(text)
        for expected in ('<borderless>false</borderless>', '<res_width>1280</res_width>',
                         '<res_height>720</res_height>', '<autosave>false</autosave>',
                         '<uisafemode>false</uisafemode>', '<gfxquality>1</gfxquality>'):
            self.assertIn(expected, text)
        self.assertNotIn('<module>', text)
        self.assertNotIn('<seed>', text)
        with self.assertRaises(core.Refused):
            core.check_lane_config(text.replace('<autosave>false', '<autosave>true'))
        with self.assertRaises(core.Refused):
            core.check_lane_config(text.replace('</root>', '<load>save_001</load></root>'))

    def test_config_overrides(self):
        text = core.apply_config_overrides(core.lane_config_xml(self.SOURCE), {'frameratetarget': '144'})
        self.assertIn('<frameratetarget>144</frameratetarget>', text)
        for key, value in (('load', 'x'), ('autosave', 'true'), ('module', 'x'), ('Bad', '1'), ('ok', 'a b')):
            with self.assertRaises(core.UsageError):
                core.apply_config_overrides(text, {key: value})

    def test_content_has_no_workshop_selection(self):
        text = core.lane_content_xml(['ego_dlc_split', 'ego_dlc_boron'], ['x4lane_combat'])
        ids = [line.split('"')[1] for line in text.splitlines() if 'extension id=' in line]
        self.assertEqual(ids, ['ego_dlc_boron', 'ego_dlc_split', 'x4_lan_lab', 'x4lane_autostart', 'x4lane_combat'])
        self.assertNotIn('ws_', text)
        self.assertNotIn('enabled="false"', text)
        with self.assertRaises(core.UsageError):
            core.lane_content_xml([], ['ws_123'])

    def test_dlc_ids_and_extension_id(self):
        install = self.tmp / 'install'
        for name in ('ego_dlc_boron', 'ego_dlc_split', 'x4_lan_lab', 'ws_1'):
            (install / 'extensions' / name).mkdir(parents=True)
            (install / 'extensions' / name / 'content.xml').write_text(f'<content id="{name}" />')
        self.assertEqual(core.dlc_ids(install), ['ego_dlc_boron', 'ego_dlc_split'])
        self.assertEqual(core.extension_id(install / 'extensions' / 'x4_lan_lab'), 'x4_lan_lab')
        (install / 'bad').mkdir()
        (install / 'bad' / 'content.xml').write_text('<content id="a b" />')
        with self.assertRaises(core.UsageError):
            core.extension_id(install / 'bad')

    def test_profile_from_command_line(self):
        cases = {'"C:\\X4\\X4.exe" -personalfolderid 45843 -skipintro': '45843',
                 'X4.exe -skipintro -personalfolderid "45850"': '45850',
                 'X4.exe -personalfolderid:45851': '45851', 'X4.exe -skipintro': None, None: None}
        for line, expected in cases.items():
            self.assertEqual(core.profile_from_cmdline(line), expected, line)

    def test_autostart_settings(self):
        text = core.autostart_settings(enabled=True, lane='b1', run='20261026-1-b1-walk-12', quit_after_s=1900.0,
                                       alive_s=10)
        self.assertIn('enabled = true', text)
        self.assertIn('lane = "b1"', text)
        self.assertIn('quit_after_s = 1900.0', text)
        lua = LuaRuntime()
        lua.execute(text)
        self.assertEqual(lua.eval('X4LANE_AUTOSTART.run'), '20261026-1-b1-walk-12')
        with self.assertRaises(core.UsageError):
            core.autostart_settings(lane='b1"; os.exit()')


class LockTests(Temp):
    def info(self, run='r1'):
        return {'agent': 'test', 'purpose': 'unit', 'run': run, 'machine': 'here', 'pid': 1}

    def test_acquire_busy_release(self):
        path = self.tmp / 'lane-b1.lock'
        lock = core.DirLock.acquire(path, self.info())
        owner = core.read_owner(path)
        self.assertEqual(owner['agent'], 'test')
        self.assertIn('updated', owner)
        with self.assertRaises(core.LockBusy):
            core.DirLock.acquire(path, self.info('r2'), pid_alive=lambda pid, created: True)
        self.assertTrue(lock.release())
        self.assertFalse(path.exists())

    def test_dead_owner_is_taken_over_and_old_owner_does_not_remove_it(self):
        path = self.tmp / 'lane-b2.lock'
        import socket
        old = core.DirLock.acquire(path, dict(self.info('old'), machine=socket.gethostname(), pid=999999))
        new = core.DirLock.acquire(path, self.info('new'), pid_alive=lambda pid, created: False)
        self.assertEqual(core.read_owner(path)['run'], 'new')
        self.assertFalse(old.release())                          # taken over: left alone
        self.assertTrue(path.exists())
        self.assertTrue(new.release())

    def test_live_owner_is_not_stale_but_old_lock_is(self):
        path = self.tmp / 'lane-b1.lock'
        import socket
        core.DirLock.acquire(path, dict(self.info('a'), machine=socket.gethostname(), pid=4242))
        with self.assertRaises(core.LockBusy):
            core.DirLock.acquire(path, self.info('b'), pid_alive=lambda pid, created: True)
        old = time.time() - core.LOCK_STALE_S - 60
        os.utime(path / 'owner.txt', (old, old))
        lock = core.DirLock.acquire(path, self.info('b'), pid_alive=lambda pid, created: True)
        self.assertEqual(core.read_owner(path)['run'], 'b')
        lock.release()

    def test_foreign_lock_is_never_taken_without_stale_logic(self):
        path = self.tmp / 'quiet.lock'
        path.mkdir()
        (path / 'owner.txt').write_text('owner: lead\nreason: test\n')
        old = time.time() - 10 * 3600
        os.utime(path / 'owner.txt', (old, old))
        with self.assertRaises(core.LockBusy):
            core.DirLock.acquire(path, self.info(), take_stale=False)
        (path / 'sub').mkdir()
        with self.assertRaises(core.UnsafeError):
            core._remove_lock_dir(path, 'test')


class HandoffTests(Temp):
    def quiet(self, handoff=None):
        path = self.tmp / 'quiet.lock'
        path.mkdir()
        lines = ['agent: overhaul-lead', 'purpose: drain for frame-diet']
        if handoff:
            lines.append(f'handoff: {handoff}')
        (path / 'owner.txt').write_text(''.join(line + chr(10) for line in lines))
        return path

    def test_named_agent_adopts_and_removes_at_the_end(self):
        path = self.quiet('frame-diet')
        lock = core.DirLock.adopt(path, {'agent': 'frame-diet', 'run': 'r9'}, 'frame-diet')
        owner = core.read_owner(path)
        self.assertEqual(owner['agent'], 'frame-diet')
        self.assertEqual(owner['handed_from'], 'overhaul-lead')
        self.assertEqual(owner['handed_from_purpose'], 'drain for frame-diet')
        self.assertNotIn('handoff', owner)                      # a second adoption is impossible
        with self.assertRaises(core.LockBusy):
            core.DirLock.adopt(path, {'agent': 'frame-diet', 'run': 'r10'}, 'frame-diet')
        self.assertTrue(lock.release())
        self.assertFalse(path.exists())

    def test_other_agents_and_missing_handoff_are_refused(self):
        path = self.quiet('frame-diet')
        with self.assertRaises(core.LockBusy):
            core.DirLock.adopt(path, {'agent': 'walk', 'run': 'r1'}, 'walk')
        plain = self.tmp / 'plain'
        plain.mkdir()
        (plain / 'owner.txt').write_text('agent: overhaul-lead' + chr(10))
        with self.assertRaises(core.LockBusy):
            core.DirLock.adopt(plain, {'agent': 'frame-diet', 'run': 'r1'}, 'frame-diet')
        self.assertEqual(core.read_owner(path)['handoff'], 'frame-diet')


class HandoffChainTests(Temp):
    def window(self, chain):
        path = self.tmp / 'quiet.lock'
        path.mkdir()
        (path / 'owner.txt').write_text(''.join(line + chr(10) for line in (
            'agent: overhaul-lead', 'purpose: batched timing window', f'handoff: {chain}')))
        return path

    def test_chain_is_handed_on_in_order_and_the_last_removes_it(self):
        path = self.window('frame-diet, combat, ecs-core')
        with self.assertRaises(core.LockBusy):
            core.DirLock.adopt(path, {'agent': 'combat', 'run': 'c0'}, 'combat')       # not first
        first = core.DirLock.adopt(path, {'agent': 'frame-diet', 'run': 'f1'}, 'frame-diet')
        owner = core.read_owner(path)
        self.assertEqual((owner['then'], owner['handed_from']), ('combat, ecs-core', 'overhaul-lead'))
        self.assertNotIn('handoff', owner)                     # nobody can adopt while it runs
        with self.assertRaises(core.LockBusy):
            core.DirLock.adopt(path, {'agent': 'combat', 'run': 'c1'}, 'combat')
        self.assertTrue(core.hand_on(first, 'frame-diet', now=5000))
        owner = core.read_owner(path)
        self.assertEqual((owner['handoff'], owner['handed_from'], owner['handoff_since']),
                         ('combat, ecs-core', 'frame-diet', '5000'))
        second = core.DirLock.adopt(path, {'agent': 'combat', 'run': 'c2'}, 'combat')
        self.assertEqual(core.read_owner(path)['handed_from'], 'frame-diet')
        self.assertTrue(core.hand_on(second, 'combat'))
        third = core.DirLock.adopt(path, {'agent': 'ecs-core', 'run': 'e3'}, 'ecs-core')
        self.assertFalse(core.hand_on(third, 'ecs-core'))       # nobody left: release as before
        self.assertTrue(third.release())
        self.assertFalse(path.exists())

    def test_members_that_do_not_adopt_in_time_are_skipped(self):
        path = self.window('frame-diet, combat, physics')
        self.assertIsNone(core.advance_handoff(path, now=10 ** 10))   # the first member is never timed
        first = core.DirLock.adopt(path, {'agent': 'frame-diet', 'run': 'f1'}, 'frame-diet')
        core.hand_on(first, 'frame-diet', now=1000)
        self.assertIsNone(core.advance_handoff(path, now=1000 + core.HANDOFF_ADOPT_S))
        self.assertEqual(core.advance_handoff(path, now=1001 + core.HANDOFF_ADOPT_S), 'skipped combat')
        owner = core.read_owner(path)
        self.assertEqual((owner['handoff'], owner['skipped']), ('physics', 'combat'))
        self.assertEqual(core.advance_handoff(path, now=1002 + 2 * core.HANDOFF_ADOPT_S),
                         'removed (skipped physics)')
        self.assertFalse(path.exists())


class FakeWindows(Temp):
    """Fake Windows hooks (Steam, RAM, processes, locks) so the real preflight code runs anywhere."""

    def setUp(self):
        super().setUp()
        import types
        t = self.tmp
        self.paths = core.Paths(t, t / 'lanes', t / 'coord', t / 'profiles', t / 'install', ROOT)
        self.paths.coord.mkdir()
        self.paths.x4lock_exe.parent.mkdir(parents=True)
        self.paths.x4lock_exe.write_bytes(b'MZ')
        self.others = []
        fake = types.SimpleNamespace(
            steam_status=lambda: {'running': True}, memory_status=lambda: (64 * 2**30, 40 * 2**30),
            processes=lambda name='X4.exe': list(self.others), process_created=lambda pid: 1,
            pid_alive=lambda pid, created=None: True)
        self.saved = (x4lane.win, x4lane.verify_view, x4lane.check_base)
        x4lane.win = lambda: fake
        x4lane.verify_view = lambda paths, lane, check_hash=True: []
        x4lane.check_base = lambda paths, base_lab=None: self.paths.base_lab

    def tearDown(self):
        x4lane.win, x4lane.verify_view, x4lane.check_base = self.saved
        super().tearDown()

    def run_for(self, agent, timing):
        argv = ['run', '--agent', agent, '--collect', str(self.tmp / 'out'), '--timeout', '60', '--lane', 'b1']
        run = x4lane.Run(x4lane.parser().parse_args(argv + (['--timing'] if timing else [])), self.paths)
        return run

    def quiet(self, handoff):
        path = self.paths.quiet_lock
        path.mkdir()
        (path / 'owner.txt').write_text(''.join(line + chr(10) for line in (
            'agent: overhaul-lead', 'purpose: drain', f'handoff: {handoff}')))
        return path



class PreflightHandoffTests(FakeWindows):
    """Run.preflight(): who may start while quiet.lock exists."""

    def test_only_the_named_timing_run_adopts(self):
        quiet = self.quiet('frame-diet')
        for agent, timing in (('walk', True), ('frame-diet', False)):
            with self.assertRaises(x4lane.TransientRefusal) as refused:
                self.run_for(agent, timing).preflight()
            self.assertIn('quiet.lock is held', str(refused.exception))
            self.assertIn('handoff to frame-diet', str(refused.exception))
        import types
        self.others = [types.SimpleNamespace(pid=7, exe='C:/other/X4.exe', cmdline='-personalfolderid 45843')]
        run = self.run_for('frame-diet', True)
        with self.assertRaises(x4lane.TransientRefusal) as waiting:
            run.preflight()
        self.assertIn('no other X4', str(waiting.exception))
        self.assertFalse(self.paths.lane('b1').lock.exists())      # lane lock given back while waiting
        self.assertEqual(core.read_owner(quiet)['handoff'], 'frame-diet')
        self.others = []
        run = self.run_for('frame-diet', True)
        run.preflight()
        owner = core.read_owner(quiet)
        self.assertEqual((owner['agent'], owner['handed_from'], owner['run']), ('frame-diet', 'overhaul-lead', run.run_id))
        self.assertTrue(run.result['quiet_lock']['held_by_this_run'])
        self.assertEqual(run.result['quiet_lock']['handed_from'], 'overhaul-lead')
        run.release()
        self.assertFalse(quiet.exists())
        self.assertFalse(self.paths.lane('b1').lock.exists())

    def test_timing_without_handoff_creates_and_removes_its_own_lock(self):
        run = self.run_for('frame-diet', True)
        run.preflight()
        self.assertEqual(core.read_owner(self.paths.quiet_lock)['run'], run.run_id)
        run.release()
        self.assertFalse(self.paths.quiet_lock.exists())


class SessionPieceTests(Temp):
    def test_repin_profile_moves_every_gate_to_the_lane(self):
        lab = make_lab(self.tmp / 'lane')
        (lab / 'ui' / 'simtick.lua').write_text("assert(cfg.test_profile=='45841','simtick requires 45841')\n")
        (lab / 'ui' / 'seed.lua').write_text("assert(path:sub(-#'/45841/save')=='/45841/save','x')\n"
                                             "local p=cfg.test_profile or \"45841\"\nport = 45841\n")
        (lab / 'ui' / 'bubble.lua').write_text("assert(profile=='45841' or profile=='45842','x')\n")
        changed = core.repin_profile(lab, '45853', self.tmp / 'lane')
        self.assertEqual(changed, {'ui/bubble.lua': 1, 'ui/config.lua': 1, 'ui/seed.lua': 3, 'ui/simtick.lua': 1})
        self.assertIn('test_profile = "45853"', (lab / 'ui' / 'config.lua').read_text())
        seed = (lab / 'ui' / 'seed.lua').read_text()
        self.assertIn("'/45853/save')=='/45853/save'", seed)
        self.assertIn('port = 45841', seed)                       # unquoted numbers are not gates
        self.assertIn("profile=='45853' or profile=='45842'", (lab / 'ui' / 'bubble.lua').read_text())
        self.assertIn("'simtick requires 45841'", (lab / 'ui' / 'simtick.lua').read_text())  # message only
        with self.assertRaises(core.UnsafeError):
            core.repin_profile(lab, '45841', self.tmp / 'lane')

    def test_loopback_config_guard(self):
        ok = CONFIG + ('X4LAN_CONFIG.enabled = true\nX4LAN_CONFIG.host = "127.0.0.1"\n'
                       'X4LAN_CONFIG.token = "0123456789abcdef0123456789abcdef"\nX4LAN_CONFIG.port = 50123\n')
        core.check_loopback_config(ok)
        for bad in (ok + 'X4LAN_CONFIG.host = "localhost"\n', ok + 'X4LAN_CONFIG.relay = "10.1.2.3"\n',
                    ok.replace('127.0.0.1', '76.13.109.115'), ok + 'X4LAN_CONFIG.url = "https://example.com"\n',
                    ok + 'X4LAN_CONFIG.host = "0.0.0.0"\n'):
            with self.assertRaises(core.OverlayError, msg=bad):
                core.check_loopback_config(bad)
        with self.assertRaises(core.OverlayError):
            core.check_offline_config(ok)                          # single runs stay offline

    def test_free_port_and_server_command(self):
        for attempt in range(5):                  # another process may be handed the port in between
            port = core.free_loopback_port()
            self.assertTrue(1024 <= port <= 65535)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.bind(('127.0.0.1', port))
                break
            except OSError:
                if attempt == 4:
                    raise
        values = {k: f'<{k}>' for k in x4lane.SERVER_TOKENS}
        values['port'] = port
        command = x4lane.server_command('{python} -m x4lan.host --bind 127.0.0.1 --port {port} --token {token}',
                                        values)
        self.assertEqual(command, f'<python> -m x4lan.host --bind 127.0.0.1 --port {port} --token <token>')
        with self.assertRaises(core.UsageError):
            x4lane.server_command('relay --port {prot}', values)

    def test_per_client_options_and_endpoint_rule(self):
        self.assertEqual(x4lane.per_client(['1:a', '2:b=c', '1:d'], '--client-config', 2),
                         {1: ['a', 'd'], 2: ['b=c']})
        for bad in (['3:x'], ['x'], ['0:x']):
            with self.assertRaises(core.UsageError):
                x4lane.per_client(bad, '--client-lua', 2)
        good = [{'proto': 'udp', 'local': '127.0.0.1', 'lport': 1},
                {'proto': 'tcp', 'local': '127.0.0.1', 'remote': '0.0.0.0', 'lport': 1},
                {'proto': 'tcp', 'local': '127.0.0.1', 'remote': '127.0.0.1', 'lport': 1},
                {'proto': 'udp6', 'local': '::1', 'lport': 1}]
        bad = [{'proto': 'udp', 'local': '0.0.0.0', 'lport': 1},
               {'proto': 'tcp', 'local': '127.0.0.1', 'remote': '93.184.216.34', 'lport': 1},
               {'proto': 'udp6', 'local': '::', 'lport': 1},
               {'proto': 'tcp', 'local': '192.168.1.5', 'remote': '0.0.0.0', 'lport': 1}]
        self.assertTrue(all(x4lane.loopback_endpoint(e) for e in good))
        self.assertFalse(any(x4lane.loopback_endpoint(e) for e in bad))

    def test_session_telemetry_overlay_installs_loopback_config(self):
        root = self.tmp / 'worktree'
        (root / '.local' / 'engine').mkdir(parents=True)
        (root / '.local' / 'engine' / 'x4lan_simtick_64.dll').write_bytes(b'MZ')
        out = self.tmp / 'overlay-session'
        self.assertEqual(x4lane.main(['make-overlay', 'session-telemetry', '--out', str(out), '--root', str(root)]), 0)
        lab = make_lab(self.tmp / 'lane')
        tokens = {'@X4LANE_PORT@': '50123', '@X4LANE_TOKEN@': 'ab' * 16, '@X4LANE_BUILD@': 'lane-session',
                  '@X4LANE_BUBBLE@': 'bubble-01', '@X4LANE_SLOT@': 'p2'}
        core.apply_overlays(lab, [out], self.tmp / 'lane', tokens)
        config = (lab / 'ui' / 'config.lua').read_text()
        for expected in ('X4LAN_CONFIG.port = 50123', 'X4LAN_CONFIG.slot = "p2"', 'X4LAN_CONFIG.enabled = true'):
            self.assertIn(expected, config)
        core.check_loopback_config(config)
        self.assertEqual((lab / 'native' / 'x4lan_simtick_64.dll').read_bytes(), b'MZ')


class SessionPreflightTests(FakeWindows):
    def session(self, *extra):
        argv = ['session', '--agent', 'player-factions', '--collect', str(self.tmp / 'sess'), '--timeout', '60',
                '--clients', '2', *extra]
        run = x4lane.SessionRun(x4lane.parser().parse_args(argv), self.paths)
        run.outdir = self.tmp / 'sess'
        run.outdir.mkdir(exist_ok=True)
        run.lanes = None
        for index in (1, 2):
            client = x4lane.Run(run.client_args(index, {'overlay': {1: [], 2: []}, 'extension': {1: [], 2: []},
                                                        'lua': {1: [], 2: []}, 'config': {1: [], 2: []}}), self.paths)
            client.network = 'loopback'
            run.clients.append(client)
        run.session_id = 'sess-1'
        return run

    def test_takes_two_lanes_and_gives_both_back_when_one_is_missing(self):
        run = self.session()
        run.preflight()
        self.assertEqual([c.lane.name for c in run.clients], ['b1', 'b2'])
        for client in run.clients:
            client.give_back_lane()
        blockers = [core.DirLock.acquire(self.paths.lane(n).lock, {'agent': 'x', 'run': n}) for n in [n for n in core.LANE_PROFILES if n != 'b1']]
        run = self.session()
        with self.assertRaises(x4lane.TransientRefusal):
            run.preflight()
        self.assertFalse(self.paths.lane('b1').lock.exists())    # nothing kept after a partial take
        for blocker in blockers:
            blocker.release()

    def test_ram_guard_counts_every_instance(self):
        run = self.session('--min-free-gb', '25')                 # fake host has 40 GB free
        with self.assertRaises(x4lane.TransientRefusal) as refused:
            run.preflight()
        self.assertIn('need 50 GB', str(refused.exception))
        self.assertFalse(any(self.paths.lane(n).lock.exists() for n in core.LANE_PROFILES))

    def test_quiet_lock_blocks_sessions(self):
        self.quiet('somebody-else')
        with self.assertRaises(x4lane.TransientRefusal):
            self.session().preflight()


class FakeClock:
    def __init__(self, on_sleep=None):
        self.now, self.on_sleep, self.sleeps = 1000.0, on_sleep, 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self)

    def time(self):
        return time.time()


class QueueTests(FakeWindows):
    def setUp(self):
        super().setUp()
        self.saved_time = x4lane.time

    def tearDown(self):
        x4lane.time = self.saved_time
        super().tearDown()

    def runner(self, wait, agent='walk', timing=False):
        import types
        self.refreshed = 0

        def refresh():
            self.refreshed += 1
        return types.SimpleNamespace(paths=self.paths, args=types.SimpleNamespace(wait=wait, agent=agent,
                                                                                    timing=timing),
                                     refresh_reserved=refresh)

    def foreign_ticket(self):
        ticket = core.Ticket.create(self.paths.queue, {'agent': 'economy', 'need': 2, 'pid': 4242,
                                                       'machine': 'elsewhere'})
        return ticket

    def test_immediate_run_is_refused_while_others_queue(self):
        self.foreign_ticket()
        attempts = []
        with self.assertRaises(x4lane.TransientRefusal) as refused:
            x4lane.wait_in_queue(self.runner(0), lambda: attempts.append(1), lambda: None, 1, 'run')
        self.assertIn('queued run(s)', str(refused.exception))
        self.assertEqual(attempts, [])
        self.quiet('walk')                                        # a quiet-window handoff bypasses the queue
        x4lane.wait_in_queue(self.runner(0, timing=True), lambda: attempts.append(1), lambda: None, 1, 'run')
        self.assertEqual(attempts, [1])

    def test_only_the_head_attempts_and_the_ticket_is_removed(self):
        ahead = self.foreign_ticket()
        attempts = []
        clock = FakeClock(on_sleep=lambda c: ahead.remove() if c.sleeps == 2 else None)
        x4lane.time = clock
        x4lane.wait_in_queue(self.runner(600), lambda: attempts.append(clock.now), lambda: None, 1, 'run')
        self.assertEqual(len(attempts), 1)                         # never attempted while second in line
        self.assertEqual(clock.sleeps, 2)
        self.assertEqual(core.live_tickets(self.paths.queue), [])
        self.assertGreaterEqual(self.refreshed, 2)

    def test_timing_waiters_never_hold_the_queue(self):
        core.Ticket.create(self.paths.queue, {'agent': 'frame-diet', 'need': 1, 'pid': 4243, 'machine': 'elsewhere',
                                              'timing': True})
        attempts = []
        x4lane.wait_in_queue(self.runner(0), lambda: attempts.append('now'), lambda: None, 1, 'run')
        x4lane.time = FakeClock()
        x4lane.wait_in_queue(self.runner(600), lambda: attempts.append('queued'), lambda: None, 1, 'run')
        self.assertEqual(attempts, ['now', 'queued'])           # a timing ticket ahead blocks nobody
        ahead = self.foreign_ticket()                             # a normal run ahead ...
        x4lane.wait_in_queue(self.runner(600, timing=True), lambda: attempts.append('timing'), lambda: None, 1,
                             'run')
        self.assertEqual(attempts[-1], 'timing')                  # ... does not stop a timing attempt
        ahead.remove()

    def test_deadline_gives_back_reservations(self):
        clock = FakeClock()
        x4lane.time = clock
        given_back = []

        def attempt():
            raise x4lane.TransientRefusal('no free lane')
        with self.assertRaises(x4lane.TransientRefusal) as refused:
            x4lane.wait_in_queue(self.runner(60), attempt, lambda: given_back.append(1), 2, 'session')
        self.assertIn('--wait 60 s elapsed (no free lane)', str(refused.exception))
        self.assertTrue(given_back)
        self.assertEqual(list(self.paths.queue.glob('q-*.json')), [])

    def test_dead_and_stale_tickets_are_dropped(self):
        dead = core.Ticket.create(self.paths.queue, {'agent': 'x', 'pid': 99, 'machine': socket.gethostname()})
        old = core.Ticket.create(self.paths.queue, {'agent': 'y', 'pid': 98, 'machine': 'elsewhere'})
        past = time.time() - core.TICKET_STALE_S - 10
        os.utime(old.path, (past, past))
        live = core.Ticket.create(self.paths.queue, {'agent': 'z', 'pid': 97, 'machine': 'elsewhere'})
        tickets = core.live_tickets(self.paths.queue, pid_alive=lambda pid, created=None: pid != 99)
        self.assertEqual([info['agent'] for _path, info in tickets], ['z'])
        self.assertFalse(dead.path.exists())
        self.assertFalse(old.path.exists())
        live.remove()


class StopAndReserveTests(FakeWindows):
    def test_stop_marker_is_matched_to_our_x4_only(self):
        import types
        run = self.run_for('walk', False)
        run.lane = self.paths.lane('b1')
        run.lane.dir.mkdir(parents=True)
        run.proc = types.SimpleNamespace(pid=321)
        self.assertIsNone(run.stop_request())
        core.write_json(run.lane.dir / x4lane.STOP_FILE, {'pid': 999, 'by': 'lead'})
        self.assertIsNone(run.stop_request())
        core.write_json(run.lane.dir / x4lane.STOP_FILE, {'pid': 321, 'by': 'lead', 'reason': 'user test'})
        self.assertEqual(run.stop_request()['reason'], 'user test')

    def test_queued_session_keeps_lanes_it_already_has(self):
        argv = ['session', '--agent', 'player-factions', '--collect', str(self.tmp / 'sess'), '--timeout', '60']
        run = x4lane.SessionRun(x4lane.parser().parse_args(argv), self.paths)
        run.outdir, run.lanes, run.session_id = self.tmp / 'sess', None, 's'
        empty = {1: [], 2: []}
        for index in (1, 2):
            client = x4lane.Run(run.client_args(index, {'overlay': empty, 'extension': empty, 'lua': empty,
                                                        'config': empty}), self.paths)
            run.clients.append(client)
        blockers = [core.DirLock.acquire(self.paths.lane(n).lock, {'agent': 'x', 'run': n}) for n in [n for n in core.LANE_PROFILES if n != 'b1']]
        with self.assertRaises(x4lane.TransientRefusal) as refused:
            run.preflight(keep=True)
        self.assertIn("reserved so far: ['b1']", str(refused.exception))
        self.assertTrue(self.paths.lane('b1').lock.exists())      # kept while queued
        blockers[2].release()                                    # b4 frees up
        run.preflight(keep=True)
        self.assertEqual([c.lane.name for c in run.clients], ['b1', 'b4'])
        run.refresh_reserved()
        self.assertTrue(self.paths.lane('b1').lock.exists())
        run.reserved_since -= x4lane.RESERVE_MAX_S + 1               # reserved too long while queued
        run.refresh_reserved()
        self.assertFalse(self.paths.lane('b1').lock.exists())
        self.assertFalse(self.paths.lane('b4').lock.exists())
        self.assertIsNone(run.reserved_since)
        run.give_back_all()
        for blocker in blockers[:2] + blockers[3:]:
            blocker.release()
        self.assertFalse(any(self.paths.lane(n).lock.exists() for n in core.LANE_PROFILES))


class DoctorTests(FakeWindows):
    def lane_dirs(self, name='b1'):
        lane = self.paths.lane(name)
        make_lab(self.paths.base)                                  # lanes/base/x4_lan_lab
        lane.extensions.mkdir(parents=True)
        core.copy_tree(self.paths.base_lab, lane.lab, lane.dir)
        lane.profile_base.mkdir(parents=True)
        for file, text in (('config.xml', '<root><autosave>false</autosave></root>'), ('inputmap.xml', '<x/>'),
                           ('content.xml', '<content/>')):
            (lane.profile_base / file).write_text(text)
        lane.profile_dir.mkdir(parents=True)
        return lane

    def levels(self, findings, lane='b1'):
        return [(f['level'], f['what']) for f in findings if f['what'].startswith(f'lane {lane}')]

    def test_dirty_free_lane_is_reported_and_restored(self):
        lane = self.lane_dirs()
        (lane.extensions / 'x4lane_autostart').mkdir()
        core.write_json(lane.state_file, {'phase': 'running', 'run': 'dead-run'})
        (lane.dir / x4lane.STOP_FILE).write_text('{}')
        found = self.levels(x4lane.doctor(self.paths, fix=False))
        self.assertIn(('problem', 'lane b1: not in base state while free'), found)
        found = self.levels(x4lane.doctor(self.paths, fix=True))
        self.assertIn(('fixed', 'lane b1: restored to base'), found)
        self.assertEqual(sorted(p.name for p in lane.extensions.iterdir()), ['x4_lan_lab'])
        self.assertEqual(json.loads(lane.state_file.read_text())['phase'], 'idle')
        self.assertFalse((lane.dir / x4lane.STOP_FILE).exists())
        self.assertFalse(lane.lock.exists())
        self.assertIn(('ok', 'lane b1: free and clean'), self.levels(x4lane.doctor(self.paths)))

    def test_lane_in_use_is_left_alone_and_stale_lock_is_removed(self):
        lane = self.lane_dirs()
        (lane.extensions / 'x4lane_autostart').mkdir()
        lock = core.DirLock.acquire(lane.lock, {'agent': 'walk', 'run': 'live', 'machine': socket.gethostname(),
                                                'pid': 1234})
        found = self.levels(x4lane.doctor(self.paths, fix=True))
        self.assertEqual([level for level, _ in found], ['ok'])     # in use: not touched
        self.assertTrue((lane.extensions / 'x4lane_autostart').exists())
        self.saved_alive = x4lane.win().pid_alive
        fake = x4lane.win()
        fake.pid_alive = lambda pid, created=None: False           # the owner died
        found = self.levels(x4lane.doctor(self.paths, fix=True))
        self.assertIn(('fixed', 'lane b1: stale lock removed'), found)
        self.assertIn(('fixed', 'lane b1: restored to base'), found)
        self.assertFalse(lane.lock.exists())
        fake.pid_alive = self.saved_alive
        lock.held = False

    def test_orphan_lane_x4_is_terminated_only_with_fix(self):
        import types
        lane = self.lane_dirs()
        killed = []
        fake = x4lane.win()
        fake.terminate = lambda pid, exe: killed.append((pid, str(exe))) or True
        self.others = [types.SimpleNamespace(pid=77, exe=str(lane.exe), cmdline='-personalfolderid 45850', created=1),
                       types.SimpleNamespace(pid=78, exe='C:/Other/X4.exe', cmdline='', created=1)]
        found = self.levels(x4lane.doctor(self.paths, fix=False))
        self.assertIn(('problem', 'lane b1: orphan X4 pid 77 without a runner'), found)
        self.assertEqual(killed, [])
        x4lane.doctor(self.paths, fix=True)
        self.assertEqual(killed, [(77, str(lane.exe))])             # never the other X4


class StallTests(FakeWindows):
    def test_silent_log_after_ingame_is_a_stall(self):
        import types
        run = self.run_for('walk', False)
        run.args.stall, run.args.alive = 60.0, 10.0
        run.lane = self.paths.lane('b1')
        run.lock = types.SimpleNamespace(refresh=lambda: None)
        log = self.tmp / 'x4lan-debug.txt'
        log.write_text('[=ERROR=] 0.00 [X4LANE] AUTOSTART INGAME gamestart=custom_creative\n')
        clock = [100.0]
        run.watch = core.LogWatcher(log, clock=lambda: clock[0])
        run.proc = types.SimpleNamespace(pid=5, poll=lambda: None, returncode=None)
        fake = x4lane.win()
        fake.dialogs_of = lambda pid: []
        fake.window_hung = lambda pid: True
        run.check_dialogs = lambda: None
        run.sample = lambda: None
        run.begin_monitor()
        self.assertIsNone(run.poll(100.0))
        self.assertIsNone(run.poll(150.0))
        reason, message = run.poll(170.0)
        self.assertEqual(reason, 'stalled')
        self.assertIn('not responding', message)
        run.args.alive = 0                                           # no heartbeat: stall check off
        self.assertIsNone(run.poll(500.0))


class ProtectedTests(FakeWindows):
    INSTALL = r'C:\Program Files (x86)\Steam\steamapps\common\X4 Foundations\X4.exe'
    GUEST = (r'C:\Users\developer\Documents\X4Mods\X4-Multiplayer-fixed-tick\.local\sandbox-baseline\guest\game-view'
             r'\steamapps\common\X4 Foundations\X4.exe')

    def proc(self, pid, exe, profile):
        import types
        return types.SimpleNamespace(pid=pid, exe=exe, cmdline=f'"{exe}" -personalfolderid {profile} -skipintro',
                                     created=1)

    def setUp(self):
        super().setUp()
        self.calls = []
        self.mutex = True
        self.saved_x4lock = x4lane.x4lock
        fake = x4lane.win()
        fake.named_object_exists = lambda name: self.mutex

        def x4lock(paths, *args, timeout=60):
            self.calls.append(tuple(str(a) for a in args))
            if args[0] == 'query':
                return (0 if int(args[2]) in self.holders else 3), []
            if args[0] == 'release':
                self.mutex = False
                return 0, ['released']
            return 0, []
        x4lane.x4lock = x4lock
        self.holders = set()

    def tearDown(self):
        x4lane.x4lock = self.saved_x4lock
        super().tearDown()

    def run_ready(self):
        run = self.run_for('walk', False)
        run.lane = self.paths.lane('b1')
        run.started = []
        run.start_x4 = lambda: run.started.append(True)
        run.wait_for_mutex = lambda: None
        return run

    def test_protected_rules(self):
        lane_exe = str(self.paths.lane('b1').exe)
        self.assertTrue(x4lane.is_protected(self.proc(1, self.INSTALL, '70000002')))   # the user's own game
        self.assertTrue(x4lane.is_protected(self.proc(2, self.GUEST, '45842')))
        self.assertTrue(x4lane.is_protected(self.proc(3, lane_exe, '45841')))            # profile rule
        self.assertTrue(x4lane.is_protected(self.proc(4, None, '45850')))               # unreadable: fail closed
        self.assertFalse(x4lane.is_protected(self.proc(5, lane_exe, '45850')))

    def test_protected_holder_is_never_queried_or_released_and_the_run_waits(self):
        client = self.proc(41, self.INSTALL, '45841')
        self.others = [client, self.proc(50, str(self.paths.lane('b2').exe), '45851')]
        run = self.run_ready()
        with self.assertRaises(x4lane.ProtectedHolder) as waiting:
            run.start_sequence()
        self.assertIn('protected X4 41/45841', str(waiting.exception))
        self.assertNotIn('41', [c[2] for c in self.calls if len(c) > 2])               # never x4lock-ed
        self.assertEqual(run.started, [])

    def test_lane_holder_is_released_next_to_protected_clients(self):
        self.others = [self.proc(41, self.INSTALL, '45841'), self.proc(42, self.GUEST, '45842'),
                       self.proc(50, str(self.paths.lane('b2').exe), '45851')]
        self.holders = {50}
        run = self.run_ready()
        run.start_sequence()
        self.assertEqual(run.started, [True])
        self.assertIn(('release', '--pid', '50', '--expect-path', str(self.paths.lane('b2').exe), '--wait-ms', '30000'),
                      self.calls)
        self.assertFalse(any(c[0] in ('query', 'release') and c[2] in ('41', '42') for c in self.calls))

    def test_preflight_waits_while_only_protected_clients_run(self):
        self.others = [self.proc(41, self.INSTALL, '45841'), self.proc(42, self.GUEST, '45842')]
        with self.assertRaises(x4lane.ProtectedHolder):
            self.run_for('walk', False).preflight()
        self.assertFalse(self.paths.lane('b1').lock.exists())
        self.mutex = False                                       # x4mods-61 freed it
        run = self.run_for('walk', False)
        run.preflight()
        self.assertTrue(self.paths.lane('b1').lock.exists())
        run.give_back_lane()

    def test_launch_waits_within_the_wait_budget(self):
        self.others = [self.proc(41, self.INSTALL, '45841')]
        run = self.run_ready()
        run.lock = core.DirLock.acquire(self.paths.lane('b1').lock, {'agent': 'walk', 'run': 'r'})
        clock = FakeClock(on_sleep=lambda c: setattr(self, 'mutex', c.sleeps < 3))
        saved, x4lane.time = x4lane.time, clock
        try:
            run.wait_deadline = clock.now + 100
            run.launch_x4()                                       # freed after 3 polls: starts
            self.assertEqual(run.started, [True])
            self.mutex, run.started = True, []
            clock.on_sleep = None
            run.wait_deadline = clock.now + 25
            with self.assertRaises(x4lane.ProtectedHolder):
                run.launch_x4()
            self.assertEqual(run.started, [])
        finally:
            x4lane.time = saved
            run.give_back_lane()
        self.assertFalse(self.paths.launch_lock.exists())

    def test_cleanup_never_touches_protected_processes(self):
        killed = []
        fake = x4lane.win()
        fake.terminate = lambda pid, exe: killed.append(pid) or True
        lane_exe = str(self.paths.lane('b1').exe)
        self.others = [self.proc(41, lane_exe, '45841'), self.proc(43, self.INSTALL, '45841')]
        x4lane.doctor(self.paths, fix=True)
        self.assertEqual(killed, [])


class AdmissionTests(FakeWindows):
    def procs(self, count):
        import types
        return [types.SimpleNamespace(pid=100 + i, exe=f'C:/x/{i}/X4.exe', cmdline='-personalfolderid 4590',
                                      created=1) for i in range(count)]

    def ram(self, gb):
        x4lane.win().memory_status = lambda: (94 * 2**30, int(gb * 2**30))

    def test_every_x4_counts_towards_the_cap(self):
        cap = core.MAX_X4_TOTAL
        self.ram(core.MIN_FREE_PER_START * 3)
        self.others = self.procs(cap - 1)
        self.assertEqual(x4lane.admission(self.paths, 1, 0, 'test')[0], cap - 1)
        with self.assertRaises(x4lane.CapReached) as refused:
            x4lane.admission(self.paths, 2, 0, 'session')          # a session counts every client
        self.assertIn(f'MAX_X4_TOTAL={cap}', str(refused.exception))
        self.others = self.procs(cap)
        with self.assertRaises(x4lane.CapReached):
            x4lane.admission(self.paths, 1, 0, 'test')

    def test_memory_per_start_and_no_lowering(self):
        need = core.MIN_FREE_PER_START
        self.ram(need + 1)
        x4lane.admission(self.paths, 1, 0, 'test')
        with self.assertRaises(x4lane.CapReached):
            x4lane.admission(self.paths, 2, 0, 'session')          # 2 x MIN_FREE_PER_START
        with self.assertRaises(x4lane.CapReached):
            x4lane.admission(self.paths, 1, need + 4, 'test')     # --min-free-gb may raise it
        self.ram(need - 0.5)
        with self.assertRaises(x4lane.CapReached):
            x4lane.admission(self.paths, 1, 2, 'test')            # ... but never lower it

    def test_pressure_holds_back_starts_until_it_recovers(self):
        path = self.paths.pressure
        low, ok = core.PRESSURE_LOW_GB, core.MIN_FREE_PER_START
        t = 1000.0
        self.assertIsNone(core.observe_pressure(path, ok + 1, 'a', now=t))
        self.assertEqual(core.observe_pressure(path, low - 1, 'a', now=t)['state'], 'low')
        self.assertIsNone(core.observe_pressure(path, low + 1, 'a', now=t + 30))   # short dip: forgotten
        self.assertFalse(path.exists())
        core.observe_pressure(path, low - 1, 'a', now=t + 40)
        self.assertEqual(core.observe_pressure(path, low - 0.5, 'b', now=t + 101)['state'], 'pressure')
        self.assertEqual(core.observe_pressure(path, ok - 1, 'b', now=t + 120)['state'], 'pressure')  # not yet
        self.assertEqual(core.observe_pressure(path, ok + 0.5, 'b', now=t + 130)['recover_since'], t + 130)
        self.assertEqual(core.observe_pressure(path, ok - 1, 'b', now=t + 150)['recover_since'], None)
        core.observe_pressure(path, ok + 1, 'b', now=t + 160)
        self.assertIsNone(core.observe_pressure(path, ok + 1, 'b', now=t + 221))  # 60 s at >= MIN_FREE_PER_START
        self.assertFalse(path.exists())

    def test_admission_refuses_during_pressure(self):
        core.write_json(self.paths.pressure, {'state': 'pressure', 'since': time.time() - 30, 'updated': time.time(),
                                              'low_since': time.time() - 100})
        self.ram(core.MIN_FREE_PER_START + 2)
        with self.assertRaises(x4lane.CapReached) as refused:
            x4lane.admission(self.paths, 1, 0, 'test')
        self.assertIn('memory pressure', str(refused.exception))
        stale = time.time() - core.PRESSURE_STALE_S - 5            # nobody observed it for 10 min
        core.write_json(self.paths.pressure, {'state': 'pressure', 'since': stale, 'updated': stale})
        x4lane.admission(self.paths, 1, 0, 'test')


class ReserveTests(AdmissionTests):
    def protected(self, count):
        import types
        exe = r'C:\Program Files (x86)\Steam\steamapps\common\X4 Foundations\X4.exe'
        return [types.SimpleNamespace(pid=400 + i, exe=exe, cmdline=f'-personalfolderid 4584{1 + i}', created=1)
                for i in range(count)]

    def test_unfilled_reserved_slots_count_as_running(self):
        cap = core.MAX_X4_TOTAL
        self.ram(100)
        self.assertEqual(self.reserve('--slots', '2', '--owner', 'x4mods-61', '--reason', 'rebuild'), 0)
        self.others = self.procs(cap - 3)                       # 3 free slots, 2 of them reserved
        x4lane.admission(self.paths, 1, 0, 'test')
        with self.assertRaises(x4lane.CapReached) as refused:
            x4lane.admission(self.paths, 2, 0, 'session')
        self.assertIn('2 slot(s) reserved for x4mods-61', str(refused.exception))
        self.others = self.procs(cap - 4) + self.protected(2)   # both clients back: nothing unfilled
        x4lane.admission(self.paths, 2, 0, 'session')
        self.assertEqual(self.reserve('--clear'), 0)
        self.assertFalse(self.paths.reserve.exists())

    def reserve(self, *argv):
        # never main(): that resolves the live rewrite-coord folder
        return x4lane.cmd_reserve(x4lane.parser().parse_args(['reserve', *argv]), self.paths)

    def test_ram_floor_counts_unfilled_slots_and_reservations_expire(self):
        need = core.MIN_FREE_PER_START + core.RESERVE_SLOT_GB * 2
        self.others = []
        core.write_json(self.paths.reserve, {'owner': 'x4mods-61', 'slots': 2})
        self.ram(need - 1)
        with self.assertRaises(x4lane.CapReached) as refused:
            x4lane.admission(self.paths, 1, 0, 'test')
        self.assertIn('per unfilled reserved slot', str(refused.exception))
        self.ram(need + 1)
        x4lane.admission(self.paths, 1, 0, 'test')
        core.write_json(self.paths.reserve, {'owner': 'x4mods-61', 'slots': 2, 'until': '2020-01-01T00:00:00+00:00'})
        self.assertIsNone(core.read_reserve(self.paths.reserve))   # expired: ignored
        self.ram(core.MIN_FREE_PER_START + 1)
        x4lane.admission(self.paths, 1, 0, 'test')
        with self.assertRaises(core.UsageError):
            self.reserve('--slots', '99', '--owner', 'x')
        self.assertEqual(json.loads(self.paths.reserve.read_text())['owner'], 'x4mods-61')   # refused: unchanged


class ExitFiveTests(ProtectedTests):
    def test_release_exit_5_next_to_a_protected_client_waits(self):
        self.others = [self.proc(41, self.INSTALL, '45841'), self.proc(60, 'C:/other/save-trim/X4.exe', '45843')]
        self.holders = {60}
        saved = x4lane.x4lock

        def x4lock(paths, *args, timeout=60):
            if args[0] == 'release':
                self.calls.append(tuple(str(a) for a in args))
                return 5, ['error: EGOSOFT_X4_INSTANCE still exists after close (another process holds it)']
            return saved(paths, *args, timeout=timeout)
        x4lane.x4lock = x4lock
        run = self.run_ready()
        with self.assertRaises(x4lane.ProtectedHolder) as waiting:
            run.start_sequence()
        self.assertIn('also held by protected X4 41/45841', str(waiting.exception))
        self.assertEqual(run.started, [])


class LogWatcherTests(Temp):
    def test_until_markers_expect_and_partial_lines(self):
        log = self.tmp / 'x4lan-debug.txt'
        watch = core.LogWatcher(log, until=core.compile_regex(r'WALKPROF', 'u'), until_count=2,
                                fail_on=[core.compile_regex('HOOKS_DISABLED', 'f')],
                                expect=[core.parse_expect('2:FRAMEPROBE frames'), core.parse_expect('READY')])
        watch.poll()                                             # no file yet
        log.write_bytes(b'Logfile started, time x\n[=ERROR=] 0.00 [X4LAN] FRAMEPROBE armed in Move')
        watch.poll()
        self.assertIn('log_started', watch.markers)
        self.assertNotIn('frameprobe_armed', watch.markers)     # partial line not evaluated yet
        with open(log, 'ab') as handle:
            handle.write(b'Worker slot 5\r\n[X4LAN] FRAMEPROBE frames=1\n[X4LAN] WALKPROF frames=1\n')
        watch.poll()
        self.assertIn('frameprobe_armed', watch.markers)
        self.assertFalse(watch.until_matched)
        with open(log, 'ab') as handle:
            handle.write(b'[=ERROR=] [X4LANE] AUTOSTART INGAME gamestart=custom_creative\n'
                         b'[X4LAN] FRAMEPROBE frames=2\n[X4LAN] WALKPROF frames=2\n')
        watch.poll()
        self.assertTrue(watch.until_matched)
        self.assertIn('frames=2', watch.until_line)
        self.assertEqual(watch.errors, 2)
        report = watch.expect_report()
        self.assertEqual([(e['count'], e['ok']) for e in report], [(2, True), (0, False)])
        self.assertFalse(watch.expectations_ok())
        self.assertIsNone(watch.failed_line)
        with open(log, 'ab') as handle:
            handle.write(b'[X4LAN] HOOKS_DISABLED reason=x')
        watch.flush()
        self.assertIn('HOOKS_DISABLED', watch.failed_line)

    def test_recreated_log_is_read_from_start(self):
        log = self.tmp / 'log.txt'
        log.write_text('a long first line that is longer than the second file\n')
        watch = core.LogWatcher(log, until=core.compile_regex('second', 'u'))
        watch.poll()
        log.write_text('second\n')
        watch.poll()
        self.assertTrue(watch.until_matched)

    def test_expect_syntax(self):
        self.assertEqual(core.parse_expect('10:WALK')[0], 10)
        self.assertEqual(core.parse_expect('WALK')[0], 1)
        with self.assertRaises(core.UsageError):
            core.parse_expect('0:x')
        with self.assertRaises(core.UsageError):
            core.parse_expect('(')


class AutostartLuaTests(unittest.TestCase):
    def runtime(self, settings):
        lua = LuaRuntime()
        lua.execute('''
          now=0;gt=0;logs={};newgames={};quits=0;soundOn=true;startmenu=false;player=false;paused=false
          gamestartName='custom_creative'
          function GetCurRealTime() return now end
          function GetCurTime() return gt end
          function DebugError(s) logs[#logs+1]=s end
          function SetScript(event,fn) assert(event=='onUpdate');ui=fn end
          function NewGame(id) newgames[#newgames+1]=id end
          function QuitGame() quits=quits+1 end
          function GetSoundOption() return soundOn end
          function SetSoundOption() soundOn=not soundOn end
          local C={IsPlayerValid=function() return player end,IsStartmenu=function() return startmenu end,
            GetGameStartName=function() return gamestartName end,IsGamePaused=function() return paused end}
          package.loaded.ffi={C=C,cdef=function() end,string=function(s) return s end}
          function count(pattern) local n=0 for _,l in ipairs(logs) do if l:find(pattern,1,true) then n=n+1 end end return n end
        ''')
        lua.execute(settings)
        lua.execute(AUTOSTART.read_text())
        return lua

    MODULE = ('X4LANE_AUTOSTART={enabled=true,lane="b1",run="r1",gamestart="custom_creative",start="module",'
              'menu_fallback_s=60,menu_delay_s=3,menu_deadline_s=300,ready_gametime_s=5,alive_s=10,'
              'quit_after_s=1000,mute=true}')

    def test_inert_when_disabled(self):
        lua = self.runtime('X4LANE_AUTOSTART={enabled=false}')
        self.assertIsNone(lua.eval('ui'))

    def test_ingame_ready_alive_and_mute(self):
        lua = self.runtime(self.MODULE)
        lua.execute('''
          now=1;ui();assert(count('AUTOSTART loaded')==1);assert(soundOn==false);assert(count('sound=off')==1)
          now=2;ui();assert(#newgames==0)                               -- loading: no start menu, no player
          player=true;now=20;gt=100;ui()
          assert(count('INGAME gamestart=custom_creative expected=custom_creative match=true')==1)
          assert(count('ALIVE')==1)
          now=21;gt=104;ui();assert(count('READY')==0)
          now=22;gt=105;ui();assert(count('READY')==1)
          now=25;gt=108;ui();assert(count('ALIVE')==1)
          now=30.5;ui();assert(count('ALIVE')==2);assert(count('INGAME')==1);assert(count('READY')==1)
          assert(soundOn==false);assert(#newgames==0);assert(quits==0)
        ''')

    def test_wrong_gamestart_is_reported(self):
        lua = self.runtime(self.MODULE)
        lua.execute("gamestartName='other';player=true;now=5;ui();assert(count('match=false')==1)")

    def test_module_fallback_starts_once_from_menu(self):
        lua = self.runtime(self.MODULE)
        lua.execute('''
          startmenu=true;now=10;ui();assert(count('MENU')==1)
          now=69;ui();assert(#newgames==0)
          now=71;ui();assert(#newgames==1 and newgames[1]=='custom_creative')
          assert(count('NEWGAME gamestart=custom_creative reason=module_fallback')==1)
          now=200;ui();assert(#newgames==1)
        ''')

    def test_menu_mode_and_deadline(self):
        lua = self.runtime(self.MODULE.replace('start="module"', 'start="menu"'))
        lua.execute('''startmenu=true;now=5;ui();now=7;ui();assert(#newgames==0)
          now=8.5;ui();assert(#newgames==1);assert(count('reason=menu')==1)''')
        late = self.runtime(self.MODULE.replace('start="module"', 'start="menu"'))
        late.execute('''startmenu=true;now=400;ui();now=404;ui();assert(#newgames==0)
          assert(count('SKIP menu deadline')==1);now=500;ui();assert(count('SKIP')==1)''')

    def test_quit_after_safety_net(self):
        lua = self.runtime(self.MODULE)
        lua.execute('''player=true;now=999;ui();assert(quits==0)
          now=1000;ui();assert(quits==1);assert(count('QUIT reason=quit_after')==1)
          now=1001;ui();assert(quits==1)''')

    def test_sound_left_alone_when_requested(self):
        lua = self.runtime(self.MODULE.replace('mute=true', 'mute=false'))
        lua.execute("now=1;ui();assert(soundOn==true);assert(count('sound=off')==0)")


class CommandLineTests(Temp):
    def test_run_arguments(self):
        args = x4lane.parser().parse_args(['run', '--agent', 'walk', '--collect', 'out', '--timeout', '600',
                                           '--overlay', 'a', '--overlay', 'b', '--expect', '5:WALKPROF',
                                           '--until', 'ATTNLOAD done', '--config', 'frameratetarget=144'])
        self.assertEqual(args.lane, 'auto')
        self.assertEqual(args.overlay, ['a', 'b'])
        self.assertEqual(args.seed, 276944566)
        self.assertEqual(args.start, 'module')
        self.assertEqual(args.settle, 3.0)

    def test_make_probe_overlay(self):
        root = self.tmp / 'worktree'
        (root / '.local' / 'engine').mkdir(parents=True)
        (root / '.local' / 'engine' / 'x4lan_fixedtick_64.dll').write_bytes(b'MZ')
        (root / 'dev').mkdir()
        (root / 'dev' / 'fixed_tick_probe.lua').write_text('-- probe\n')
        out = self.tmp / 'overlay-probe'
        self.assertEqual(x4lane.main(['make-overlay', 'probe', '--out', str(out), '--root', str(root),
                                      '--mode', 'profile']), 0)
        self.assertEqual((out / 'native' / 'x4lan_fixedtick_64.dll').read_bytes(), b'MZ')
        self.assertEqual((out / 'ui.xml.files').read_text().split(), ['ui/fixed_tick_probe.lua'])
        self.assertIn("X4LAN_FIXEDTICK_MODE = 'profile'", (out / 'ui' / 'config.lua.append').read_text())
        self.assertEqual(x4lane.main(['make-overlay', 'probe', '--out', str(out), '--root', str(root)]), 1)
        self.assertEqual(x4lane.main(['make-overlay', 'probe', '--out', str(out), '--root', str(root),
                                      '--force']), 0)
        self.assertFalse((out / 'ui' / 'config.lua.append').exists())
        lab = make_lab(self.tmp / 'lane')
        core.apply_overlays(lab, [out], self.tmp / 'lane')
        self.assertIn('ui/fixed_tick_probe.lua', (lab / 'ui.xml').read_text())

    def fake_attnload(self, guard=True):
        attn = self.tmp / 'attnload'
        for rel in ('dev', 'extension/x4_lan_lab/ui', '.local/hooks'):
            (attn / rel).mkdir(parents=True, exist_ok=True)
        (attn / 'dev' / 'install_attention_load_test.py').write_text(
            'def overlay(steps, durations, warmup, profile, extra):\n'
            '    return "\\n".join(["-- BEGIN", f"X4LAN_CONFIG.attention_load_profile = \\"{profile}\\"",'
            ' f"X4LAN_CONFIG.attention_load_steps = {steps}"] + extra + ["-- END"]) + "\\n"\n')
        (attn / 'dev' / 'attention_load_test.lua').write_text('-- driver\n')
        (attn / 'dev' / 'attention_load_test.xml').write_text('<mdscript />\n')
        (attn / '.local' / 'hooks' / 'x4lan_hooks_64.dll').write_bytes(b'MZ')
        line = ' ' + x4lane.GUARD_OLD if guard else " assert(cfg.attention_load_profile=='45841','x')"
        (attn / 'extension' / 'x4_lan_lab' / 'ui' / 'engine_hooks.lua').write_text('local cfg\n' + line + '\n')
        return attn

    def test_make_attnload_overlay(self):
        attn = self.fake_attnload()
        out = self.tmp / 'overlay-attn'
        self.assertEqual(x4lane.main(['make-overlay', 'attnload', '--out', str(out), '--attnload', str(attn),
                                      '--steps', '0,4', '--durations', '30,60',
                                      '--set', 'attention_load_prefer="any"']), 0)
        hooks = (out / 'ui' / 'engine_hooks.lua').read_text()
        self.assertIn("'45850'", hooks)
        self.assertNotIn(x4lane.GUARD_OLD, hooks)
        append = (out / 'ui' / 'config.lua.append').read_text()
        self.assertIn('attention_load_profile = "@X4LANE_PROFILE@"', append)
        self.assertIn('X4LAN_CONFIG.attention_load_prefer = "any"', append)
        self.assertEqual((out / 'ui.xml.files').read_text().split(), ['ui/engine_hooks.lua', 'ui/attention_load_test.lua'])
        self.assertTrue((out / 'md' / 'x4lan_attnload.xml').is_file())
        lab = make_lab(self.tmp / 'lane')
        core.apply_overlays(lab, [out], self.tmp / 'lane', {'@X4LANE_PROFILE@': '45851'})
        self.assertIn('attention_load_profile = "45851"', (lab / 'ui' / 'config.lua').read_text())

    def test_old_attnload_overlays_are_widened_for_the_lane_in_use(self):
        lab = make_lab(self.tmp / 'lane')
        old = (" assert(cfg.attention_load_profile=='45841' or cfg.attention_load_profile=='45842' or "
               "cfg.attention_load_profile=='45850' or cfg.attention_load_profile=='45851',"
               "'unsupported attention load profile')\n")
        (lab / 'ui' / 'engine_hooks.lua').write_text('local cfg\n' + old)
        self.assertTrue(core.widen_attention_guard(lab, '45855', self.tmp / 'lane'))
        text = (lab / 'ui' / 'engine_hooks.lua').read_text()
        self.assertIn("=='45851' or cfg.attention_load_profile=='45855','unsupported attention load profile')", text)
        self.assertFalse(core.widen_attention_guard(lab, '45855', self.tmp / 'lane'))   # already accepted
        self.assertFalse(core.widen_attention_guard(lab, '45850', self.tmp / 'lane'))
        self.assertIn("'45858'", x4lane.GUARD_NEW)                                     # new overlays: all lanes
        (lab / 'ui' / 'engine_hooks.lua').unlink()
        self.assertFalse(core.widen_attention_guard(lab, '45855', self.tmp / 'lane'))

    def test_attnload_overlay_refuses_unknown_guard(self):
        attn = self.fake_attnload(guard=False)
        self.assertEqual(x4lane.main(['make-overlay', 'attnload', '--out', str(self.tmp / 'o'),
                                      '--attnload', str(attn)]), 1)


if __name__ == '__main__':
    unittest.main()
