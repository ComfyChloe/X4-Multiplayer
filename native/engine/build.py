"""Build the opt-in Windows engine module; never install it or bundle it."""
import argparse
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zig', required=True, type=Path)
    parser.add_argument('--test', action='store_true', help='Build/run synthetic Windows lifecycle tests')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    out = root / '.local' / 'engine'
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        str(args.zig.resolve()), 'cc', '-target', 'x86_64-windows-gnu',
        '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared',
        str(root / 'native/engine/simtick.c'), '-lbcrypt',
        '-o', str(out / 'x4lan_simtick_64.dll'),
    ], check=True)
    print(out / 'x4lan_simtick_64.dll')
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
