import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from x4lan import seed, world_start, world_store


def native_save(label, *, generation_seed=42, guid='11111111-2222-3333-4444-555555555555'):
    return (f'<savegame><info><game id="X4" seed="{generation_seed}" start="custom_creative" '
            f'version="900" build="611726" guid="{guid}"/></info><state>{label}</state></savegame>').encode()


class WorldStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.recipe=world_start.create(world_id='a'*32,generation_seed=42,game_build='test',mod_sha256='b'*64,extensions=[])
        self.store=world_store.WorldStore.create(self.root/'world',self.recipe)
        self.source=self.root/'completed.xml';self.source.write_bytes(native_save('one'))

    def commit(self, revision=0):
        return self.store.publish(self.source,expected_revision=revision)

    def test_checkpoint_survives_separate_host_process_reopen(self):
        saved=self.commit()
        code='from x4lan.world_store import WorldStore; import sys; s=WorldStore(sys.argv[1],world_id=sys.argv[2]); print(s.latest()["manifest"]["sha256"])'
        result=subprocess.run([sys.executable,'-c',code,str(self.store.root),self.recipe['world_id']],
                              cwd=Path(__file__).resolve().parents[1],text=True,capture_output=True,check=True)
        self.assertEqual(result.stdout.strip(),saved['manifest']['sha256'])
        self.assertEqual(saved['snapshot'].read_bytes(),self.source.read_bytes())

    def test_missing_session_client_downloads_latest_checkpoint_after_host_restart(self):
        first=self.commit()
        self.source.write_bytes(native_save('two'))
        second=self.commit(1)
        reopened=world_store.WorldStore(self.store.root,world_id=self.recipe['world_id'])
        latest=reopened.latest()
        self.assertEqual(latest['info']['revision'],2)
        self.assertEqual(latest['info']['parent'],first['checkpoint_sha256'])
        self.assertEqual(first['snapshot'].read_bytes(),native_save('one'))
        listener=socket.socket();self.addCleanup(listener.close);listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(3)
        errors=[]
        def send():
            try:
                conn,_=listener.accept()
                with conn:
                    conn.settimeout(3);seed.send_bundle(conn,latest['folder'],'f'*32)
            except Exception as exc: errors.append(exc)
        worker=threading.Thread(target=send,daemon=True);worker.start()
        try:
            with socket.create_connection(listener.getsockname(),timeout=3) as conn:
                received=seed.receive_bundle(conn,self.root/'late-client','f'*32,
                    seed_id=latest['manifest']['seed_id'],**world_start.content(self.recipe))
        finally: worker.join(4)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertEqual(received,second['manifest'])
        self.assertEqual((self.root/'late-client/seed.bin').read_bytes(),self.source.read_bytes())

    def test_stale_writer_cannot_replace_newer_world(self):
        first=self.commit()
        with self.assertRaisesRegex(world_store.WorldStoreError,'stale'):self.commit(0)
        self.assertEqual(self.store.latest()['checkpoint_sha256'],first['checkpoint_sha256'])

    def test_crash_before_head_commit_keeps_previous_head_and_preserves_orphan(self):
        first=self.commit();self.source.write_bytes(native_save('next'))
        with patch.object(world_store.os,'replace',side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):self.commit(1)
        self.assertEqual(self.store.latest()['info']['revision'],1)
        self.assertEqual(self.store.latest()['manifest'],first['manifest'])
        self.assertEqual(len(list((self.store.root/'checkpoints').iterdir())),2)
        self.assertEqual(self.commit(1)['info']['revision'],2)

    def test_corrupt_committed_save_fails_closed_without_falling_back(self):
        self.commit();self.source.write_bytes(native_save('new'));second=self.commit(1)
        second['snapshot'].write_bytes(b'bad')
        with self.assertRaises(seed.SeedError):self.store.latest()
        with self.assertRaises(seed.SeedError):self.commit(2)

    def test_deleted_or_malformed_head_is_not_treated_as_empty_world(self):
        self.commit();head=self.store.root/'HEAD.json';head.unlink()
        with self.assertRaises(seed.SeedError):self.store.latest()
        head.write_text('{bad')
        with self.assertRaises(ValueError):self.store.latest()

    def test_partial_uncommitted_bundle_is_ignored(self):
        partial=self.store.root/'checkpoints/.pending-interrupted';partial.mkdir();(partial/'seed.part').write_bytes(b'partial')
        self.assertIsNone(self.store.latest())
        self.assertEqual(self.commit()['info']['revision'],1)
        self.assertEqual((partial/'seed.part').read_bytes(),b'partial')

    def test_path_traversal_pointer_and_changed_metadata_refused(self):
        self.commit();head=self.store.root/'HEAD.json';original=head.read_bytes()
        head.write_text(json.dumps(dict(checkpoint='../outside',sha256='c'*64)))
        with self.assertRaises(seed.SeedError):self.store.latest()
        head.write_bytes(original);saved=self.store.latest()
        (saved['folder']/'checkpoint.json').write_text('{}')
        with self.assertRaisesRegex(seed.SeedError,'checksum'):self.store.latest()

    def test_wrong_world_existing_directory_and_changed_recipe_refused(self):
        with self.assertRaises(seed.SeedError):world_store.WorldStore(self.store.root,world_id='c'*32)
        with self.assertRaises(FileExistsError):world_store.WorldStore.create(self.store.root,self.recipe)
        (self.store.root/'world.json').write_text(json.dumps(self.recipe | {'generation_seed':43}))
        with self.assertRaisesRegex(seed.SeedError,'changed'):self.commit()

    def test_writer_lock_rejects_second_publisher(self):
        with world_store._lock(self.store.root):
            with self.assertRaisesRegex(seed.SeedError,'writer'):self.commit()
        self.assertIsNone(self.store.latest())

    def test_source_bytes_preserved_and_no_inode_shared(self):
        before=self.source.read_bytes();saved=self.commit()
        self.assertEqual(self.source.read_bytes(),before)
        self.source.write_bytes(b'later engine write')
        self.assertEqual(saved['snapshot'].read_bytes(),before)

    def test_invalid_revision_and_linked_store_refused(self):
        for rev in (True,-1,'0',2**53):
            with self.assertRaises(seed.SeedError):self.store.publish(self.source,expected_revision=rev)
        link=self.root/'link';link.symlink_to(self.store.root,target_is_directory=True)
        with self.assertRaises(seed.SeedError):world_store.WorldStore(link,world_id='a'*32)

    def test_native_seed_or_world_guid_mismatch_does_not_advance_committed_head(self):
        first=self.commit()
        for raw in (native_save('wrong seed',generation_seed=43),
                    native_save('wrong world',guid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')):
            self.source.write_bytes(raw)
            with self.assertRaisesRegex(seed.SeedError,'another world'):self.commit(1)
            self.assertEqual(self.store.latest()['checkpoint_sha256'],first['checkpoint_sha256'])

    def test_incomplete_or_invalid_header_never_becomes_a_committed_checkpoint(self):
        for raw in (b'not xml',b'<savegame><info>',b'<savegame><component><game seed="42"/></component></savegame>'):
            self.source.write_bytes(raw)
            with self.assertRaises(seed.SeedError):self.commit()
            self.assertIsNone(self.store.latest())
