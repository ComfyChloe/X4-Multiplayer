"""Native view-cache capture in a test lane, then offline replay with the model.

    python native/engine/xform_lane.py --zig <zig.exe> --label xform-001 [--count 6] [--first 20]
        [--every 30] [--trace 2] [--shadow N] [--shadow-frames 2000] [--switch THREADS] [--switch-ab 1]
        [--switch-frames 5000] [--timeout 900] [--wait 3600] [--replay-only]

1. builds .local/engine/x4lan_xform_64.dll (native/engine/xform_probe.c, -Wall -Wextra -Werror);
2. assembles the lane overlay .local/lane/overlays/xform-<label> (DLL, ui/xform_probe.lua, ui.xml.files,
   and with --shadow N the Rust model x4mp_transform_ffi.dll, which checks every Nth frame in game;
   with --switch THREADS the same DLL's Rust build replaces BuildCache, by default A/B: alternating with
   the original on plain and on shadow frames, both timed and both shadow-checked);
3. runs ONE lane through the released ../lanes/release/dev/x4lane.py (a new disposable world; rules in
   rewrite-coord/native-lane.md: lane profiles only, quiet.lock respected, <= 20 min), with the
   captures written to .local/lane/runs/<label>-captures;
4. replays every capture with rust/x4mp-transform (examples/replay_capture.rs) and writes
   .local/lane/runs/<label>-replay.txt.
Exit status: the lane runner's on failure, else 0 when every capture replays identically.
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

LANE = ROOT.parent / 'lanes' / 'release' / 'dev' / 'x4lane.py'  # released lane tool, see rewrite-coord/native-lane.md


def build(zig: Path) -> Path:
    out = ROOT / '.local' / 'engine'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'fixedtick_signatures.h').write_text(c_header())
    dll = out / 'x4lan_xform_64.dll'
    subprocess.run([str(zig), 'cc', '-target', 'x86_64-windows-gnu', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-shared', '-I', str(out), '-I', str(ROOT / 'native/engine'), str(ROOT / 'native/engine/xform_probe.c'),
                    '-lbcrypt', '-o', str(dll)], check=True)
    return dll


def build_shadow() -> Path:
    subprocess.run(['cargo', 'build', '-q', '--release', '-p', 'x4mp-transform-ffi'], cwd=ROOT / 'rust', check=True)
    return ROOT / 'rust' / 'target' / 'release' / 'x4mp_transform_ffi.dll'


def overlay(label: str, dll: Path, shadow_dll: Path | None) -> Path:
    # one overlay per run label: a queued run copies its overlay only when it starts
    o = ROOT / '.local' / 'lane' / 'overlays' / f'xform-{label}'
    if o.exists():
        shutil.rmtree(o)
    (o / 'native').mkdir(parents=True)
    (o / 'ui').mkdir()
    shutil.copy2(dll, o / 'native' / 'x4lan_xform_64.dll')
    if shadow_dll:
        shutil.copy2(shadow_dll, o / 'native' / 'x4mp_transform_ffi.dll')
    shutil.copy2(ROOT / 'native/engine/xform_probe.lua', o / 'ui' / 'xform_probe.lua')
    (o / 'ui.xml.files').write_text('ui/xform_probe.lua\n')
    (o / 'x4lane-overlay.txt').write_text('transforms: view-cache capture probe (native/engine/xform_probe.c), '
                                          'patches the call at 0x9fe549 (rewrite-coord/hooks.tsv)\n')
    return o


def replay(captures: Path, report: Path) -> int:
    files = sorted(str(p) for p in captures.glob('*.cap'))
    if not files:
        report.write_text('no captures\n')
        print('XFORM REPLAY no captures')
        return 1
    r = subprocess.run(['cargo', 'run', '-q', '--release', '-p', 'x4mp-transform', '--example', 'replay_capture', '--', *files],
                       cwd=ROOT / 'rust', capture_output=True, text=True)
    report.write_text(r.stdout + r.stderr)
    print(r.stdout.strip())
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--zig', required=True, type=Path)
    ap.add_argument('--label', required=True)
    ap.add_argument('--count', type=int, default=6)
    ap.add_argument('--first', type=float, default=20)
    ap.add_argument('--every', type=float, default=30)
    ap.add_argument('--trace', type=int, default=2)
    ap.add_argument('--shadow', type=int, default=0, help='check every Nth frame with the Rust model (0 = off)')
    ap.add_argument('--shadow-frames', type=int, default=2000)
    ap.add_argument('--switch', type=int, default=0, help='threads of the Rust build that replaces BuildCache (0 = off)')
    ap.add_argument('--switch-ab', type=int, choices=(0, 1), default=1, help='1: alternate with the original (A/B)')
    ap.add_argument('--switch-frames', type=int, default=5000)
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('--wait', type=int, default=0)
    ap.add_argument('--replay-only', action='store_true')
    args = ap.parse_args()
    runs = ROOT / '.local' / 'lane' / 'runs'
    captures = runs / f'{args.label}-captures'
    collect = runs / args.label
    if not args.replay_only:
        if collect.exists() or captures.exists():
            ap.error(f'{collect} or {captures} exists; pick a new --label')
        captures.mkdir(parents=True)
        o = overlay(args.label, build(args.zig), build_shadow() if args.shadow or args.switch else None)
        lua_dir = str(captures).replace('\\', '/')
        cmd = [sys.executable, str(LANE), 'run', '--agent', 'transforms', '--label', args.label, '--lane', 'auto',
               '--overlay', str(o),
               '--lua', f"X4LAN_XFORM_DIR='{lua_dir}'", '--lua', f'X4LAN_XFORM_FIRST={args.first:g}',
               '--lua', f'X4LAN_XFORM_EVERY={args.every:g}', '--lua', f'X4LAN_XFORM_COUNT={args.count}',
               '--lua', f'X4LAN_XFORM_TRACE={args.trace}',
               '--lua', f'X4LAN_XFORM_SHADOW={args.shadow}', '--lua', f'X4LAN_XFORM_SHADOW_FRAMES={args.shadow_frames}',
               '--lua', f'X4LAN_XFORM_SWITCH={args.switch}', '--lua', f'X4LAN_XFORM_SWITCH_AB={args.switch_ab}',
               '--lua', f'X4LAN_XFORM_SWITCH_FRAMES={args.switch_frames}',
               '--until', r'\[X4LAN\] XFORM done', '--timeout', str(args.timeout),
               '--expect', r'\[X4LAN\] XFORM armed', '--fail-on', r'FEATURE xform DISABLED',
               '--collect', str(collect)]
        if args.wait:
            cmd += ['--wait', str(args.wait)]
        print(' '.join(cmd), flush=True)
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print(f'XFORM LANE runner exit {rc}; replaying whatever was captured', flush=True)
            replay(captures, runs / f'{args.label}-replay.txt')
            return rc
    return replay(captures, runs / f'{args.label}-replay.txt')


if __name__ == '__main__':
    sys.exit(main())
