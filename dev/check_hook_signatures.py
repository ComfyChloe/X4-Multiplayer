"""Verify build-pinned X4 hook signatures offline; never modifies the executable."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "native/hooks/signatures.json"
HEX64 = set("0123456789abcdef")


class SignatureError(ValueError):
    pass


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise SignatureError(f"invalid {name}")
    return value


def _hash(value, name):
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= HEX64:
        raise SignatureError(f"invalid {name}")
    return value


def load_manifest(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SignatureError(f"cannot read manifest: {error}") from error
    if not isinstance(value, dict) or set(value) != {"format", "target", "sites"}:
        raise SignatureError("manifest must contain format, target and sites")
    if value["format"] != 1:
        raise SignatureError("unsupported manifest format")
    target = value["target"]
    required = {"platform", "game_version", "steam_build", "size", "sha256", "image_size", "timestamp"}
    if not isinstance(target, dict) or set(target) != required:
        raise SignatureError("invalid target identity")
    if target["platform"] != "windows-x64" or not isinstance(target["game_version"], str):
        raise SignatureError("unsupported target platform")
    _integer(target["steam_build"], "Steam build", 1, 2**31 - 1)
    _integer(target["size"], "executable size", 1, 2**31 - 1)
    _integer(target["image_size"], "image size", 1, 2**31 - 1)
    _integer(target["timestamp"], "PE timestamp", 0, 2**32 - 1)
    _hash(target["sha256"], "executable SHA-256")
    sites = value["sites"]
    if not isinstance(sites, list) or not sites or len(sites) > 256:
        raise SignatureError("invalid site list")
    names = set()
    for site in sites:
        if not isinstance(site, dict) or set(site) != {"name", "rva", "length", "sha256"}:
            raise SignatureError("invalid hook site")
        name = site["name"]
        if not isinstance(name, str) or not name or len(name) > 96 or name in names:
            raise SignatureError("invalid or duplicate site name")
        names.add(name)
        _integer(site["rva"], f"{name} RVA", 0, 2**31 - 1)
        _integer(site["length"], f"{name} length", 1, 4096)
        _hash(site["sha256"], f"{name} SHA-256")
    return value


def _u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def pe_layout(data, target):
    if len(data) < 64 or data[:2] != b"MZ":
        raise SignatureError("not a PE executable")
    pe = _u32(data, 0x3C)
    if pe > len(data) - 24 or data[pe:pe + 4] != b"PE\0\0":
        raise SignatureError("invalid PE header")
    machine = _u16(data, pe + 4)
    sections = _u16(data, pe + 6)
    timestamp = _u32(data, pe + 8)
    optional_size = _u16(data, pe + 20)
    optional = pe + 24
    if machine != 0x8664 or not 1 <= sections <= 96 or optional_size < 112:
        raise SignatureError("unsupported PE layout")
    if optional + optional_size > len(data) or _u16(data, optional) != 0x20B:
        raise SignatureError("invalid PE64 optional header")
    image_size = _u32(data, optional + 56)
    headers_size = _u32(data, optional + 60)
    if timestamp != target["timestamp"] or image_size != target["image_size"]:
        raise SignatureError("PE identity differs from manifest")
    if headers_size > len(data):
        raise SignatureError("truncated PE headers")
    rows = []
    table = optional + optional_size
    if table + sections * 40 > len(data):
        raise SignatureError("truncated PE section table")
    for index in range(sections):
        row = table + index * 40
        virtual_size = _u32(data, row + 8)
        rva = _u32(data, row + 12)
        raw_size = _u32(data, row + 16)
        raw = _u32(data, row + 20)
        if raw + raw_size > len(data):
            raise SignatureError("truncated PE section")
        rows.append((rva, virtual_size, raw, raw_size))
    return headers_size, rows


def rva_offset(layout, rva, length):
    headers_size, sections = layout
    if rva + length <= headers_size:
        return rva
    for start, virtual_size, raw, raw_size in sections:
        if start <= rva and rva + length <= start + raw_size:
            if rva + length > start + max(virtual_size, raw_size):
                break
            return raw + rva - start
    raise SignatureError(f"RVA 0x{rva:x} length {length} is not file-backed")


def verify_executable_bytes(data, manifest):
    target = manifest["target"]
    failures = []
    if len(data) != target["size"]:
        failures.append(f"executable size: expected {target['size']}, got {len(data)}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != target["sha256"]:
        failures.append(f"executable SHA-256: expected {target['sha256']}, got {digest}")
    layout = pe_layout(data, target)
    for site in manifest["sites"]:
        try:
            offset = rva_offset(layout, site["rva"], site["length"])
            actual = hashlib.sha256(data[offset:offset + site["length"]]).hexdigest()
        except SignatureError as error:
            failures.append(f"{site['name']}: {error}")
            continue
        if actual != site["sha256"]:
            failures.append(f"{site['name']}: signature mismatch at RVA 0x{site['rva']:x}")
    return failures


def verify_executable(path, manifest):
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise SignatureError(f"cannot read executable: {error}") from error
    return verify_executable_bytes(data, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", nargs="?", type=Path,
                        default=ROOT.parent / "x4-re/bin/X4.exe")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--manifest-only", action="store_true",
                        help="validate stored schema/hashes without reading X4 bytes")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.manifest_only:
            print(f"PASS manifest: {len(manifest['sites'])} pinned hook sites; no executable read")
            return 0
        failures = verify_executable(args.executable, manifest)
    except SignatureError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"PASS {args.executable}: exact build and {len(manifest['sites'])} hook signatures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
