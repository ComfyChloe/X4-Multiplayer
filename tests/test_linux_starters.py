"""Exercise generated desktop starters as processes, without touching X4."""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from distribution import build_packages


class LinuxStarters(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'package with spaces $(not-a-command)'
        self.package.mkdir()
        self.starter = self.package / '01-INSTALL.sh'
        self.starter.write_text(build_packages.linux_starter('setup', True))
        self.starter.chmod(0o755)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin))
        # dirname is needed before the handoff; no actual terminal is visible.
        (self.bin / 'dirname').symlink_to(shutil.which('dirname'))

    def terminal(self, name):
        target = self.bin / name
        target.write_text('#!' + sys.executable + '\nimport json, sys\n'
                          'from pathlib import Path\n'
                          'Path("terminal-args.json").write_text(json.dumps(sys.argv[1:]))\n')
        target.chmod(0o755)

    def run_starter(self, *args):
        return subprocess.run(['/bin/bash', str(self.starter.relative_to(self.root)), *args],
                              cwd=self.root, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=5)

    def test_desktop_handoff_preserves_paths_and_arguments_for_each_terminal(self):
        for terminal, prefix in [('konsole', ['--separate', '-e']),
                                 ('gnome-terminal', ['--title=X4 Multiplayer', '--']),
                                 ('xfce4-terminal', ['--title=X4 Multiplayer', '-x']),
                                 ('xterm', ['-T', 'X4 Multiplayer', '-e'])]:
            with self.subTest(terminal=terminal):
                self.terminal(terminal)
                args = ['--role', 'guest', '--invite', '/tmp/Host invitation $(literal).json']
                result = self.run_starter(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((self.package / 'terminal-args.json').read_text()),
                                 [*prefix, 'bash', str(self.starter), '--x4lan-terminal', *args])
                (self.bin / terminal).unlink()
        self.assertFalse((self.package / 'session.json').exists())

    def test_missing_terminal_fails_with_manual_command_without_configuring(self):
        result = self.run_starter()
        self.assertEqual(result.returncode, 1)
        self.assertIn('No supported terminal', result.stderr)
        self.assertIn('01-INSTALL.sh', result.stderr)
        self.assertFalse((self.package / 'session.json').exists())

    def test_failed_terminal_handoff_cannot_recurse_or_read_invisibly(self):
        self.terminal('konsole')
        result = self.run_starter('--x4lan-terminal')
        self.assertEqual(result.returncode, 1)
        self.assertIn('No interactive terminal', result.stderr)
        self.assertFalse((self.package / 'terminal-args.json').exists())

    def prepare_launcher(self):
        shutil.copy2(build_packages.DIST / 'session_launcher.py', self.package / 'launcher.py')
        shutil.copy2(build_packages.DIST / 'profile_settings.py', self.package / 'profile_settings.py')
        # Terminal/input regression only; do not depend on the user's live game.
        (self.package / 'setup_linux.py').write_text('def ensure_closed(): pass\n')
        (self.package / 'VERSION.txt').write_text(build_packages.version())
        runtime = self.package / 'runtime/bin'
        runtime.mkdir(parents=True)
        (runtime / 'python3').symlink_to(sys.executable)

    def test_actual_install_prompt_is_visible_and_error_waits_for_enter_in_terminal(self):
        import pty
        self.prepare_launcher()
        self.terminal('konsole')  # Must not be called from an existing terminal.
        master, slave = pty.openpty()
        process = subprocess.Popen(['/bin/bash', str(self.starter)], cwd=self.root,
                                   env=self.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        def read_until(needle):
            deadline = time.monotonic() + 5
            while needle not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
            self.assertIn(needle, output)
        try:
            read_until(b'Role: [H]ost or [G]uest?')
            os.write(master, b'invalid\n')
            read_until(b'Press Enter to close...')
            self.assertIsNone(process.poll())
            os.write(master, b'\n')
            self.assertEqual(process.wait(timeout=5), 1)
            self.assertIn(b'Choose H for host or G for guest.', output)
            self.assertNotIn(b'Traceback', output)
            self.assertFalse((self.package / 'session.json').exists())
            self.assertFalse((self.package / 'terminal-args.json').exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            os.close(master)

    def test_direct_launcher_eof_is_an_actionable_error_without_traceback(self):
        self.prepare_launcher()
        result = subprocess.run([sys.executable, '-I', '-B', str(self.package / 'launcher.py'), 'setup'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Interactive input is unavailable', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.package / 'session.json').exists())
