"""Platform-independent core of the native X4 test lanes (dev/x4lane.py).

Lane game views are linked to the real X4 install with directory junctions and hardlinks;
the install itself is never written. Everything this module writes lives in the lanes
root, the lane profiles (45850, 45851), lock directories in the coordination folder, or
the caller's collect directory. Every delete/write helper re-checks that rule.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import time
import xml.etree.ElementTree as ET
from pathlib import Path

LANE_PROFILES = {'b1': '45850', 'b2': '45851'}
# Profiles that belong to other sessions or to the user; never written by the lanes.
FOREIGN_PROFILES = {'45841', '45842', '45843', '45844', '45852', '70000001', '70000002'}
SOURCE_PROFILE = '45842'          # display/audio/input preferences are copied from here (read only)
STEAM_APP_ID = '392160'
AUTOSTART_ID = 'x4lane_autostart'
LAB_ID = 'x4_lan_lab'
LOCK_STALE_S = 45 * 60            # rewrite-coord/README.md: stale after 45 min without update
LOCK_MAX_HOLD_S = 30 * 60         # lane locks: max hold 30 min
MAX_TIMEOUT_S = 1680              # leaves room for preparation, settle and close inside 30 min
MIN_FREE_RAM_GB = 12.0
CONFIG_FORBIDDEN = ('load', 'module', 'mpar', 'seed', 'usedefaultconfig')
CONFIG_OVERRIDE_FORBIDDEN = CONFIG_FORBIDDEN + ('autosave',)
TEXT_SUFFIXES = {'.lua', '.xml', '.txt', '.json', '.md', '.append', '.files', '.cfg', '.ini'}
OVERLAY_META_PREFIX = 'x4lane-'   # overlay root files with this prefix are documentation only

EXIT = {
    'pass': 0, 'usage': 1, 'refused': 2, 'timeout': 3, 'x4_exited': 4, 'fail_pattern': 5,
    'expect_failed': 6, 'launch_failed': 7, 'error': 8, 'interrupted': 9,
}


class LaneError(RuntimeError):
    """Base class: the message is shown to the caller and stored in result.json."""


class UsageError(LaneError):
    pass


class Refused(LaneError):
    """A precondition failed before any X4 was started (exit 2)."""


class UnsafeError(LaneError):
    """A file operation would leave the lane-owned area (fails closed)."""


class OverlayError(LaneError):
    pass


class LockBusy(LaneError):
    def __init__(self, path, owner):
        super().__init__(f'{Path(path).name} is held: {owner_summary(owner)}')
        self.path = Path(path)
        self.owner = owner


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec='seconds')


# ---------------------------------------------------------------------------------------
# paths


@dataclasses.dataclass(frozen=True)
class Paths:
    x4mods: Path
    lanes: Path
    coord: Path
    profiles: Path
    install: Path
    repo: Path

    @classmethod
    def from_env(cls, repo: Path) -> 'Paths':
        home = Path.home()
        x4mods = Path(os.environ.get('X4LANE_X4MODS', home / 'Documents' / 'X4Mods'))
        return cls(
            x4mods=x4mods,
            lanes=Path(os.environ.get('X4LANE_ROOT', x4mods / 'lanes')),
            coord=Path(os.environ.get('X4LANE_COORD', x4mods / 'rewrite-coord')),
            profiles=Path(os.environ.get('X4LANE_PROFILES', home / 'Documents' / 'Egosoft' / 'X4')),
            install=Path(os.environ.get('X4LANE_INSTALL',
                                        r'C:\Program Files (x86)\Steam\steamapps\common\X4 Foundations')),
            repo=Path(repo),
        )

    @property
    def quiet_lock(self) -> Path:
        return self.coord / 'quiet.lock'

    @property
    def launch_lock(self) -> Path:
        return self.coord / 'lane-launch.lock'

    @property
    def base(self) -> Path:
        return self.lanes / 'base'

    @property
    def base_lab(self) -> Path:
        return self.base / LAB_ID

    @property
    def tools(self) -> Path:
        return self.lanes / 'tools'

    @property
    def x4lock_exe(self) -> Path:
        return self.tools / 'x4lock-target' / 'release' / 'x4lock.exe'

    @property
    def autostart_source(self) -> Path:
        return self.repo / 'dev' / 'lane' / AUTOSTART_ID

    @property
    def workshop(self) -> Path:
        return self.install.parent.parent / 'workshop' / 'content' / STEAM_APP_ID

    def lane(self, name: str) -> 'Lane':
        if name not in LANE_PROFILES:
            raise UsageError(f'unknown lane {name!r} (b1 or b2)')
        return Lane(self, name, LANE_PROFILES[name])


@dataclasses.dataclass(frozen=True)
class Lane:
    paths: Paths
    name: str
    profile: str

    @property
    def dir(self) -> Path:
        return self.paths.lanes / self.name

    @property
    def view(self) -> Path:
        return self.dir / 'game-view'

    @property
    def game(self) -> Path:
        return self.view / 'steamapps' / 'common' / 'X4 Foundations'

    @property
    def exe(self) -> Path:
        return self.game / 'X4.exe'

    @property
    def extensions(self) -> Path:
        return self.game / 'extensions'

    @property
    def lab(self) -> Path:
        return self.extensions / LAB_ID

    @property
    def profile_dir(self) -> Path:
        return self.paths.profiles / self.profile

    @property
    def profile_base(self) -> Path:
        return self.dir / 'profile-base'

    @property
    def lock(self) -> Path:
        return self.paths.coord / f'lane-{self.name}.lock'

    @property
    def state_file(self) -> Path:
        return self.dir / 'state.json'

    @property
    def debug_log(self) -> Path:
        return self.profile_dir / 'x4lan-debug.txt'


# ---------------------------------------------------------------------------------------
# file safety


def is_link(path) -> bool:
    """True for symlinks, directory junctions and any other reparse point."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        return True
    isjunction = getattr(os.path, 'isjunction', None)
    return bool(isjunction and isjunction(path))


