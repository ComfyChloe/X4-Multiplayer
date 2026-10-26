"""Build the lane overlay for the in-game joint-step recorder/shadow (test only).

    python dev/combat_scenario/capture_overlay.py --out .local/lane/overlays/combatcap ^
        [--dll rust/target/release/x4mp_combat.dll] [--mode both|capture|shadow|switch] [--capacity 200000] [--verify 1]

The overlay (for `x4lane.py run --overlay`) adds native/x4mp_combat_64.dll, ui/combat_capture.lua,
a ui.xml entry and the X4MP_COMBATCAP setting. Use it together with the combat scenario
extension (`install.py stage`); it arms only inside the scenario's measurement window.
Never touches a game view itself.
"""
import argparse
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_DLL = ROOT / 'rust' / 'target' / 'release' / 'x4mp_combat.dll'
MARKER = 'x4lane-overlay.txt'


def build(out: Path, dll: Path, mode: str, capacity: int, verify: int = 1) -> Path:
    if mode not in ('both', 'capture', 'shadow', 'switch'):
        raise ValueError(f'unknown mode {mode!r}')
    if not 0 < capacity <= 2_000_000:
        raise ValueError('capacity must be 1..2000000')
    if not 0 <= verify <= 1_000_000:
        raise ValueError('verify must be 0..1000000')
    if not dll.is_file():
        raise FileNotFoundError(f'{dll} not built (cargo build -p x4mp-combat --release)')
    if out.exists():
        if not (out / MARKER).is_file():
            raise FileExistsError(f'{out} exists and is not an overlay built by this tool')
        shutil.rmtree(out)
    (out / 'native').mkdir(parents=True)
    (out / 'ui').mkdir()
    shutil.copy2(dll, out / 'native' / 'x4mp_combat_64.dll')
    shutil.copy2(HERE / 'combat_capture.lua', out / 'ui' / 'combat_capture.lua')
    (out / 'ui.xml.files').write_text('ui/combat_capture.lua\n', encoding='utf-8')
    (out / 'ui' / 'config.lua.append').write_text(
        f"X4MP_COMBATCAP = {{mode='{mode}', capacity={capacity}, verify={verify}}} -- combat recorder overlay\n",
        encoding='utf-8')
    (out / MARKER).write_text(
        'combat agent overlay: joint-step recorder/shadow/switch (code patch at 0xe1f840, restored on stop),\n'
        f'mode={mode} capacity={capacity} verify={verify}; output <profile>/x4mp-combatcap.txt\n', encoding='utf-8')
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--dll', type=Path, default=DEFAULT_DLL)
    parser.add_argument('--mode', default='both')
    parser.add_argument('--capacity', type=int, default=200_000)
    parser.add_argument('--verify', type=int, default=1, help='switch mode: verify every N-th call (0 = never)')
    args = parser.parse_args(argv)
    try:
        print(f'overlay {build(args.out, args.dll, args.mode, args.capacity, args.verify)}')
    except (ValueError, FileNotFoundError, FileExistsError) as error:
        print(f'REFUSED {error}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
