import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from dev import check_hook_signatures as checks


def executable(site=b"abc"):
    data = bytearray(1536)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, 0x8664)
    struct.pack_into("<H", data, 0x86, 1)
    struct.pack_into("<I", data, 0x88, 12345)
    struct.pack_into("<H", data, 0x94, 0xF0)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<I", data, optional + 56, 0x3000)
    struct.pack_into("<I", data, optional + 60, 0x400)
    section = optional + 0xF0
    data[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<I", data, section + 8, 0x100)
    struct.pack_into("<I", data, section + 12, 0x1000)
    struct.pack_into("<I", data, section + 16, 0x200)
    struct.pack_into("<I", data, section + 20, 0x400)
    data[0x400:0x400 + len(site)] = site
    return bytes(data)


def manifest(data):
    return {
        "format": 1,
        "target": {"platform": "windows-x64", "game_version": "9.00", "steam_build": 23660954,
                   "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                   "image_size": 0x3000, "timestamp": 12345},
        "sites": [{"name": "site", "rva": 0x1000, "length": 3,
                   "sha256": hashlib.sha256(b"abc").hexdigest()}],
    }


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.data = executable()
        self.manifest = manifest(self.data)

    def write_manifest(self, folder, value):
        path = Path(folder) / "signatures.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_valid_executable_and_header_rva(self):
        self.assertEqual(checks.verify_executable_bytes(self.data, self.manifest), [])
        self.manifest["sites"][0].update(rva=0x80, length=4,
                                         sha256=hashlib.sha256(b"PE\0\0").hexdigest())
        self.assertEqual(checks.verify_executable_bytes(self.data, self.manifest), [])

    def test_full_hash_and_site_mismatch_are_distinct(self):
        changed = bytearray(self.data)
        changed[0x400] = ord("z")
        failures = checks.verify_executable_bytes(bytes(changed), self.manifest)
        self.assertEqual(len(failures), 2)
        self.assertIn("executable SHA-256", failures[0])
        self.assertIn("site: signature mismatch", failures[1])

    def test_truncation_and_malformed_pe_fail_closed(self):
        for value in (self.data[:100], b"not a PE", self.data[:1100]):
            with self.subTest(length=len(value)), self.assertRaises(checks.SignatureError):
                checks.verify_executable_bytes(value, self.manifest)

    def test_unmapped_or_cross_section_ranges_are_rejected(self):
        for rva, length in ((0x2000, 1), (0x11FE, 4), (0x1200, 1)):
            self.manifest["sites"][0].update(rva=rva, length=length)
            with self.subTest(rva=rva):
                failures = checks.verify_executable_bytes(self.data, self.manifest)
                self.assertEqual(len(failures), 1)
                self.assertIn("not file-backed", failures[0])

    def test_pe_identity_mismatch_is_rejected(self):
        for key in ("timestamp", "image_size"):
            value = copy.deepcopy(self.manifest)
            value["target"][key] += 1
            with self.subTest(key=key), self.assertRaises(checks.SignatureError):
                checks.verify_executable_bytes(self.data, value)

    def test_manifest_schema_and_hash_only_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_manifest(folder, self.manifest)
            self.assertEqual(checks.load_manifest(path), self.manifest)
            self.assertEqual(checks.main(["--manifest", str(path), "--manifest-only"]), 0)
            for value in ({}, {**self.manifest, "format": 2}):
                path = self.write_manifest(folder, value)
                with self.subTest(value=value), self.assertRaises(checks.SignatureError):
                    checks.load_manifest(path)

    def test_missing_executable_is_a_clean_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.exe"
            with self.assertRaisesRegex(checks.SignatureError, "cannot read executable"):
                checks.verify_executable(path, self.manifest)


if __name__ == "__main__":
    unittest.main()
