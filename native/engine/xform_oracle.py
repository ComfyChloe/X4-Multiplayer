"""Offline oracle run for the transforms rewrite (rust/x4mp-transform).

Builds native/engine/xform_oracle.c, generates view-cache scenarios and pose cases with the
Rust generator, runs the ORIGINAL X4 9.00 code (BuildCache, PropagateCacheFlags,
GetLocalTransform, GetTransformRelativeTo) on them from a verified local copy of the pinned
X4.exe, and compares with the Rust models (rust/x4mp-transform/tests/oracle.rs).

    python native/engine/xform_oracle.py --zig <zig.exe> --x4 <copy of X4.exe> [--scenarios 400] [--seed 1]

Everything is written below .local/xform-oracle/. The X4.exe must be a copy outside the game
installation (for example x4-re/bin/X4.exe); the oracle refuses any file that is not the
pinned build. It never starts the game and never touches the installation.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from native.engine.signatures import c_header  # noqa: E402


def build(zig: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'fixedtick_signatures.h').write_text(c_header())
    exe = out / 'xform_oracle.exe'
    subprocess.run([str(zig), 'cc', '-target', 'x86_64-windows-gnu', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-I', str(out), str(ROOT / 'native/engine/xform_oracle.c'), '-lbcrypt', '-o', str(exe)], check=True)
    return exe


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--zig', required=True, type=Path)
    ap.add_argument('--x4', required=True, type=Path, help='verified copy of the pinned X4.exe (not the install)')
    ap.add_argument('--scenarios', type=int, default=400)
    ap.add_argument('--seed', type=int, default=1)
    args = ap.parse_args()
    if os.name != 'nt':
        ap.error('the oracle executes Windows x64 code and requires Windows')
    if 'steamapps' in str(args.x4.resolve()).lower():
        ap.error('use a copy of X4.exe outside the game installation')
    out = ROOT / '.local' / 'xform-oracle'
    exe = build(args.zig, out)
    cases = out / f'cases-{args.seed}'
    if cases.exists():
        shutil.rmtree(cases)
    rust = ROOT / 'rust'
    subprocess.run(['cargo', 'run', '-q', '--release', '-p', 'x4mp-transform', '--example', 'oracle_inputs', '--',
                    str(cases), str(args.scenarios), str(args.seed)], cwd=rust, check=True)
    runs = [('pose', cases / 'poses.pos', cases / 'poses.por')]
    runs += [('cache', p, p.with_suffix('.res')) for p in sorted(cases.glob('*.scn'))]
    for mode, src, dst in runs:
        r = subprocess.run([str(exe), mode, str(args.x4), str(src), str(dst)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f'oracle {mode} {src.name} failed ({r.returncode}): {r.stderr.strip()}')
            return 1
    env = dict(os.environ, X4MP_XFORM_ORACLE_DIR=str(cases))
    r = subprocess.run(['cargo', 'test', '-q', '--release', '-p', 'x4mp-transform', '--test', 'oracle', '--', '--nocapture'],
                       cwd=rust, env=env, capture_output=True, text=True)
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.startswith('oracle:') or 'panicked' in ln or 'differs' in ln]
    print('\n'.join(lines))
    print('XFORM ORACLE', 'PASS' if r.returncode == 0 else 'FAIL')
    return r.returncode


if __name__ == '__main__':
    sys.exit(main())
