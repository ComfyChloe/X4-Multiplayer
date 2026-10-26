"""Verify build-pinned engine hook signatures against a copied X4.exe."""
import argparse
import hashlib
from pathlib import Path
import sys

import pefile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from native.engine.signatures import EXECUTABLE, SITES


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('exe', type=Path, help='Read-only path to the copied X4.exe')
    args = parser.parse_args()
    path = args.exe.resolve()
    failures = []
    size = path.stat().st_size
    digest = file_digest(path)
    if size != EXECUTABLE['size']:
        failures.append(f'file size: expected {EXECUTABLE["size"]}, got {size}')
    if digest != EXECUTABLE['sha256']:
        failures.append(f'file SHA-256: expected {EXECUTABLE["sha256"]}, got {digest}')
    pe = pefile.PE(str(path), fast_load=True)
    timestamp = pe.FILE_HEADER.TimeDateStamp
    image_size = pe.OPTIONAL_HEADER.SizeOfImage
    if timestamp != EXECUTABLE['timestamp']:
        failures.append(f'PE timestamp: expected {EXECUTABLE["timestamp"]:08x}, got {timestamp:08x}')
    if image_size != EXECUTABLE['image_size']:
        failures.append(f'image size: expected {EXECUTABLE["image_size"]:x}, got {image_size:x}')
    data = path.read_bytes()
    for name, (rva, length, expected) in SITES.items():
        offset = pe.get_offset_from_rva(rva)
        actual = hashlib.sha256(data[offset:offset + length]).hexdigest()
        if actual != expected:
            failures.append(f'{name} @ {rva:x}: expected {expected}, got {actual}')
        else:
            print(f'PASS {name} rva={rva:x} bytes={length}')
    if failures:
        for failure in failures:
            print('FAIL', failure, file=sys.stderr)
        return 1
    print(f'PASS executable and {len(SITES)} hook signatures; no files modified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
