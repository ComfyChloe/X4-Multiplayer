"""Stage-5 benchmark matrix for x4mp-sim (per-sector scheduler at scale, synthetic load).

Runs target/release/x4mp-sim-bench over
  - an even spread: 10k/50k/100k/200k entities x 8/50/200 worlds x 1..32 threads,
  - the native shape: worlds and relative sizes of the last frame of a mirror capture (--shape),
  - a low-traffic variant at 200k entities (fewer messages, no migrations),
each pass repeated --passes times, and writes CSV, the bench's markdown tables, machine information
and the X4 processes seen before and after into OUT (default .local/bench/<timestamp>).

Timing is only trustworthy without any X4 running and without other heavy load: run it through
`x4lane hold --agent ecs-core -- python rust/x4mp-sim/bench/matrix.py ...` in the quiet window.

  python rust/x4mp-sim/bench/matrix.py [--shape CAPTURE.x4cap] [--passes 3] [--frames 200]
      [--warmup 20] [--out DIR] [--smoke]
"""
import argparse
import csv
import datetime
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUST = ROOT / 'rust'
BENCH = RUST / 'target' / 'release' / ('x4mp-sim-bench.exe' if os.name == 'nt' else 'x4mp-sim-bench')


def x4_processes():
    """Running X4.exe processes (Windows); [] elsewhere."""
    if os.name != 'nt':
        return []
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq X4.exe', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.lower().startswith('"x4.exe"')]


def machine():
    info = {'platform': platform.platform(), 'python': platform.python_version(),
            'logical_cpus': os.cpu_count()}
    if os.name == 'nt':
        ps = ('Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,'
              'MaxClockSpeed | ConvertTo-Json -Compress')
        try:
            info['cpu'] = json.loads(subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                                                    capture_output=True, text=True, timeout=60).stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            info['cpu'] = f'unavailable: {e}'
    try:
        info['commit'] = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                        capture_output=True, text=True).stdout.strip()
    except OSError:
        info['commit'] = 'unknown'
    return info


def bench(out_dir: Path, name: str, args: list[str]) -> list[dict]:
    csv_path = out_dir / f'{name}.csv'
    cmd = [str(BENCH), *args, '--csv', str(csv_path)]
    print('+', ' '.join(cmd), flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    (out_dir / f'{name}.md').write_text(res.stdout + res.stderr)
    print(res.stdout.splitlines()[-1] if res.stdout else res.stderr, flush=True)
    if res.returncode != 0:
        raise SystemExit(f'{name}: x4mp-sim-bench exited {res.returncode} (determinism or usage failure)')
    with csv_path.open(newline='') as f:
        return [dict(r, run=name) for r in csv.DictReader(f)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--shape', type=Path, help='mirror capture whose last frame gives the native world sizes')
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--frames', type=int, default=200)
    ap.add_argument('--warmup', type=int, default=20)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--smoke', action='store_true', help='tiny matrix to test the runner')
    ap.add_argument('--no-build', action='store_true')
    a = ap.parse_args()

    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    out = a.out or ROOT / '.local' / 'bench' / stamp
    out.mkdir(parents=True, exist_ok=True)
    meta = {'started': datetime.datetime.now().astimezone().isoformat(timespec='seconds'), 'machine': machine(),
            'x4_before': x4_processes(), 'args': vars(a) | {'shape': str(a.shape) if a.shape else None,
                                                            'out': str(out)}}
    if not a.no_build:
        subprocess.run(['cargo', 'build', '--release', '-p', 'x4mp-sim', '--bin', 'x4mp-sim-bench'],
                       cwd=RUST, check=True)

    if a.smoke:
        entities, sectors, threads, frames, warmup = '2000,4000', '8,50', '1,2', '5', '1'
        big = '4000'
    else:
        entities, sectors, threads = '10000,50000,100000,200000', '8,50,200', '1,2,4,8,16,32'
        frames, warmup, big = str(a.frames), str(a.warmup), '200000'
    common = ['--threads', threads, '--frames', frames, '--warmup', warmup]
    rows = []
    for p in range(1, a.passes + 1):
        rows += bench(out, f'even-p{p}', ['--entities', entities, '--sectors', sectors, *common])
        if a.shape:
            rows += bench(out, f'shape-p{p}', ['--entities', entities, '--shape', str(a.shape), *common])
            rows += bench(out, f'shape-lowtraffic-p{p}', ['--entities', big, '--shape', str(a.shape),
                                                          '--msg-per-mille', '1', '--migrate-per-mille', '0', *common])
    meta['x4_after'] = x4_processes()
    meta['finished'] = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    meta['timing_trustworthy'] = not meta['x4_before'] and not meta['x4_after']
    (out / 'meta.json').write_text(json.dumps(meta, indent=2))

    # median frame time per configuration over the passes
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        kind = r['run'].rsplit('-p', 1)[0]
        groups.setdefault((kind, int(r['entities']), int(r['worlds']), int(r['threads'])), []).append(r)
    with (out / 'summary.csv').open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['kind', 'entities', 'worlds', 'threads', 'passes', 'frame_ms_median', 'frame_ms_min',
                    'frame_ms_max', 'parallel_ms', 'barrier_ms', 'speedup_vs_1', 'lpt_bound', 'same_hash'])
        base: dict[tuple, float] = {}
        for key in sorted(groups):
            g = groups[key]
            ms = sorted(float(r['frame_ms_median']) for r in g)
            med = ms[len(ms) // 2]
            if key[3] == 1:
                base[key[:3]] = med
            b = base.get(key[:3])
            w.writerow([*key, len(g), f'{med:.4f}', f'{ms[0]:.4f}', f'{ms[-1]:.4f}',
                        f"{sorted(float(r['parallel_ms']) for r in g)[len(g) // 2]:.4f}",
                        f"{sorted(float(r['barrier_ms']) for r in g)[len(g) // 2]:.4f}",
                        f'{b / med:.2f}' if b else '', g[0]['lpt_bound'],
                        all(r['same_hash'] == 'true' for r in g) and len({r['hash'] for r in g}) == 1])
    print(f"done: {out} timing_trustworthy={meta['timing_trustworthy']}", flush=True)
    if not meta['timing_trustworthy']:
        print('WARNING: X4 was running; timings are functional only', file=sys.stderr)


if __name__ == '__main__':
    main()