def is_inside(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def assert_inside(path, root, what='path'):
    """The resolved path (links followed) must be strictly inside the resolved root."""
    path, root = Path(path), Path(root)
    resolved, base = path.resolve(), root.resolve()
    if resolved == base or not is_inside(resolved, base):
        raise UnsafeError(f'{what} {path} resolves to {resolved}, outside {base}')


def assert_parent_within(path, root, what='path'):
    """The resolved parent of path must be the resolved root or inside it."""
    parent, base = Path(path).parent.resolve(), Path(root).resolve()
    if parent != base and not is_inside(parent, base):
        raise UnsafeError(f'{what} {path} resolves outside {base}')


def check_tree_unlinked(path):
    """Refuse a tree that contains links or hardlinked files (they may point into the install)."""
    path = Path(path)
    if is_link(path):
        raise UnsafeError(f'{path} is a link or junction')
    for dirpath, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        for name in dirnames:
            if is_link(Path(dirpath) / name):
                raise UnsafeError(f'{Path(dirpath) / name} is a link or junction')
        for name in filenames:
            item = Path(dirpath) / name
            if is_link(item):
                raise UnsafeError(f'{item} is a link')
            if os.lstat(item).st_nlink > 1:
                raise UnsafeError(f'{item} is hardlinked (it may be an install file)')


def safe_rmtree(path, root):
    """Delete a lane-owned directory tree; never follows or deletes through links."""
    path = Path(path)
    if not os.path.lexists(path):
        return
    assert_inside(path, root, 'delete target')
    check_tree_unlinked(path)
    shutil.rmtree(path)


def safe_unlink(path, root):
    path = Path(path)
    if not os.path.lexists(path):
        return
    assert_parent_within(path, root, 'delete target')
    if is_link(path):
        raise UnsafeError(f'refusing to delete link {path}')
    if os.lstat(path).st_nlink > 1:
        raise UnsafeError(f'refusing to delete hardlinked file {path}')
    path.unlink()


def safe_write(path, data, root):
    """Atomic write of a lane-owned file. Refuses links, hardlinked files and foreign roots."""
    path = Path(path)
    assert_parent_within(path, root, 'write target')
    if os.path.lexists(path):
        if is_link(path):
            raise UnsafeError(f'refusing to write through link {path}')
        if os.lstat(path).st_nlink > 1:
            raise UnsafeError(f'refusing to write hardlinked file {path}')
    if isinstance(data, str):
        data = data.encode('utf-8')
    tmp = path.with_name(path.name + f'.x4lane-tmp-{os.getpid()}')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def tree_hashes(root) -> dict:
    root = Path(root)
    out = {}
    for item in sorted(root.rglob('*')):
        if item.is_file():
            out[item.relative_to(root).as_posix()] = sha256_file(item)
    return out


def substitute(data: bytes, rel: str, tokens: dict) -> bytes:
    if not tokens or Path(rel).suffix.lower() not in TEXT_SUFFIXES:
        return data
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return data
    for key, value in tokens.items():
        text = text.replace(key, value)
    return text.encode('utf-8')


def copy_tree(src, dst, root, tokens=None):
    """Copy a link-free source tree to a new lane-owned destination (optionally with tokens)."""
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        raise LaneError(f'not a directory: {src}')
    check_tree_unlinked(src)
    if os.path.lexists(dst):
        raise UnsafeError(f'copy destination exists: {dst}')
    assert_parent_within(dst, root, 'copy destination')
    for item in sorted(src.rglob('*')):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(substitute(item.read_bytes(), rel.as_posix(), tokens))
    dst.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------------------
# overlays


UI_CONFIG_RE = re.compile(r'<file\s+name="ui/config\.lua"\s*/>')
UI_FILE_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_./-]*\.lua$')


