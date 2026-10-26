"""Build the Windows engine module used by the internal package builder."""
import argparse
import os
from pathlib import Path
import subprocess
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from native.engine.signatures import c_header


def compiler_path(zig: Path | None) -> str:
    compiler = zig or os.environ.get('X4LAN_ZIG') or shutil.which('zig')
    if not compiler:
        raise RuntimeError('Packaging requires Zig 0.15.2; supply --zig or set X4LAN_ZIG.')
    compiler = str(Path(compiler).resolve())
    if subprocess.check_output([compiler, 'version'], text=True).strip() != '0.15.2':
        raise RuntimeError('The engine module requires Zig 0.15.2.')
    return compiler


def output_directory(root: Path) -> Path:
    out = root.resolve() / '.local' / 'engine'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'fixedtick_signatures.h').write_text(c_header())
    return out


def build_module(root: Path, zig: Path | None = None) -> Path:
    root = root.resolve()
    compiler = compiler_path(zig)
    out = output_directory(root)
    module = out / 'x4lan_simtick_64.dll'
    subprocess.run([
        compiler, 'cc', '-target', 'x86_64-windows-gnu',
        '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared',
        '-I', str(out), str(root / 'native/engine/simtick.c'), '-lbcrypt', '-o', str(module),
    ], check=True)
    return module


def build_fixedtick_module(root: Path, zig: Path | None = None) -> Path:
    root = root.resolve()
    compiler = compiler_path(zig)
    out = output_directory(root)
    module = out / 'x4lan_fixedtick_64.dll'
    subprocess.run([
        compiler, 'cc', '-target', 'x86_64-windows-gnu',
        '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared',
        '-I', str(out), str(root / 'native/engine/fixedtick.c'), '-lbcrypt', '-o', str(module),
    ], check=True)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zig', required=True, type=Path)
    parser.add_argument('--fixedtick', action='store_true', help='Build the opt-in fixed-tick probe')
    parser.add_argument('--test', action='store_true', help='Build/run synthetic Windows lifecycle tests')
    args = parser.parse_args()
    root = ROOT
    module = build_fixedtick_module(root, args.zig) if args.fixedtick else build_module(root, args.zig)
    out = module.parent
    print(module)
    if args.test:
        if os.name != 'nt':
            parser.error('--test executes a Windows binary and requires Windows')
        # The sampler test (simprof) unwinds against the test executable's own .pdata.
        names = ['fixedtick', 'simprof', 'physics_probe'] if args.fixedtick else ['simtick']
        for name in names:
            test = out / f'test_{name}.exe'
            subprocess.run([
                str(args.zig.resolve()), 'cc', '-target', 'x86_64-windows-gnu',
                '-std=c11', '-O1', '-Wall', '-Wextra', '-Werror', '-I', str(out),
                str(root / f'native/engine/test_{name}.c'), '-lbcrypt', '-o', str(test),
            ], check=True)
            subprocess.run([str(test)], check=True)


if __name__ == '__main__':
    main()
