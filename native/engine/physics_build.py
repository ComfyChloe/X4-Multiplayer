"""Build and run the physics rewrite's offline checks (never loaded into X4).

physics_determinism: Jolt 5.5.0 (the version X4 9.00 links) with X4's PhysicsSettings, stepped by an
X4-style serial executor at several reported concurrencies and by Jolt's thread pool at several
thread counts; compares PhysicsSystem::SaveState bytes and contact callbacks every frame.

Jolt is third-party (MIT) source that is not committed here. It is taken from --jolt, or from
.local/third_party/JoltPhysics-5.5.0, or cloned there (tag v5.5.0, commit pinned below). All
outputs stay under .local/physics/.

  python native/engine/physics_build.py --zig <zig 0.15.2> [--jolt <checkout>] [--quick] [--no-run]
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


def compile_one(zig, jolt, src, obj):
    if obj.exists() and obj.stat().st_mtime >= src.stat().st_mtime:
        return obj
    obj.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([zig, 'c++', *FLAGS, '-I', str(jolt), '-c', str(src), '-o', str(obj)], check=True)
    return obj


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--zig', type=str)
    parser.add_argument('--jolt', type=str)
    parser.add_argument('--quick', action='store_true', help='fewer frames per scenario')
    parser.add_argument('--repeats', type=int, default=2, help='runs per thread-pool configuration')
    parser.add_argument('--no-run', action='store_true')
    args = parser.parse_args()
    zig = zig_path(args.zig)
    jolt = jolt_checkout(args.jolt)
    out = ROOT / '.local' / 'physics'
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