def insert_ui_files(text: str, files) -> str:
    """Insert <file name=...> entries right after ui/config.lua, in order, without duplicates."""
    wanted = []
    for name in files:
        if not UI_FILE_RE.fullmatch(name) or '..' in name.split('/'):
            raise OverlayError(f'invalid ui.xml file entry {name!r}')
        if f'name="{name}"' not in text and name not in wanted:
            wanted.append(name)
    if not wanted:
        return text
    match = UI_CONFIG_RE.search(text)
    if not match:
        raise OverlayError('ui.xml has no <file name="ui/config.lua" /> entry')
    line_start = text.rfind('\n', 0, match.start()) + 1
    indent = text[line_start:match.start()]
    if indent.strip():
        indent = '    '
    entries = ''.join(f'\n{indent}<file name="{name}" />' for name in wanted)
    return text[:match.end()] + entries + text[match.end():]


def read_list_file(path) -> list:
    out = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            out.append(line)
    return out


def overlay_plan(overlay):
    """Classify the files of one overlay directory: copies, appends, ui.xml entries."""
    overlay = Path(overlay)
    if not overlay.is_dir():
        raise OverlayError(f'overlay is not a directory: {overlay}')
    check_tree_unlinked(overlay)
    copies, appends, ui_files = [], [], []
    for item in sorted(overlay.rglob('*')):
        if not item.is_file():
            continue
        rel = item.relative_to(overlay).as_posix()
        if '/' not in rel and rel.startswith(OVERLAY_META_PREFIX):
            continue
        if rel == 'ui.xml.files':
            ui_files += read_list_file(item)
        elif rel.endswith('.append'):
            appends.append((rel[:-len('.append')], item))
        else:
            copies.append((rel, item))
    return copies, appends, ui_files


