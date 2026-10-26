"""Install the combat load scenario into a lane game view's extensions folder (test only).

    python dev/combat_scenario/install.py install --extensions <lane view>/extensions --variant battle
    python dev/combat_scenario/install.py status --extensions <dir>
    python dev/combat_scenario/install.py uninstall --extensions <dir>

The target must resolve (junctions and symlinks followed) to a directory named "extensions"
inside the lanes root (default: <X4Mods>/lanes, the agent lanes B1/B2). Anything else, in
particular the Steam installation or another session's game view, is refused. The installed
folder carries INSTALL.json; uninstall removes only a folder with that marker. Never
launches or touches X4 itself.
"""
import argparse
import datetime
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXTENSION_ID = 'x4mp_combat_scenario'
MARKER = 'INSTALL.json'
VARIANT_TARGET = Path('md') / 'x4mp_combat_variant.xml'


class Refused(Exception):
    pass


def default_lanes_root() -> Path:
    return ROOT.parent / 'lanes'


def variants() -> dict[str, Path]:
    return {path.stem: path for path in sorted((HERE / 'variants').glob('*.xml'))}


def checked_target(extensions: Path, lanes_root: Path) -> Path:
    """The resolved extension folder, or Refused when the location is not a lane view."""
    if not extensions.is_dir():
        raise Refused(f'not a directory: {extensions}')
    real = extensions.resolve(strict=True)
    root = lanes_root.resolve(strict=True)
    if real.name.lower() != 'extensions':
        raise Refused(f'target is not an X4 extensions folder: {real}')
    if root != real and root not in real.parents:
        raise Refused(f'target {real} is outside the lanes root {root}')
    return real / EXTENSION_ID


def read_marker(folder: Path):
    try:
        return json.loads((folder / MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def install(extensions: Path, variant: str, lanes_root: Path) -> Path:
    choices = variants()
    if variant not in choices:
        raise Refused(f'unknown variant {variant!r}; choose one of {", ".join(choices)}')
    folder = checked_target(extensions, lanes_root)
    if folder.exists():
        if read_marker(folder) is None:
            raise Refused(f'{folder} exists and was not installed by this tool')
        shutil.rmtree(folder)
    shutil.copytree(HERE / 'extension', folder)
    shutil.copy2(choices[variant], folder / VARIANT_TARGET)
    marker = dict(extension=EXTENSION_ID, variant=variant,
                  installed=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                  source=str(HERE))
    (folder / MARKER).write_text(json.dumps(marker, indent=2) + '\n', encoding='utf-8')
    return folder


def uninstall(extensions: Path, lanes_root: Path) -> bool:
    folder = checked_target(extensions, lanes_root)
    if not folder.exists():
        return False
    if read_marker(folder) is None:
        raise Refused(f'{folder} has no {MARKER}; not removing a folder this tool did not create')
    shutil.rmtree(folder)
    return True


def status(extensions: Path, lanes_root: Path) -> str:
    folder = checked_target(extensions, lanes_root)
    marker = read_marker(folder) if folder.exists() else None
    if not folder.exists():
        return 'not installed'
    if marker is None:
        return f'foreign folder at {folder}'
    return f'installed variant={marker.get("variant")} at {folder}'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('install', 'uninstall', 'status', 'variants'))
    parser.add_argument('--extensions', type=Path, help='the lane game view extensions folder')
    parser.add_argument('--variant', default='battle')
    parser.add_argument('--lanes-root', type=Path, default=default_lanes_root())
    args = parser.parse_args(argv)
    if args.action == 'variants':
        print(' '.join(variants()))
        return 0
    if args.extensions is None:
        parser.error('--extensions is required')
    try:
        if args.action == 'install':
            print(f'installed {args.variant} at {install(args.extensions, args.variant, args.lanes_root)}')
        elif args.action == 'uninstall':
            print('removed' if uninstall(args.extensions, args.lanes_root) else 'not installed')
        else:
            print(status(args.extensions, args.lanes_root))
    except Refused as refusal:
        print(f'REFUSED {refusal}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
