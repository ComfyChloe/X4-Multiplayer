"""Build and run the physics rewrite's offline checks (never loaded into X4).

physics_determinism: Jolt 5.5.0 (the version X4 9.00 links) with X4's PhysicsSettings, stepped by an
X4-style serial executor at several reported concurrencies and by Jolt's thread pool at several
thread counts; compares PhysicsSystem::SaveState bytes and contact callbacks every frame.

Jolt is third-party (MIT) source that is not committed here. It is taken from --jolt, or from
.local/third_party/JoltPhysics-5.5.0, or cloned there (tag v5.5.0, commit pinned below). All
outputs stay under .local/physics/.

  python native/engine/physics_build.py --zig <zig 0.15.2> [--jolt <checkout>] [--quick] [--no-run]
  python native/engine/physics_build.py --msvc [--jolt <checkout>] [--quick]
      MSVC build (VS 2022 Build Tools, vcvars64): same harness plus the C serial barrier of
      physics_jobs.h behind an X4-like job system, i.e. the same ABI as inside X4.
"""
import argparse
import concurrent.futures
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
JOLT_TAG = 'v5.5.0'
JOLT_COMMIT = '23dadd0e603f1b321142d4c74df07fce85064989'
# X4.exe and the Linux build were compiled for baseline x86-64 (no SSE4.1/AVX/FMA in Jolt code:
# no dpps, no VEX); JPH_FLOATING_POINT_EXCEPTIONS_ENABLED is the only feature bit of the Linux
# build's JPH_VERSION_ID (0x400050500). It only masks/unmasks exceptions and does not change results.
FLAGS = ['-target', 'x86_64-windows-gnu', '-mcpu=x86_64', '-std=c++17', '-O2', '-DNDEBUG',
         '-DJPH_FLOATING_POINT_EXCEPTIONS_ENABLED', '-ffp-contract=off', '-fno-exceptions', '-fno-rtti',
         '-Wno-unused-command-line-argument']


def zig_path(value):
    compiler = value or os.environ.get('X4LAN_ZIG') or shutil.which('zig')
    if not compiler:
        raise SystemExit('Zig 0.15.2 required: --zig or X4LAN_ZIG')
    compiler = str(Path(compiler).resolve())
    if subprocess.check_output([compiler, 'version'], text=True).strip() != '0.15.2':
        raise SystemExit('Zig 0.15.2 required')
    return compiler


def jolt_checkout(value):
    path = Path(value).resolve() if value else ROOT / '.local' / 'third_party' / 'JoltPhysics-5.5.0'
    if not (path / 'Jolt' / 'Jolt.h').exists():
        if value:
            raise SystemExit(f'no Jolt checkout at {path}')
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', JOLT_TAG,
                        'https://github.com/jrouwe/JoltPhysics', str(path)], check=True)
    head = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    if head.returncode == 0 and head.stdout.strip() != JOLT_COMMIT:
        raise SystemExit(f'Jolt checkout {path} is {head.stdout.strip()}, expected {JOLT_COMMIT} ({JOLT_TAG})')
    core = (path / 'Jolt' / 'Core' / 'Core.h').read_text(encoding='utf-8', errors='replace')
    if not all(f'#define JPH_VERSION_{k} {v}' in core for k, v in (('MAJOR', 5), ('MINOR', 5), ('PATCH', 0))):
        raise SystemExit('Jolt checkout is not 5.5.0')
    return path


VCVARS = Path('C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC/Auxiliary/Build/vcvars64.bat')
MSVC_FLAGS = ['/nologo', '/std:c++17', '/O2', '/DNDEBUG', '/DJPH_FLOATING_POINT_EXCEPTIONS_ENABLED', '/fp:precise', '/EHsc', '/MD']


def msvc_env():
    vcvars = Path(os.environ.get('X4LAN_VCVARS', str(VCVARS)))
    if not vcvars.exists():
        raise SystemExit(f'MSVC build needs vcvars64.bat ({vcvars}); set X4LAN_VCVARS')
    out = subprocess.run(f'cmd /c ""{vcvars}" >nul 2>&1 && set"', capture_output=True, text=True, shell=True)
    env = dict(line.split('=', 1) for line in out.stdout.splitlines() if '=' in line)
    if 'INCLUDE' not in env:
        raise SystemExit('vcvars64.bat did not provide an MSVC environment')
    path = env.get('PATH') or env.get('Path') or ''
    for tool in ('cl', 'link'):
        found = shutil.which(tool, path=path)
        if not found:
            raise SystemExit(f'{tool}.exe not found in the vcvars64 PATH')
        env['X4LAN_' + tool.upper()] = found
    return env