def apply_overlays(target, overlays, root, tokens=None, ui_files=(), lua_lines=()):
    """Apply overlays (in order) to a lane x4_lan_lab copy. Returns a manifest dict.

    Plain files replace or add files. ``<path>.append`` files append to ``<path>``.
    ``ui.xml.files`` lists ui/*.lua files to load right after ui/config.lua.
    ``lua_lines`` are appended to ui/config.lua last (after every overlay append).
    """
    target = Path(target)
    assert_inside(target, root, 'overlay target')
    check_tree_unlinked(target)
    manifest = {'copied': [], 'appended': [], 'ui_files': [], 'lua_lines': list(lua_lines)}
    pending_ui = []
    for overlay in overlays:
        copies, appends, entries = overlay_plan(overlay)
        for rel, item in copies:
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            safe_write(dest, substitute(item.read_bytes(), rel, tokens), root)
            manifest['copied'].append(f'{Path(overlay).name}:{rel}')
        for rel, item in appends:
            dest = target / rel
            if not dest.is_file():
                raise OverlayError(f'{item}: nothing to append to ({rel} missing)')
            old = dest.read_bytes()
            add = substitute(item.read_bytes(), rel, tokens)
            sep = b'' if not old or old.endswith(b'\n') else b'\n'
            safe_write(dest, old + sep + add + (b'' if add.endswith(b'\n') else b'\n'), root)
            manifest['appended'].append(f'{Path(overlay).name}:{rel}')
        pending_ui += entries
    pending_ui += list(ui_files)
    if pending_ui:
        ui = target / 'ui.xml'
        text = ui.read_text(encoding='utf-8')
        new = insert_ui_files(text, pending_ui)
        if new != text:
            safe_write(ui, new, root)
        manifest['ui_files'] = pending_ui
    if lua_lines:
        config = target / 'ui' / 'config.lua'
        old = config.read_text(encoding='utf-8')
        sep = '' if old.endswith('\n') else '\n'
        block = '-- x4lane --lua lines (test only)\n' + '\n'.join(lua_lines) + '\n'
        safe_write(config, old + sep + block, root)
    return manifest


LOCAL_HOSTS = {'', '127.0.0.1', 'localhost', '::1'}


def check_offline_config(text: str):
    """The lane x4_lan_lab must stay offline: networking off, no token, loopback address only."""
    problems = []
    if re.search(r'(?<![\w.])enabled\s*=\s*true\b', text) or re.search(r'X4LAN_CONFIG\.enabled\s*=\s*true\b', text):
        problems.append('enabled = true (networking adapter)')
    for token in re.findall(r'\btoken\s*=\s*["\']([^"\']*)["\']', text):
        if token:
            problems.append('non-empty token')
            break
    for host in re.findall(r'\bhost\s*=\s*["\']([^"\']*)["\']', text):
        if host.strip().lower() not in LOCAL_HOSTS:
            problems.append(f'non-loopback host {host!r}')
    if not re.search(r'\benabled\s*=\s*false\b', text):
        problems.append('no enabled = false')
    if problems:
        raise OverlayError('lane x4_lan_lab config is not offline: ' + '; '.join(problems))


def check_base_lab(path):
    path = Path(path)
    for required in ('content.xml', 'ui.xml', 'ui/config.lua', 'md/x4lan.xml'):
        if not (path / required).is_file():
            raise LaneError(f'{path} is not an x4_lan_lab extension (missing {required})')
    check_tree_unlinked(path)
    check_offline_config((path / 'ui' / 'config.lua').read_text(encoding='utf-8'))


def extension_id(path) -> str:
    content = Path(path) / 'content.xml'
    if not content.is_file():
        raise UsageError(f'{path}: an extension needs content.xml')
    ident = ET.parse(content).getroot().get('id') or ''
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', ident):
        raise UsageError(f'{content}: invalid extension id {ident!r}')
    return ident


# ---------------------------------------------------------------------------------------
# profile files


def _set(root, key, value):
    element = root.find(key)
    if element is None:
        element = ET.SubElement(root, key)
    element.text = value


def lane_config_xml(source_text: str, width=1280, height=720) -> str:
    """Derive the lane config.xml from a test profile config (display/audio/input kept)."""
    root = ET.fromstring(source_text.lstrip('\ufeff'))
    if root.tag != 'root':
        raise LaneError('unexpected config.xml root element')
    for key in CONFIG_FORBIDDEN:
        for element in root.findall(key):
            root.remove(element)
    _set(root, 'fullscreen', 'false')
    _set(root, 'borderless', 'false')
    _set(root, 'res_width', str(int(width)))
    _set(root, 'res_height', str(int(height)))
    _set(root, 'autosave', 'false')
    _set(root, 'uisafemode', 'false')
    return xml_text(root)


