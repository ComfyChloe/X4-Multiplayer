"""Create a local development environment; never modify X4 or OS packages."""
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
if sys.version_info < (3, 10):
    raise SystemExit('Python 3.10 or newer is required.')
environment = ROOT / '.venv'
if not environment.exists():
    venv.EnvBuilder(with_pip=True).create(environment)
python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT/'requirements-dev.txt')], check=True)
subprocess.run([str(python), '-c', 'from lupa.luajit21 import LuaRuntime; print("LuaJIT test runtime ready.")'], check=True)
print('Next:', python, 'dev/check.py')