HARNESS_HEADERS = ('physics_jobs.h', 'physics_pool.h')


def up_to_date(src, obj):
    """Jolt objects depend on their source; the harness's own sources also on its headers."""
    if not obj.exists():
        return False
    newest = src.stat().st_mtime
    if src.parent == ROOT / 'native' / 'engine':
        newest = max([newest] + [(src.parent / h).stat().st_mtime for h in HARNESS_HEADERS])
    return obj.stat().st_mtime >= newest


def msvc_compile(env, jolt, src, obj, c_mode=False):
    if up_to_date(src, obj):
        return obj
    obj.parent.mkdir(parents=True, exist_ok=True)
    flags = ['/nologo', '/O2', '/TC'] if c_mode else MSVC_FLAGS
    r = subprocess.run([env['X4LAN_CL'], *flags, '/c', '/I', str(jolt), str(src), f'/Fo{obj}'], env=env, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit('cl failed for %s:\n%s\n%s' % (src, r.stdout, r.stderr))
    return obj


def build_msvc(jolt, out):
    env = msvc_env()
    objdir = out / 'jolt-msvc-obj'
    sources = sorted((jolt / 'Jolt').rglob('*.cpp'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) - 2)) as pool:
        objs = list(pool.map(lambda s: msvc_compile(env, jolt, s, objdir / s.relative_to(jolt).with_suffix('.obj')), sources))
    here = ROOT / 'native' / 'engine'
    objs.append(msvc_compile(env, jolt, here / 'physics_jobs_msvc.c', out / 'msvc' / 'physics_jobs_msvc.obj', c_mode=True))
    objs.append(msvc_compile(env, jolt, here / 'physics_determinism.cpp', out / 'msvc' / 'physics_determinism.obj'))
    exe = out / 'physics_determinism_msvc.exe'
    r = subprocess.run([env['X4LAN_LINK'], '/nologo', f'/OUT:{exe}', *map(str, objs)], env=env, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit('link failed:\n%s\n%s' % (r.stdout, r.stderr))
    return exe


def compile_one(zig, jolt, src, obj):
    if up_to_date(src, obj):
        return obj
    obj.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([zig, 'c++', *FLAGS, '-I', str(jolt), '-c', str(src), '-o', str(obj)], check=True)
    return obj


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--zig', type=str)
    parser.add_argument('--msvc', action='store_true', help='build with MSVC instead of Zig (adds the C serial barrier configs)')
    parser.add_argument('--jolt', type=str)
    parser.add_argument('--quick', action='store_true', help='fewer frames per scenario')
    parser.add_argument('--repeats', type=int, default=2, help='runs per thread-pool configuration')
    parser.add_argument('--no-run', action='store_true')
    args = parser.parse_args()
    jolt = jolt_checkout(args.jolt)
    out = ROOT / '.local' / 'physics'
    if args.msvc:
        exe = build_msvc(jolt, out)
        print(exe)
        if not args.no_run:
            sys.exit(subprocess.run([str(exe), f'--repeats={args.repeats}'] + (['--quick'] if args.quick else [])).returncode)
        return
    zig = zig_path(args.zig)
    objdir = out / 'jolt-obj'
    sources = sorted((jolt / 'Jolt').rglob('*.cpp'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) - 2)) as pool:
        objs = list(pool.map(lambda s: compile_one(zig, jolt, s, objdir / s.relative_to(jolt).with_suffix('.o')), sources))
    exe = out / 'physics_determinism.exe'
    harness = ROOT / 'native' / 'engine' / 'physics_determinism.cpp'
    subprocess.run([zig, 'c++', *FLAGS, '-I', str(jolt), str(harness), *map(str, objs), '-o', str(exe)], check=True)
    print(exe)
    if args.no_run:
        return
    cmd = [str(exe), f'--repeats={args.repeats}'] + (['--quick'] if args.quick else [])
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()