def check_lane_config(text: str):
    root = ET.fromstring(text.lstrip('\ufeff'))
    if root.tag != 'root':
        raise Refused('lane config.xml: unexpected root element')
    if root.findtext('autosave') != 'false':
        raise Refused('lane config.xml must keep autosave false')
    for key in CONFIG_FORBIDDEN:
        if root.find(key) is not None:
            raise Refused(f'lane config.xml contains a <{key}> override')


def apply_config_overrides(text: str, overrides: dict) -> str:
    if not overrides:
        return text
    root = ET.fromstring(text.lstrip('\ufeff'))
    for key, value in overrides.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key) or key in CONFIG_OVERRIDE_FORBIDDEN:
            raise UsageError(f'--config {key}: not allowed')
        if not re.fullmatch(r'[A-Za-z0-9_.\-]{0,64}', value):
            raise UsageError(f'--config {key}={value}: value must be a simple token')
        _set(root, key, value)
    return xml_text(root)


def lane_content_xml(dlc_ids, extra_ids=()) -> str:
    """Enabled-extensions setup of a lane profile. No Workshop (ws_*) selections are written."""
    root = ET.Element('content')
    seen = set()
    for ident in list(sorted(dlc_ids)) + [LAB_ID, AUTOSTART_ID] + list(extra_ids):
        if ident.startswith('ws_'):
            raise UsageError(f'refusing a Workshop extension selection ({ident})')
        if ident in seen:
            continue
        seen.add(ident)
        ET.SubElement(root, 'extension', id=ident, enabled='true')
    return xml_text(root)


def xml_text(root) -> str:
    if hasattr(ET, 'indent'):
        ET.indent(root, space='  ')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode') + '\n'


def dlc_ids(install) -> list:
    out = []
    ext = Path(install) / 'extensions'
    if ext.is_dir():
        for item in sorted(ext.iterdir()):
            if item.is_dir() and item.name.startswith('ego_dlc_') and (item / 'content.xml').is_file():
                out.append(item.name)
    return out


def autostart_settings(**values) -> str:
    def lua(value):
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, (int, float)):
            return repr(value)
        text = str(value)
        if not re.fullmatch(r'[A-Za-z0-9_.:\-]{0,96}', text):
            raise UsageError(f'invalid autostart setting value {text!r}')
        return f'"{text}"'
    body = ',\n'.join(f'  {key} = {lua(value)}' for key, value in values.items())
    return ('-- Written by dev/x4lane.py for one lane run (test only).\n'
            'X4LANE_AUTOSTART = {\n' + body + ',\n}\n')


PROFILE_RE = re.compile(r'(?:^|\s)-personalfolderid(?:\s+|[:=])"?(\d+)"?', re.IGNORECASE)


def profile_from_cmdline(cmdline) -> str | None:
    match = PROFILE_RE.search(cmdline or '')
    return match.group(1) if match else None


# ---------------------------------------------------------------------------------------
# locks (rewrite-coord/README.md: mkdir is the atomic acquire, owner.txt inside)


def read_owner(path) -> dict:
    owner = {}
    file = Path(path) / 'owner.txt'
    try:
        text = file.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return owner
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep:
            owner[key.strip().lower()] = value.strip()
    return owner


def owner_summary(owner: dict) -> str:
    if not owner:
        return 'no owner.txt'
    keys = ('agent', 'owner', 'purpose', 'reason', 'start', 'since', 'updated', 'run', 'pid')
    return ', '.join(f'{k}={owner[k]}' for k in keys if k in owner) or str(owner)


def owner_text(info: dict) -> str:
    return ''.join(f'{key}: {value}\n' for key, value in info.items())


def lock_age_s(path) -> float:
    path = Path(path)
    stamp = path / 'owner.txt'
    target = stamp if stamp.exists() else path
    try:
        return max(0.0, time.time() - target.stat().st_mtime)
    except OSError:
        return 0.0


