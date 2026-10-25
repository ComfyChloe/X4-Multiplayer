"""Build the Windows engine module used by the internal package builder."""
import argparse
import os
from pathlib import Path
import subprocess
import shutil


def build_module(root: Path, zig: Path | None = None) -> Path:
    root = root.resolve()
    compiler = zig or os.environ.get('X4LAN_ZIG') or shutil.which('zig')
    if not compiler:
        raise RuntimeError('Packaging requires Zig 0.15.2; supply --zig or set X4LAN_ZIG.')
    compiler = str(Path(compiler).resolve())
    if subprocess.check_output([compiler, 'version'], text=True).strip() != '0.15.2':
        raise RuntimeError('The engine module requires Zig 0.15.2.')
    out = root / '.local' / 'engine'
    out.mkdir(parents=True, exist_ok=True)
    module = out / 'x4lan_simtick_64.dll'
    subprocess.run([
        compiler, 'cc', '-target', 'x86_64-windows-gnu',
        '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared',
        str(root / 'native/engine/simtick.c'), '-lbcrypt', '-o', str(module),
    ], check=True)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zig', required=True, type=Path)
    parser.add_argument('--test', action='store_true', help='Build/run synthetic Windows lifecycle tests')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    module = build_module(root, args.zig)
    out = module.parent
    print(module)
    if args.test:
        if os.name != 'nt':
            parser.error('--test executes a Windows binary and requires Windows')
        test = out / 'test_simtick.exe'
        subprocess.run([
            str(args.zig.resolve()), 'cc', '-target', 'x86_64-windows-gnu',
            '-std=c11', '-O1', '-Wall', '-Wextra', '-Werror',
            str(root / 'native/engine/test_simtick.c'), '-lbcrypt', '-o', str(test),
        ], check=True)
        subprocess.run([str(test)], check=True)


if __name__ == '__main__':
    main()
