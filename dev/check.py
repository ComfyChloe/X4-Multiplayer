"""Run the development checks from any working directory, without launching X4."""
from pathlib import Path
import platform
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    raise SystemExit('LuaJIT test runtime missing. Run: python3 dev/bootstrap.py')
print('Project:', ROOT)
print('Version:', (ROOT/'VERSION.txt').read_text().strip())
print('Python:', platform.python_version(), 'Platform:', platform.platform())
suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print('Incomplete validation: skipped checks:', len(result.skipped))
raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