def lock_is_stale(path, owner, pid_alive=None) -> str | None:
    """Reason string if a lane/launch lock may be taken over, else None."""
    age = lock_age_s(path)
    if not owner:
        return 'no owner.txt for 120 s' if age > 120 else None
    if age > LOCK_STALE_S:
        return f'not updated for {age / 60:.0f} min'
    if pid_alive and owner.get('machine') == socket.gethostname() and owner.get('pid', '').isdigit():
        if not pid_alive(int(owner['pid']), owner.get('pid_created')):
            return f'owner process {owner["pid"]} has exited'
    return None


class DirLock:
    """An mkdir lock with owner.txt. ``refresh`` updates the timestamp; ``release`` removes it."""

    def __init__(self, path, info):
        self.path = Path(path)
        self.info = dict(info)
        self.held = False

    @classmethod
    def acquire(cls, path, info, pid_alive=None, take_stale=True):
        path = Path(path)
        for attempt in range(2):
            try:
                path.mkdir()
            except FileExistsError:
                owner = read_owner(path)
                reason = lock_is_stale(path, owner, pid_alive) if take_stale else None
                if attempt == 0 and reason:
                    if read_owner(path) == owner:       # unchanged since we judged it
                        _remove_lock_dir(path, f'stale ({reason})')
                        continue
                raise LockBusy(path, owner)
            lock = cls(path, info)
            lock.held = True
            lock.refresh()
            return lock
        raise LockBusy(path, read_owner(path))

    def refresh(self):
        if not self.held:
            return
        self.info['updated'] = now_iso()
        tmp = self.path / f'owner.txt.tmp{os.getpid()}'
        tmp.write_text(owner_text(self.info), encoding='utf-8')
        os.replace(tmp, self.path / 'owner.txt')

    def release(self) -> bool:
        if not self.held:
            return True
        self.held = False
        owner = read_owner(self.path)
        if owner.get('run') != str(self.info.get('run')):
            return False                                  # taken over; leave the new owner alone
        _remove_lock_dir(self.path, 'released')
        return True


def _remove_lock_dir(path, why):
    path = Path(path)
    for item in path.iterdir():
        if item.is_dir() or is_link(item):
            raise UnsafeError(f'lock {path} contains {item.name}; not removing ({why})')
    graveyard = path.with_name(f'{path.name}.removing-{os.getpid()}-{int(time.time() * 1000)}')
    os.replace(path, graveyard)                           # atomic: nobody sees a half-deleted lock
    for item in graveyard.iterdir():
        item.unlink()
    graveyard.rmdir()


# ---------------------------------------------------------------------------------------
# log watching


MARKERS = {
    'log_started': re.compile(r'^Logfile started'),
    'autostart_loaded': re.compile(r'\[X4LANE\] AUTOSTART loaded'),
    'autostart_menu': re.compile(r'\[X4LANE\] AUTOSTART MENU'),
    'autostart_newgame': re.compile(r'\[X4LANE\] AUTOSTART NEWGAME'),
    'game_start': re.compile(r"Starting new game using gamestart '"),
    'autostart_ingame': re.compile(r'\[X4LANE\] AUTOSTART INGAME'),
    'autostart_ready': re.compile(r'\[X4LANE\] AUTOSTART READY'),
    'autostart_skip': re.compile(r'\[X4LANE\] AUTOSTART SKIP'),
    'autostart_quit': re.compile(r'\[X4LANE\] AUTOSTART QUIT'),
    'frameprobe_armed': re.compile(r'\[X4LAN\] FRAMEPROBE armed'),
}


def parse_expect(spec: str):
    """'N:REGEX' (at least N matching lines) or 'REGEX' (at least one)."""
    match = re.match(r'^(\d+):(.*)$', spec, re.S)
    count, pattern = (int(match.group(1)), match.group(2)) if match else (1, spec)
    if count < 1:
        raise UsageError(f'--expect {spec!r}: count must be >= 1')
    try:
        return count, re.compile(pattern)
    except re.error as error:
        raise UsageError(f'--expect {spec!r}: {error}')


