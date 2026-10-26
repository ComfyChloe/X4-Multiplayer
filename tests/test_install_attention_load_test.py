"""Attention load installer: backup, merge and byte-exact restore in a temporary game view."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_attention_load_test", ROOT / "dev/install_attention_load_test.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

UI_XML = """<?xml version="1.0" encoding="utf-8"?>\r
<addon name="x4_lan_lab">\r
  <environment type="menus">\r
    <dependency name="ego_detailmonitor" />\r
    <file name="ui/config.lua" />\r
    <file name="ui/fixed_tick_probe.lua" />\r
    <file name="ui/transport.lua" />\r
  </environment>\r
</addon>\r
"""
CONFIG = """-- Internal Test. Private session configuration; do not publish.\r
X4LAN_CONFIG = {\r
    enabled = false,\r
    test_profile = "45841",\r
    port = 45841,\r
}\r
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name) / "X4 Foundations"
        self.ext = base / "extensions/x4_lan_lab"
        for name in ("ui", "md", "native"):
            (self.ext / name).mkdir(parents=True)
        (self.ext / "ui.xml").write_bytes(UI_XML.encode())
        (self.ext / "ui/config.lua").write_bytes(CONFIG.encode())
        (self.ext / "ui/fixed_tick_probe.lua").write_text("-- probe\n")
        (self.ext / "md/x4lan.xml").write_text("<mdscript/>\n")
        (self.ext / "native/x4lan_fixedtick_64.dll").write_bytes(b"fixedtick")
        # A stale hooks adapter from an older run must be backed up, not lost.
        (self.ext / "ui/engine_hooks.lua").write_text("-- old adapter\n")
        self.dll = Path(self.tmp.name) / "x4lan_hooks_64.dll"
        self.dll.write_bytes(b"hooks-dll")
        self.before = {p.relative_to(self.ext).as_posix(): digest(p) for p in self.ext.rglob("*") if p.is_file()}

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, **kwargs):
        args = dict(steps=[0, 8], durations=[60, 180], warmup=20, check_exe=False)
        args.update(kwargs)
        return installer.install(self.ext, self.dll, **args)

    def test_install_merges_and_restore_is_byte_exact(self):
        backup = self.install()
        self.assertEqual(backup, self.ext.parent.parent / "x4lan-attnload-backup")
        self.assertFalse(str(backup).startswith(str(self.ext.parent)))
        self.assertEqual((self.ext / "native/x4lan_hooks_64.dll").read_bytes(), b"hooks-dll")
        self.assertEqual((self.ext / "ui/engine_hooks.lua").read_bytes(),
                         (ROOT / "extension/x4_lan_lab/ui/engine_hooks.lua").read_bytes())
        self.assertEqual((self.ext / "ui/attention_load_test.lua").read_bytes(),
                         (ROOT / "dev/attention_load_test.lua").read_bytes())
        self.assertEqual((self.ext / "md/x4lan_attnload.xml").read_bytes(),
                         (ROOT / "dev/attention_load_test.xml").read_bytes())
        ui = (self.ext / "ui.xml").read_text(encoding="utf-8")
        order = [ui.index(f'name="{n}"') for n in ("ui/config.lua", "ui/fixed_tick_probe.lua",
                                                  "ui/engine_hooks.lua", "ui/attention_load_test.lua",
                                                  "ui/transport.lua")]
        self.assertEqual(order, sorted(order))
        config = (self.ext / "ui/config.lua").read_text(encoding="utf-8")
        self.assertIn('attention_load_profile = "45842"', config)
        self.assertIn("engine_hash_enabled = false", config)
        self.assertIn("attention_load_durations = {60, 180}", config)
        self.assertIn("test_profile", installer.status(self.ext) + config)
        self.assertTrue(installer.status(self.ext).startswith("installed"))
        with self.assertRaises(installer.InstallError):
            self.install()  # second install refused while a backup exists
        self.assertEqual(installer.restore(self.ext), [])
        after = {p.relative_to(self.ext).as_posix(): digest(p) for p in self.ext.rglob("*") if p.is_file()}
        self.assertEqual(after, self.before)
        self.assertFalse(backup.exists())
        self.assertEqual(installer.status(self.ext), "not installed")

    @unittest.skipUnless(LuaRuntime, "LuaJIT unavailable")
    def test_installed_config_evaluates_with_load_test_settings(self):
        self.install(steps=[0, 1, 2, 4, 8], durations=[60, 60, 60, 60, 60], warmup=15,
                     extra=['X4LAN_CONFIG.attention_load_prefer = "any"'])
        lua = LuaRuntime()
        lua.execute((self.ext / "ui/config.lua").read_text(encoding="utf-8"))
        cfg = lua.eval("X4LAN_CONFIG")
        self.assertEqual(cfg.test_profile, "45841")
        self.assertIs(cfg.enabled, False)
        self.assertIs(cfg.engine_hooks, True)
        self.assertIs(cfg.attention_load_test, True)
        self.assertEqual(cfg.attention_load_profile, "45842")
        self.assertEqual(cfg.engine_hash_cadence, 3600)
        self.assertEqual(lua.eval("table.concat(X4LAN_CONFIG.attention_load_steps, ',')"), "0,1,2,4,8")
        self.assertEqual(cfg.attention_load_warmup, 15)
        self.assertEqual(cfg.attention_load_prefer, "any")

    def test_restore_reports_files_changed_after_install(self):
        self.install()
        (self.ext / "ui/config.lua").write_text("-- edited during the run\n")
        self.assertEqual(installer.restore(self.ext), ["ui/config.lua"])
        self.assertEqual((self.ext / "ui/config.lua").read_bytes(), CONFIG.encode())

    def test_preconditions(self):
        (self.ext / "ui/config.lua").write_bytes(CONFIG.replace("enabled = false", "enabled = true").encode())
        with self.assertRaisesRegex(installer.InstallError, "enabled = false"):
            self.install()
        (self.ext / "ui/config.lua").write_bytes(CONFIG.replace('"45841"', '"45842"').encode())
        with self.assertRaisesRegex(installer.InstallError, "test_profile"):
            self.install()
        (self.ext / "ui/config.lua").write_bytes(CONFIG.encode())
        (self.ext / "ui/fixed_tick_probe.lua").unlink()
        with self.assertRaisesRegex(installer.InstallError, "fixed_tick_probe"):
            self.install()
        self.assertFalse((self.ext.parent.parent / "x4lan-attnload-backup").exists())
        with self.assertRaisesRegex(installer.InstallError, "X4.exe not found"):
            self.install(check_exe=True, force=True)
        with self.assertRaisesRegex(installer.InstallError, "0..8"):
            self.install(steps=[0, 9], force=True)

    def test_cli_status_and_set_validation(self):
        self.assertEqual(installer.main(["status", "--extension", str(self.ext)]), 0)
        self.assertEqual(installer.main(["install", "--extension", str(self.ext), "--dll", str(self.dll),
                                         "--skip-exe-check", "--set", "engine_hooks=false"]), 1)
        self.assertEqual(installer.main(["install", "--extension", str(self.ext), "--dll", str(self.dll),
                                         "--skip-exe-check", "--set", "attention_load_census_seconds=30"]), 0)
        self.assertIn("attention_load_census_seconds = 30", (self.ext / "ui/config.lua").read_text())
        self.assertEqual(installer.main(["restore", "--extension", str(self.ext)]), 0)


if __name__ == "__main__":
    unittest.main()
