"""Scaling table of combat scenario lane runs (one run per battle size, simprof profile mode).

    python dev/combat_scenario/scaling.py <run dir>... [--json]

Each run directory is an x4lane --collect folder (x4lan-debug.txt, result.json) of a run with the
probe overlay in profile mode. Only the scenario's measurement window counts ("COMBATSCN window
begin" to "window end"): FRAMEPROBE lines for frame rate and the busy time of the MoveWorker frame
(slots 0 to 15), WALKPROF for the sector walk, the simprof windows completely inside it for
inclusive shares of MoveWorker samples, the tick lines for the ships alive. Memory is the X4
process from result.json. Shares times the wall time per frame give ms per frame per system;
"sim" is UniverseFrameUpdate (slot 3), the simulation proper.
Results are functional unless the run held the quiet window (result.json timing_trustworthy).
Never launches X4.
"""
import argparse
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import markers  # noqa: E402

# (column, RVAs whose presence anywhere in a sampled stack counts the sample)
GROUPS = [
    ('sim', ('9fdef0',)),              # UniverseFrameUpdate: the simulation proper (slot 3)
    ('weapons', ('8132d0',)),          # U::Weapon::Move (all weapon classes)
    # U::Bullet::Move tail-jumps into UpdateFlight / the damage-over-time apply, so its own
    # frame is rarely on the stack
    ('bullets', ('d4f090', 'd4f240', 'd528f0')),
    ('raycast', ('bc4d0', 'bc750')),   # zone ray casts, all callers
    ('missiles', ('566d20',)),         # U::Explosive::Move
    ('jolt', ('ba600',)),              # XPhys Scene::Step (Jolt)
    ('ai', ('106d250',)),              # AIDirector::Move (scripts, orders)
    ('md', ('1121f80',)),              # MD scheduler
    ('events', ('99bb50',)),           # ProcessDueEvents (deferred event delivery)
    ('posts', ('99a3f0',)),            # EventSource::PostEvent, all callers
    ('walk', ('784230',)),             # Sector::Move: the per-sector component walk
    ('buildcache', ('3d4a80',)),
    ('ui', ('b3bad0',)),
    ('render', ('121c1f0',)),
]
FIELD = re.compile(r'(\w+)=(-?[0-9.]+)')


def fields(line: str) -> dict:
    return {k: float(v) for k, v in FIELD.findall(line)}


def shares(stacks) -> dict:
    total = sum(stacks.values())
    out = {name: 0 for name, _ in GROUPS}
    for stack, count in stacks.items():
        frames = set(stack.split(';'))
        for name, rvas in GROUPS:
            if any(r in frames for r in rvas):
                out[name] += count
    return {name: (value / total if total else 0.0) for name, value in out.items()}, total


def analyse(run: Path) -> dict:
    lines = (run / 'x4lan-debug.txt').read_text(encoding='utf-8', errors='replace').splitlines()
    parsed = markers.parse_markers(lines)
    info = markers.summary(parsed)
    begin, end = markers.window_lines(parsed)
    if begin is None or end is None:
        raise ValueError(f'{run}: no complete measurement window')
    probe = [fields(l) for l in lines[begin:end] if 'FRAMEPROBE frames=' in l]
    walk = [fields(l) for l in lines[begin:end] if 'WALKPROF frames=' in l]
    ticks = [m.fields for m in parsed if m.kind == 'tick' and begin < m.line < end]
    if len(probe) < 2:
        raise ValueError(f'{run}: no FRAMEPROBE lines in the window (probe overlay in profile mode?)')
    first, last = probe[0], probe[-1]
    wall = last['wallElapsed'] - first['wallElapsed']
    fps = (last['frames'] - first['frames']) / wall if wall > 0 else 0.0
    game_speed = (last['gameElapsed'] - first['gameElapsed']) / wall if wall > 0 else 0.0
    busy_frames = sum(p.get('simFrames', 0) for p in probe[1:])
    busy_ms = sum(p.get('simMeanMs', 0) * p.get('simFrames', 0) for p in probe[1:]) / busy_frames if busy_frames else 0.0
    busy_max = max((p.get('simMaxMs', 0) for p in probe[1:]), default=0.0)
    walk_frames = sum(w.get('frames', 0) for w in walk)
    walk_ms = sum(w.get('walkMs', 0) * w.get('frames', 0) for w in walk) / walk_frames if walk_frames else 0.0
    stacks, windows = markers.combat_profile(lines)
    share, samples = shares(stacks)
    alive = [t.get('a', 0) + t.get('b', 0) for t in ticks]
    spawned = sum(s.get('count', 0) for s in info['spawned'].values())
    result = json.loads((run / 'result.json').read_text(encoding='utf-8')) if (run / 'result.json').is_file() else {}
    memory = (result.get('x4') or {}).get('memory') or {}
    frame_ms = 1000.0 / fps if fps else 0.0
    sim_ms = share['sim'] * frame_ms
    ships = sum(alive) / len(alive) if alive else float(spawned)
    return {
        'run': run.name,
        'variant': (info.get('loaded') or {}).get('variant'),
        'spawned': spawned,
        'spawn_seconds': (info.get('spawn_complete') or {}).get('seconds'),
        'ships_alive_mean': round(ships, 1),
        'window_seconds': info.get('window_seconds'),
        'window_reason': (info.get('window_end') or {}).get('reason'),
        'fps': round(fps, 2),
        'frame_ms': round(frame_ms, 2),
        'game_speed': round(game_speed, 3),
        'busy_ms': round(busy_ms, 2),
        'busy_max_ms': round(busy_max, 1),
        'sim_ms': round(sim_ms, 2),
        'walk_ms': round(walk_ms, 2),
        'sim_us_per_ship': round(1000.0 * sim_ms / ships, 1) if ships else None,
        'profile_windows': len(windows),
        'samples': samples,
        'shares': {k: round(v, 4) for k, v in share.items()},
        'ms_per_frame': {k: round(v * frame_ms, 2) for k, v in share.items()},
        'working_set_gb': memory.get('working_set_gb'),
        'peak_working_set_gb': memory.get('peak_working_set_gb'),
        'peak_commit_gb': memory.get('peak_commit_gb'),
        'timing_trustworthy': result.get('timing_trustworthy'),
    }


def table(rows: list[dict]) -> str:
    cols = ['weapons', 'bullets', 'raycast', 'jolt', 'ai', 'events', 'walk']
    head = ('| variant | ships spawned / alive | fps | frame ms (max) | sim ms/frame | game speed | '
            + ' | '.join(f'{c} %' for c in cols) + ' | sim us/ship | peak working set GB |')
    out = [head, '|' + '---|' * (head.count('|') - 1)]
    for r in rows:
        out.append(
            f"| {r['variant']} | {r['spawned']} / {r['ships_alive_mean']:.0f} | {r['fps']:.1f} | "
            f"{r['frame_ms']:.1f} ({r['busy_max_ms']:.0f}) | {r['sim_ms']:.1f} | {r['game_speed']:.2f} | "
            + ' | '.join(f"{100 * r['shares'][c]:.1f}" for c in cols)
            + f" | {r['sim_us_per_ship']} | {r['peak_working_set_gb']} |")
    return '\n'.join(out) + '\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    rows = []
    for run in args.runs:
        try:
            rows.append(analyse(run))
        except (OSError, ValueError) as error:
            print(f'SKIP {error}', file=sys.stderr)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        sys.stdout.write(table(rows))
    return 0 if rows else 1


if __name__ == '__main__':
    sys.exit(main())
