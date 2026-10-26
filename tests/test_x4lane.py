"""Offline tests of the native test lane tool (dev/x4lane.py, dev/lane/). No X4 is started."""
from pathlib import Path
import os
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

    def test_attnload_overlay_refuses_unknown_guard(self):
        attn = self.fake_attnload(guard=False)
        self.assertEqual(x4lane.main(['make-overlay', 'attnload', '--out', str(self.tmp / 'o'),
                                      '--attnload', str(attn)]), 1)


if __name__ == '__main__':
    unittest.main()
