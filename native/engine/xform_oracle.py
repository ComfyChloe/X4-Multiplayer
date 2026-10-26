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
                    '-I', str(out), '-I', str(ROOT / 'native/engine'), str(ROOT / 'native/engine/xform_oracle.c'),
                    '-lbcrypt', '-o', str(exe)], check=True)
    return exe


def final_state(res: bytes) -> bytes:
    # result file: magic, u32 frames, per frame u32 n + n*16 bytes, then the final state
    at = 8
    frames = int.from_bytes(res[at:at + 4], 'little')
    at += 4
    for _ in range(frames):
        at += 4 + 16 * int.from_bytes(res[at:at + 4], 'little')
    return res[at:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--zig', required=True, type=Path)
    ap.add_argument('--x4', required=True, type=Path, help='verified copy of the pinned X4.exe (not the install)')
    ap.add_argument('--scenarios', type=int, default=400)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--captures', type=int, default=60, help='scenarios also run through the capture core and the embedded probe')
    ap.add_argument('--switch', type=int, default=60, help='scenarios also run with the Rust build in place of the original')
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
    # Capture core and embedded probe: same final state as the plain run, every capture replays.
    caps = cases / 'captures'
    caps.mkdir()
    for p in sorted(cases.glob('*.scn'))[:args.captures]:
        plain = p.with_suffix('.res').read_bytes()
        for mode in ('capture', 'probe'):
            res = caps / f'{p.stem}-{mode}.res'
            target = str(caps / f'{p.stem}-c') if mode == 'capture' else str(caps / f'{p.stem}-p')
            if mode == 'probe':
                Path(target).mkdir()
            r = subprocess.run([str(exe), mode, str(args.x4), str(p), str(res), target], capture_output=True, text=True)
            if r.returncode != 0:
                print(f'oracle {mode} {p.name} failed ({r.returncode}): {r.stderr.strip()}')
                return 1
            if final_state(res.read_bytes()) != final_state(plain):
                print(f'oracle {mode} {p.name}: final state differs from the plain run')
                return 1
        for f in (caps / f'{p.stem}-p').glob('*.cap'):
            f.rename(caps / f'{p.stem}-p-{f.name}')
    # Shadow: the embedded probe checks every frame with the Rust model (x4mp_transform_ffi.dll).
    subprocess.run(['cargo', 'build', '-q', '--release', '-p', 'x4mp-transform-ffi'], cwd=rust, check=True)
    shadow_env = dict(os.environ, XFORM_ORACLE_SHADOW=str(rust / 'target' / 'release' / 'x4mp_transform_ffi.dll'))
    shadow_frames = 0
    for p in sorted(cases.glob('*.scn'))[:args.captures]:
        target = caps / f'{p.stem}-s'
        target.mkdir()
        res = caps / f'{p.stem}-shadow.res'
        r = subprocess.run([str(exe), 'probe', str(args.x4), str(p), str(res), str(target)], capture_output=True, text=True,
                           env=shadow_env)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith('SHADOW ')), '')
        if r.returncode != 0 or ' bad=0 ' not in line or final_state(res.read_bytes()) != final_state(p.with_suffix('.res').read_bytes()):
            print(f'oracle shadow {p.name} failed ({r.returncode}): {line or r.stderr.strip()}')
            return 1
        shadow_frames += int(line.split('frames=')[1].split()[0])
    print(f'shadow: {shadow_frames} frames checked by the Rust model inside the embedded probe, 0 with differences')
    # Switch: the embedded probe runs the Rust build (x4mp_xform_build) instead of the original,
    # shadow-checks every frame, and the final state must equal the original run's. A refused frame
    # runs the original; the only expected refusal is the synthetic repeated kind. The A/B mode (as
    # in game) alternates the original and the Rust build frame by frame.
    for threads, alternate in ((1, False), (8, False), (4, True)):
        switched = fallbacks = 0
        env = dict(shadow_env, XFORM_ORACLE_SWITCH=str(threads), XFORM_ORACLE_ALTERNATE='1' if alternate else '0')
        for p in sorted(cases.glob('*.scn'))[:args.switch]:
            target = caps / f'{p.stem}-w{threads}{"ab" if alternate else ""}'
            target.mkdir()
            res = caps / f'{p.stem}-switch{threads}{"ab" if alternate else ""}.res'
            r = subprocess.run([str(exe), 'probe', str(args.x4), str(p), str(res), str(target)], capture_output=True, text=True,
                               env=env)
            sw = next((ln for ln in r.stdout.splitlines() if ln.startswith('SWITCH ')), '')
            sh = next((ln for ln in r.stdout.splitlines() if ln.startswith('SHADOW ')), '')
            fb = int(sw.split('fallbacks=')[1].split()[0]) if sw else -1
            if (r.returncode != 0 or ' bad=0 ' not in sh or fb < 0
                    or (fb and not sw.endswith('refusal=specification list with a repeated kind'))
                    or final_state(res.read_bytes()) != final_state(p.with_suffix('.res').read_bytes())):
                print(f'oracle switch {threads}{" A/B" if alternate else ""} {p.name} failed ({r.returncode}): {sw} {sh or r.stderr.strip()}')
                return 1
            switched += int(sw.split(' frames=')[1].split()[0])
            fallbacks += fb
        print(f'switch threads={threads}{" A/B" if alternate else ""}: {switched} frames built by Rust (each shadow-checked, 0 differences), '
              f'{fallbacks} refused (repeated kind), final state identical in {min(args.switch, len(list(cases.glob("*.scn"))))} scenarios')
    env = dict(os.environ, X4MP_XFORM_ORACLE_DIR=str(cases), X4MP_XFORM_CAPTURE_DIR=str(caps))
    r = subprocess.run(['cargo', 'test', '-q', '--release', '-p', 'x4mp-transform', '-p', 'x4mp-transform-ffi', '--test', 'oracle',
                        '--test', 'capture', '--lib', '--', '--nocapture'], cwd=rust, env=env, capture_output=True, text=True)
    lines = [ln for ln in (r.stdout + r.stderr).splitlines()
             if ln.startswith(('oracle:', 'captures:')) or 'panicked' in ln or 'differs' in ln]
    print('\n'.join(lines))
    print('XFORM ORACLE', 'PASS' if r.returncode == 0 else 'FAIL')
    return r.returncode


if __name__ == '__main__':
    sys.exit(main())