def compile_regex(pattern, option):
    try:
        return re.compile(pattern)
    except re.error as error:
        raise UsageError(f'{option} {pattern!r}: {error}')


class LogWatcher:
    """Incremental reader of the X4 debug log: until/fail/expect patterns and lane markers."""

    def __init__(self, path, until=None, until_count=1, fail_on=(), expect=(), clock=time.monotonic):
        self.path = Path(path)
        self.until = until
        self.until_count = until_count
        self.fail_on = list(fail_on)
        self.expect = [[count, regex, 0] for count, regex in expect]
        self.clock = clock
        self.t0 = clock()
        self.offset = 0
        self.partial = b''
        self.lines = 0
        self.errors = 0
        self.until_hits = 0
        self.until_line = None
        self.until_at = None
        self.failed_line = None
        self.failed_regex = None
        self.markers = {}

    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:                         # recreated/truncated
            self.offset, self.partial = 0, b''
        if size == self.offset:
            return
        with open(self.path, 'rb') as handle:
            handle.seek(self.offset)
            data = handle.read(size - self.offset)
        self.offset += len(data)
        chunks = (self.partial + data).split(b'\n')
        self.partial = chunks.pop()
        for raw in chunks:
            self.line(raw.decode('utf-8', errors='replace').rstrip('\r'))

    def flush(self):
        self.poll()
        if self.partial:
            raw, self.partial = self.partial, b''
            self.line(raw.decode('utf-8', errors='replace').rstrip('\r'))

    def line(self, text):
        self.lines += 1
        if '[=ERROR=]' in text:
            self.errors += 1
        elapsed = round(self.clock() - self.t0, 3)
        for name, regex in MARKERS.items():
            if name not in self.markers and regex.search(text):
                self.markers[name] = {'s': elapsed, 'line': text[:300]}
        for entry in self.expect:
            if entry[1].search(text):
                entry[2] += 1
        if self.failed_line is None:
            for regex in self.fail_on:
                if regex.search(text):
                    self.failed_line, self.failed_regex = text[:500], regex.pattern
                    break
        if self.until is not None and self.until_at is None and self.until.search(text):
            self.until_hits += 1
            if self.until_hits >= self.until_count:
                self.until_line, self.until_at = text[:500], elapsed

    @property
    def until_matched(self):
        return self.until_at is not None

    def expect_report(self):
        return [{'regex': regex.pattern, 'min': count, 'count': hits, 'ok': hits >= count}
                for count, regex, hits in self.expect]

    def expectations_ok(self):
        return all(hits >= count for count, _regex, hits in self.expect)


# ---------------------------------------------------------------------------------------
# misc


def run_id(lane: str, agent: str) -> str:
    stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    agent = re.sub(r'[^A-Za-z0-9_-]', '_', agent)[:24]
    return f'{stamp}-{lane}-{agent}-{os.getpid()}'


def check_agent(agent: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', agent or ''):
        raise UsageError('--agent must be 1..32 characters of letters, digits, _ . -')
    return agent


def check_outdir(outdir, paths: Paths, overwrite=False) -> Path:
    outdir = Path(outdir).resolve()
    forbidden = [(paths.install, 'the X4 install'), (paths.profiles, 'the X4 profiles')]
    forbidden += [(paths.lanes / name, f'lane {name}') for name in LANE_PROFILES]
    forbidden += [(paths.base, 'the lane base'), (paths.tools, 'the lane tools')]
    for root, what in forbidden:
        if outdir == Path(root).resolve() or is_inside(outdir, root):
            raise UsageError(f'--collect {outdir} must not be inside {what}')
    if (outdir / 'result.json').exists() and not overwrite:
        raise UsageError(f'{outdir} already holds a result.json (use a new directory or --overwrite)')
    return outdir


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + f'.tmp{os.getpid()}')
    tmp.write_text(json.dumps(data, indent=1, sort_keys=False, default=str) + '\n', encoding='utf-8')
    os.replace(tmp, path)
