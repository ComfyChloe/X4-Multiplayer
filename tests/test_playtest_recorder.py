"""Continuous playtest recorder (distribution/playtest_recorder.py): read-only, redacting, resilient."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('playtest_recorder', ROOT / 'distribution/playtest_recorder.py')
rec = importlib.util.module_from_spec(spec); spec.loader.exec_module(rec)
TOKEN, BUBBLE, LEASE = 'ab' * 16, 'cd' * 16, '0123456789abcdef' * 2
SEED = 'seed=' + 'ef' * 16


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pkg, self.profile = base / 'pkg', base / 'profile'
        (self.pkg / 'logs').mkdir(parents=True); self.profile.mkdir()
        (self.pkg / 'package.json').write_text(json.dumps(dict(slot='p2', version='7.5.0', steam_build='23660954')))
        (self.pkg / 'session.json').write_text(json.dumps(dict(token=TOKEN, bubble=BUBBLE, server='10.1.2.3')))
        (self.pkg / 'installation.json').write_text(json.dumps(dict(profile=str(self.profile), game=str(base / 'game'))))
        self.log = self.profile / 'x4lan-debug.txt'
        self.clock = Clock()

    def recorder(self, **kw):
        r = rec.Recorder(self.pkg, out=Path(self.tmp.name) / 'runs', clock=self.clock, **kw)
        self.addCleanup(lambda: r.events.closed or r.events.close())   # Windows cannot delete open files
        return r

    def events(self, r):
        return [json.loads(line) for line in (r.out / 'events.jsonl').read_text().splitlines()]

    def text(self, r, name):
        return (r.out / 'sources' / name).read_text()

    def test_copies_complete_lines_and_survives_truncation_and_replacement(self):
        self.log.write_bytes(b'Logfile started, time A\nline 1\npart')
        r = self.recorder(); r.poll()
        self.assertEqual(self.text(r, 'engine.1.log'), 'Logfile started, time A\nline 1\n')
        with open(self.log, 'ab') as f: f.write(b'ial\nline 2\n')
        r.poll()
        self.assertEqual(self.text(r, 'engine.1.log'), 'Logfile started, time A\nline 1\npartial\nline 2\n')
        self.log.write_bytes(b'Logfile started, time B\n')                     # X4 restart: shorter
        r.poll()
        self.log.write_bytes(b'Logfile started, time C\n' + b'x' * 200 + b'\n')  # restart: longer
        r.poll()
        self.assertEqual(self.text(r, 'engine.2.log'), 'Logfile started, time B\n')
        self.assertTrue(self.text(r, 'engine.3.log').startswith('Logfile started, time C\n'))
        kinds = [e['kind'] for e in self.events(r)]
        self.assertEqual((kinds.count('truncated'), kinds.count('replaced')), (1, 1))
        out = r.close('test')
        manifest = json.loads((out / 'manifest.json').read_text())
        self.assertEqual(manifest['role'], 'guest')
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)

    def test_session_key_never_reaches_disk_and_ids_are_pseudonymous(self):
        self.log.write_text(f'k={TOKEN} b={BUBBLE.upper()} lease={LEASE} again={LEASE} {SEED} sha={"aa" * 32}\n')
        r = self.recorder(); r.poll(); out = r.close()
        line = self.text(r, 'engine.1.log')
        self.assertIn('k=[SESSION KEY] b=', line)
        self.assertNotIn(LEASE, line); self.assertIn(SEED, line); self.assertIn('aa' * 32, line)
        ids = [part.split('=')[1] for part in line.split() if part.startswith(('lease=', 'again='))]
        self.assertEqual(ids[0], ids[1]); self.assertTrue(ids[0].startswith('<id:'))
        for path in out.rglob('*'):
            if path.is_file():
                data = path.read_bytes().lower()
                self.assertNotIn(TOKEN.encode(), data, path); self.assertNotIn(BUBBLE.encode(), data, path)

    def test_missing_sources_and_new_relay_streams_are_events(self):
        r = self.recorder(); r.poll()
        self.log.write_text('late\n'); r.poll()
        self.log.unlink(); r.poll(); r.poll()
        (self.pkg / 'logs/server-20261025-220000-000001.jsonl').write_text('{"running": true}\n')
        r.poll()
        kinds = [(e['kind'], e.get('source')) for e in self.events(r)]
        self.assertEqual(kinds.count(('missing', 'engine')), 2)                 # once per absence
        self.assertIn(('present', 'engine'), kinds)
        self.assertIn(('present', 'relay-20261025-220000-000001'), kinds)
        self.assertEqual(self.text(r, 'relay-20261025-220000-000001.1.log'), '{"running": true}\n')

    def test_server_status_transitions_and_launcher_capture_snapshots(self):
        status = self.pkg / 'logs/server-status.json'
        r = self.recorder(); r.poll()
        status.write_text(json.dumps(dict(running=True))); r.poll(); r.poll()
        status.write_text(json.dumps(dict(running=False))); r.poll()
        capture = self.pkg / 'world/capture'; capture.mkdir(parents=True)
        (capture / 'previous.log').write_text(f'old session {TOKEN}\n'); r.poll(); r.poll()
        events = self.events(r)
        self.assertEqual([e['running'] for e in events if e['kind'] == 'server_status'], [False, True, False])
        snaps = [e for e in events if e['kind'] == 'snapshot']
        self.assertEqual([s['path'] for s in snaps], ['world/capture/previous.log'])
        copied = list((r.out / 'snapshots/world/capture').iterdir())
        self.assertEqual(copied[0].read_text(), 'old session [SESSION KEY]\n')

    def test_storage_limit_stops_copying_but_keeps_events(self):
        r = self.recorder(max_mb=0)
        self.log.write_text('x' * 100 + '\n'); r.poll(); r.poll()
        self.assertFalse((r.out / 'sources/engine.1.log').exists())
        self.assertEqual([e['kind'] for e in self.events(r)].count('storage_limit'), 1)
        self.clock.now += rec.HEARTBEAT; r.poll()
        self.assertEqual(self.events(r)[-1]['kind'], 'heartbeat')

    def test_sources_are_never_modified_and_runs_never_share_a_folder(self):
        self.log.write_text('a\n'); (self.pkg / 'logs/server-1.jsonl').write_text('{}\n')
        watched = [self.log, self.pkg / 'logs/server-1.jsonl', self.pkg / 'session.json', self.pkg / 'installation.json']
        before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in watched]
        a, b = self.recorder(), self.recorder()
        for r in (a, b): r.poll(); r.close()
        self.assertNotEqual(a.out, b.out)
        self.assertEqual([(p.stat().st_mtime_ns, p.read_bytes()) for p in watched], before)
        self.assertEqual(sorted(os.listdir(self.profile)), ['x4lan-debug.txt'])

    def test_export_masks_network_and_user_names_and_refuses_overwrite(self):
        self.log.write_text('peer 192.0.2.109 local 127.0.0.1 file C:\\Users\\someone\\Documents\\x /home/other/x\n')
        r = self.recorder(); r.poll(); out = r.close()
        target = rec.export(out)
        with zipfile.ZipFile(target) as z:
            text = z.read(out.name + '/sources/engine.1.log').decode()
            self.assertIn('manifest.json', ' '.join(z.namelist()))
        self.assertIn('peer <ipv4> local 127.0.0.1', text)
        self.assertIn('C:\\Users\\<user>\\Documents', text); self.assertIn('/home/<user>/x', text)
        with self.assertRaises(FileExistsError): rec.export(out)

    def test_unconfigured_package_still_records_relay_streams(self):
        (self.pkg / 'installation.json').unlink(); (self.pkg / 'session.json').unlink()
        (self.pkg / 'logs/server-2.jsonl').write_text('{}\n')
        r = self.recorder(); r.poll(); r.close()
        kinds = [e['kind'] for e in self.events(r)]
        self.assertIn('no_profile', kinds); self.assertTrue((r.out / 'sources/relay-2.1.log').is_file())

    def test_atomic_equal_size_replacement_with_same_header_starts_a_new_segment(self):
        head = b'H' * rec.HEADER
        self.log.write_bytes(head + b'old\n')
        r = self.recorder(); r.poll()
        swap = self.profile / 'swap.tmp'; swap.write_bytes(head + b'new\n'); os.replace(swap, self.log)
        r.poll()
        self.assertEqual(self.text(r, 'engine.2.log'), (head + b'new\n').decode())
        self.assertIn('replaced', [e['kind'] for e in self.events(r)])

    def test_export_manifest_hashes_the_exported_bytes_and_keeps_provenance(self):
        self.log.write_text('peer 10.0.0.7\n')
        r = self.recorder(); r.poll(); out = r.close()
        with zipfile.ZipFile(rec.export(out)) as z:
            info = json.loads(z.read(out.name + '/export-manifest.json'))
            for name, row in info['files'].items():
                data = z.read(out.name + '/' + name)
                self.assertEqual((hashlib.sha256(data).hexdigest(), len(data)), (row['sha256'], row['size']), name)
            self.assertIn(out.name + '/original-manifest.json', z.namelist())
        self.assertTrue(info['masked'])
        self.assertEqual(info['original_sha256']['sources/engine.1.log'],
                         hashlib.sha256((out / 'sources/engine.1.log').read_bytes()).hexdigest())

    def test_account_names_with_spaces_and_escaped_json_paths_are_masked_whole(self):
        for raw, masked in (('C:\\Users\\Alice Smith\\X4', 'C:\\Users\\<user>\\X4'),
                            ('"C:\\\\Users\\\\Alice Smith\\\\X4"', '"C:\\\\Users\\\\<user>\\\\X4"'),
                            ('/home/bob smith/x', '/home/<user>/x')):
            self.assertEqual(rec.mask(raw), masked)
            self.assertNotIn('Smith', rec.mask(raw)); self.assertNotIn('smith', rec.mask(raw))

    def test_storage_cap_streams_in_chunks_and_records_the_omitted_range(self):
        old = rec.CHUNK, rec.LINE_MAX
        self.addCleanup(lambda: (setattr(rec, 'CHUNK', old[0]), setattr(rec, 'LINE_MAX', old[1])))
        rec.CHUNK, rec.LINE_MAX = 4096, 4096
        line = b'y' * 99 + b'\n'
        self.log.write_bytes(line * 40000 + b'z' * 50000)                          # ~4 MB, unfinished tail
        r = self.recorder(max_mb=1)
        reads = []
        real = rec.Sink.feed
        def spy(sink, chunk, final=False): reads.append(len(chunk)); return real(sink, chunk, final)
        rec.Sink.feed = spy; self.addCleanup(setattr, rec.Sink, 'feed', real)
        r.poll()
        tail = r.tails['engine']
        self.assertLessEqual(max(reads), 4096); self.assertLessEqual(len(tail.sink.pending), 4096 + 4096)
        r.close()
        copied = (r.out / 'sources/engine.1.log').stat().st_size
        omitted = [e for e in self.events(r) if e['kind'] == 'omitted']
        self.assertLessEqual(copied, 1 << 20)
        self.assertEqual(omitted[0]['reason'], 'storage_limit'); self.assertEqual(omitted[0]['start'], copied)
        self.assertEqual(copied + sum(e['bytes'] for e in omitted), self.log.stat().st_size)
        stop = self.events(r)[-1]
        self.assertEqual((stop['kind'], stop['complete']), ('stop', False))

    def test_oversized_line_is_omitted_whole_never_split(self):
        old = rec.CHUNK, rec.LINE_MAX
        self.addCleanup(lambda: (setattr(rec, 'CHUNK', old[0]), setattr(rec, 'LINE_MAX', old[1])))
        rec.CHUNK, rec.LINE_MAX = 4096, 4096
        # Astra's case: an all-hex oversized line with the session key across the old cut point.
        self.log.write_bytes(b'f' * (2 * 4096 - 80) + TOKEN.encode() + b'0' * 48)
        r = self.recorder(); r.poll()
        self.assertLessEqual(len(r.tails['engine'].sink.pending), 4096)
        with open(self.log, 'ab') as f: f.write(b'\nafter ' + LEASE.encode() + b'\n')
        r.poll(); out = r.close()
        text = self.text(r, 'engine.1.log')
        self.assertTrue(text.startswith('after <id:')); self.assertNotIn(LEASE, text)
        gap = [e for e in self.events(r) if e['kind'] == 'omitted']
        self.assertEqual([(g['start'], g['end'], g['reason']) for g in gap], [(0, 2 * 4096 + 1, 'oversized_line')])
        for path in out.rglob('*'):
            if path.is_file(): self.assertNotIn(TOKEN.encode(), path.read_bytes().lower(), path)

    def test_export_never_masks_part_of_a_line(self):
        old = rec.LINE_MAX; self.addCleanup(setattr, rec, 'LINE_MAX', old)
        r = self.recorder(); r.poll(); out = r.close()
        rec.LINE_MAX = 1000
        (out / 'sources/foreign.log').write_bytes(b'q' * 994 + b'192.0.2.109\n' + b'x' * 995 + b'C:\\Users\\Alice Smith\\x\n'
                                                   + b'ok 10.0.0.1\n')
        with zipfile.ZipFile(rec.export(out)) as z:
            text = z.read(out.name + '/sources/foreign.log').decode()
            info = json.loads(z.read(out.name + '/export-manifest.json'))
        self.assertNotIn('192.0.2.109', text); self.assertNotIn('Smith', text); self.assertNotIn('ce Smith', text)
        self.assertEqual(text.count('[export omitted an oversized line'), 2); self.assertIn('ok <ipv4>', text)
        self.assertEqual([d['bytes'] for d in info['omitted_lines']], [1006, 1018])

    def test_write_errors_are_explicit_omissions_and_mark_the_run_incomplete(self):
        import builtins, errno
        self.log.write_bytes(b'first\n')
        r = self.recorder()
        failed = []
        def full_disk(path, mode='r', *a, **kw):
            if mode == 'ab' and str(path).endswith('engine.1.log') and not failed:
                failed.append(path); raise OSError(errno.ENOSPC, 'No space left on device')
            return builtins.open(path, mode, *a, **kw)
        rec.open = full_disk; self.addCleanup(delattr, rec, 'open')
        r.poll()
        with open(self.log, 'ab') as f: f.write(b'second\n')
        r.poll(); r.close()
        self.assertEqual(self.text(r, 'engine.1.log'), 'second\n')
        events = self.events(r)
        gap = [e for e in events if e['kind'] == 'omitted']
        self.assertEqual([(g['start'], g['end'], g['reason']) for g in gap], [(0, 6, 'write_error')])
        self.assertEqual([e['kind'] for e in events].count('write_error'), 1)
        manifest = json.loads((r.out / 'manifest.json').read_text())
        self.assertFalse(manifest['complete']); self.assertEqual(list(manifest['write_errors'].values()), [1])
        self.assertEqual(r.used, len('second\n'))

    def test_recorder_stays_standalone_and_read_only(self):
        source = (ROOT / 'distribution/playtest_recorder.py').read_text()
        self.assertNotIn('diagnostics', source.replace('the launcher\'s diagnostics', ''))
        self.assertNotIn('import x4lan', source); self.assertNotIn('from x4lan', source)
        for call in ('unlink(', 'rmtree', 'os.remove', '.replace(self.', 'write_text(self.profile'):
            self.assertNotIn(call, source)


if __name__ == '__main__':
    unittest.main()
