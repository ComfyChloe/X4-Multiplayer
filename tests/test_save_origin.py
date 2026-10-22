import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from x4lan import seed,save_origin

HEADER=b'<savegame><info><game id="X4" seed="42" start="custom_creative" version="900" build="611726" guid="11111111-2222-3333-4444-555555555555"/></info></savegame>'


class SaveOriginTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.path=Path(temp.name)/'snapshot'

    def test_compressed_and_plain_native_origin_agree(self):
        self.path.write_bytes(HEADER);plain=save_origin.read(self.path,'x4-xml')
        self.path.write_bytes(gzip.compress(HEADER));compressed=save_origin.read(self.path,'x4-xml-gzip')
        self.assertEqual(plain,compressed);self.assertEqual(plain['generation_seed'],42)

    def test_wrong_game_invalid_guid_and_out_of_range_seed_rejected(self):
        for raw in (HEADER.replace(b'id="X4"',b'id="X3"'),HEADER.replace(b'seed="42"',b'seed="2147483648"'),
                    HEADER.replace(b'11111111-2222-3333-4444-555555555555',b'not-a-guid')):
            self.path.write_bytes(raw)
            with self.assertRaises(seed.SeedError):save_origin.read(self.path,'x4-xml')

    def test_dtd_and_utf16_entity_headers_rejected(self):
        raw=b'<!DOCTYPE savegame [<!ENTITY x "42">]>'+HEADER.replace(b'seed="42"',b'seed="&x;"')
        for data in (raw,raw.decode().encode('utf-16')):
            self.path.write_bytes(data)
            with self.assertRaises(seed.SeedError):save_origin.read(self.path,'x4-xml')

    def test_bounded_header_and_bad_gzip_rejected(self):
        self.path.write_bytes(b'<savegame>'+b' '*1000+HEADER)
        with patch.object(save_origin,'MAX_HEADER_BYTES',128),self.assertRaises(seed.SeedError):save_origin.read(self.path,'x4-xml')
        self.path.write_bytes(b'not gzip')
        with self.assertRaises(seed.SeedError):save_origin.read(self.path,'x4-xml-gzip')
