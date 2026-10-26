"""Install or restore the attention load test in a task-owned X4 game view.

TEST INSTALLS ONLY. `install` merges the engine hooks (x4lan_hooks_64.dll and
ui/engine_hooks.lua), the driver (ui/attention_load_test.lua), its read-only MD helper
(md/x4lan_attnload.xml), two ui.xml entries and a marked config overlay into an
x4_lan_lab copy that already carries the fixed-tick probe. Every file it touches is
backed up first (or recorded as added) in a backup folder next to `extensions`, and
`restore` puts the previous state back byte for byte and removes the backup.

Test-only adjustments made by the overlay (see reports/ATTENTION-LOAD-TEST.md):
  attention_load_test = true       enables the driver and the hooks' load-test paths
  attention_load_profile = "45842" hooks save-folder guard accepts profile 45842
  engine_hash_enabled = false      no STATEHASH captures inside the measured slot loop
  engine_hash_cadence = 3600       observer callback into Lua at most once a minute
test_profile stays "45841": the fixed-tick probe and the hooks config guard require it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTENSION = (Path.home() / "Documents/X4Mods/X4-Multiplayer-fixed-tick/.local/sandbox-baseline/host/"
                     "game-view/steamapps/common/X4 Foundations/extensions/x4_lan_lab")
DEFAULT_DLL = ROOT / ".local/hooks/x4lan_hooks_64.dll"
BACKUP_NAME = "x4lan-attnload-backup"
MANIFEST = "manifest.json"
BEGIN = "-- BEGIN x4lan attention load test overlay (dev/install_attention_load_test.py; restore removes it)"
END = "-- END x4lan attention load test overlay"
UI_FILES = ("ui/engine_hooks.lua", "ui/attention_load_test.lua")
MANAGED = ("native/x4lan_hooks_64.dll", "ui/engine_hooks.lua", "ui/attention_load_test.lua",
           "md/x4lan_attnload.xml", "ui.xml", "ui/config.lua")


class InstallError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def backup_dir(extension):
    # Outside extensions/ so X4 never loads it as an extension.
    return Path(extension).resolve().parent.parent / BACKUP_NAME


def lua_list(values):
    return "{" + ", ".join(str(v) for v in values) + "}"


def overlay(steps, durations, warmup, profile, extra):
    lines = [BEGIN,
             "X4LAN_CONFIG.engine_hooks = true",
             "X4LAN_CONFIG.attention_load_test = true",
             f'X4LAN_CONFIG.attention_load_profile = "{profile}"',
             "X4LAN_CONFIG.engine_hash_enabled = false",
             "X4LAN_CONFIG.engine_hash_cadence = 3600",
             'X4LAN_CONFIG.engine_hash_run = "attnload"',
             f"X4LAN_CONFIG.attention_load_steps = {lua_list(steps)}",
             f"X4LAN_CONFIG.attention_load_durations = {lua_list(durations)}",
             f"X4LAN_CONFIG.attention_load_warmup = {warmup}"]
    lines += extra
    lines.append(END)
    return "\n".join(lines) + "\n"


def patched_ui_xml(text):
    missing = [name for name in UI_FILES if f'name="{name}"' not in text]
    if not missing:
        return text
    entries = "".join(f'\n    <file name="{name}" />' for name in missing)
    for anchor in (r'<file name="ui/fixed_tick_probe.lua"\s*/>', r'<file name="ui/config.lua"\s*/>'):
        match = re.search(anchor, text)
        if match:
            return text[:match.end()] + entries + text[match.end():]
    raise InstallError("ui.xml has no ui/config.lua entry")


def check_config(text, force):
    if BEGIN in text:
        raise InstallError("config.lua already carries the attention load overlay; restore first")
    if not re.search(r'test_profile\s*=\s*"45841"', text):
        raise InstallError('config.lua must keep test_profile = "45841" (fixed-tick probe and hooks guard)')
    if not force and not re.search(r"\benabled\s*=\s*false", text):
        raise InstallError("config.lua must have enabled = false (no networking during the load test); use --force")


def verify_executable(extension):
    exe = Path(extension).resolve().parent.parent / "X4.exe"
    if not exe.is_file():
        raise InstallError(f"X4.exe not found next to extensions: {exe}")
    spec = importlib.util.spec_from_file_location("check_hook_signatures", ROOT / "dev/check_hook_signatures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.main([str(exe)]) != 0:
        raise InstallError("X4.exe does not match the hooks signatures; the hooks would refuse to arm")


def install(extension, dll, steps, durations, warmup, profile="45842", extra=(), force=False, check_exe=True):
    extension = Path(extension)
    for required in ("ui.xml", "ui/config.lua", "md/x4lan.xml"):
        if not (extension / required).is_file():
            raise InstallError(f"missing {required} in {extension}")
    if not (extension / "ui/fixed_tick_probe.lua").is_file() and not force:
        raise InstallError("ui/fixed_tick_probe.lua missing: no FRAMEPROBE sim cost would be logged; use --force")
    if not Path(dll).is_file():
        raise InstallError(f"hooks DLL not found: {dll} (python native/hooks/build.py --test)")
    if any(n < 0 or n > 8 for n in steps) or len(steps) != len(durations) or not steps:
        raise InstallError("steps must be 0..8 with one duration per step")
    config_text = (extension / "ui/config.lua").read_text(encoding="utf-8")
    check_config(config_text, force)
    ui_text = (extension / "ui.xml").read_text(encoding="utf-8")
    new_ui = patched_ui_xml(ui_text)
    if check_exe:
        verify_executable(extension)
    backup = backup_dir(extension)
    if backup.exists():
        raise InstallError(f"backup already exists ({backup}); run restore first")
    sources = {
        "native/x4lan_hooks_64.dll": Path(dll),
        "ui/engine_hooks.lua": ROOT / "extension/x4_lan_lab/ui/engine_hooks.lua",
        "ui/attention_load_test.lua": ROOT / "dev/attention_load_test.lua",
        "md/x4lan_attnload.xml": ROOT / "dev/attention_load_test.xml",
    }
    backup.mkdir(parents=True)
    record = {"extension": str(extension.resolve()), "files": {}}
    for name in MANAGED:
        target = extension / name
        if target.exists():
            saved = backup / "files" / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            record["files"][name] = {"action": "replaced", "sha256": sha256(target)}
        else:
            record["files"][name] = {"action": "added"}
    (backup / MANIFEST).write_text(json.dumps(record, indent=1), encoding="utf-8")
    for name, source in sources.items():
        target = extension / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    newline = "" if config_text.endswith("\n") else "\n"
    (extension / "ui/config.lua").write_text(
        config_text + newline + overlay(steps, durations, warmup, profile, list(extra)), encoding="utf-8")
    (extension / "ui.xml").write_text(new_ui, encoding="utf-8")
    for name in MANAGED:
        record["files"][name]["installed"] = sha256(extension / name)
    (backup / MANIFEST).write_text(json.dumps(record, indent=1), encoding="utf-8")
    return backup


def restore(extension):
    extension = Path(extension)
    backup = backup_dir(extension)
    manifest = backup / MANIFEST
    if not manifest.is_file():
        raise InstallError(f"no attention load backup at {backup}")
    record = json.loads(manifest.read_text(encoding="utf-8"))
    changed = []
    for name, entry in record["files"].items():
        target = extension / name
        if entry.get("installed") and target.exists() and sha256(target) != entry["installed"]:
            changed.append(name)
        if entry["action"] == "added":
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / "files" / name, target)
            if sha256(target) != entry["sha256"]:
                raise InstallError(f"restored {name} does not match its backup hash; backup kept at {backup}")
    shutil.rmtree(backup)
    return changed


def status(extension):
    backup = backup_dir(extension)
    if not (backup / MANIFEST).is_file():
        return "not installed"
    record = json.loads((backup / MANIFEST).read_text(encoding="utf-8"))
    return "installed: " + ", ".join(f"{n} ({e['action']})" for n, e in record["files"].items())


def numbers(text):
    return [int(v) for v in text.split(",") if v.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("install", "restore", "status"))
    parser.add_argument("--extension", type=Path, default=DEFAULT_EXTENSION,
                        help="the game view's extensions/x4_lan_lab copy")
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--steps", default="0,8", help="proxy counts per step (default 0,8)")
    parser.add_argument("--durations", default="60,180", help="seconds per step (default 60,180)")
    parser.add_argument("--warmup", type=int, default=20, help="seconds after HOOKS_READY before ranking")
    parser.add_argument("--profile", default="45842", choices=("45841", "45842"))
    parser.add_argument("--set", action="append", default=[], metavar="KEY=LUA",
                        help="extra overlay line, e.g. --set attention_load_prefer='\"any\"'")
    parser.add_argument("--force", action="store_true", help="skip the enabled=false and probe checks")
    parser.add_argument("--skip-exe-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            extra = []
            for item in args.set:
                key, _, value = item.partition("=")
                if not re.fullmatch(r"attention_load_\w+", key) or not value:
                    raise InstallError(f"--set accepts attention_load_* keys only: {item}")
                extra.append(f"X4LAN_CONFIG.{key} = {value}")
            backup = install(args.extension, args.dll, numbers(args.steps), numbers(args.durations), args.warmup,
                             args.profile, extra, args.force, not args.skip_exe_check)
            print(f"installed into {args.extension}\nbackup: {backup}")
        elif args.action == "restore":
            changed = restore(args.extension)
            for name in changed:
                print(f"note: {name} had changed since install; previous version restored anyway")
            print(f"restored {args.extension}")
        else:
            print(status(args.extension))
    except InstallError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
