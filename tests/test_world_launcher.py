"""Shared-world launcher boundaries; engine completion is tested separately."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from x4lan import seed

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('world_launcher',ROOT/'distribution/world_launcher.py')
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)

class WorldLauncher(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'package';self.root.mkdir()
        self.profile=Path(self.temp.name)/'45841';self.profile.mkdir()
        (self.profile/'config.xml').write_text('<root><autosave>false</autosave><uisafemode>false</uisafemode></root>')
        self.game=Path(self.temp.name)/'steamapps/common/X4';self.game.mkdir(parents=True)
        (self.game/'version.dat').write_text('900')
        (self.game.parent.parent/'appmanifest_392160.acf').write_text('"buildid" "23660954"')
        self.target=self.game/'extensions/x4_lan_lab';self.target.mkdir(parents=True)
        (self.target/'ui').mkdir();(self.target/'content.xml').write_text('<content id="x4_lan_lab" version="8"/>')
        (self.target/'ui/config.lua').write_text('X4LAN_CONFIG = {\nseed_verified = false,\n}\n')
        (self.target/'.x4lan-package-id').write_text('lan-owned')
        (self.root/'extension').mkdir();shutil.copytree(self.target,self.root/'extension/x4_lan_lab')
        self.state=dict(game=str(self.game),profile=str(self.profile),package_id='lan-owned',steam=['steam'])
        w.write(self.root/'installation.json',self.state)
        self.package=dict(slot='p1',package_id='lan-owned',steam_build='23660954')
        self.session=dict(bubble='a'*32,token='c'*32,build='world1-test',server='127.0.0.1')
        dlc=self.game/'extensions/timelines';dlc.mkdir();(dlc/'content.xml').write_text('<content id="ego_dlc_timelines" version="900"/>')
        _,_,self.identity=w.context(self.root,self.package,self.session)
        self.world=self.root/'world';self.world.mkdir()
        self.source=Path(self.temp.name)/'new.xml';self.source.write_bytes(b'<savegame>native fixture</savegame>')
        self.manifest=seed.create_bundle(self.source,self.world/'bundle',**self.identity)
        w.write(self.world/'prepared.json',dict(seed_id=self.session['bubble'],sha256=self.manifest['sha256'],native_completed=True))
    def test_content_digest_is_equal_across_platform_transport_binaries_but_rejects_script_drift(self):
        (self.target/'native').mkdir();(self.target/'native/x4lan_net.so').write_bytes(b'linux')
        w.context(self.root,self.package,self.session)
        (self.target/'ui/unknown.lua').write_text('different')
        with self.assertRaisesRegex(RuntimeError,'scripts differ'):w.context(self.root,self.package,self.session)
    def test_content_read_only_and_active_extra_mod_rejected(self):
        mod=self.game/'extensions/extra';mod.mkdir();(mod/'content.xml').write_text('<content id="ws_123" version="100"/>')
        with self.assertRaisesRegex(RuntimeError,'Enabled extra mod'):w.local_content(self.game,self.profile)
        selection=self.profile/'content.xml';selection.write_text('<content><extension id="ws_123" enabled="false"/></content>')
        before=selection.read_bytes();self.assertEqual(w.local_content(self.game,self.profile),self.identity['extensions']);self.assertEqual(before,selection.read_bytes())
    def test_stage_idempotent_and_removal_requires_exact_owned_bytes(self):
        path=w.stage(self.root,self.profile,self.world/'bundle')
        self.assertEqual(path,w.stage(self.root,self.profile,self.world/'bundle'))
        path.write_bytes(b'player changed this')
        with self.assertRaisesRegex(RuntimeError,'preserved'):w.remove_slot(self.root,self.state)
        self.assertEqual(path.read_bytes(),b'player changed this')
        path.write_bytes(self.source.read_bytes());w.remove_slot(self.root,self.state)
        self.assertFalse(path.exists());self.assertTrue((self.world/'bundle/seed.bin').is_file())
    def test_preexisting_identical_slot_cannot_be_adopted_without_ownership(self):
        folder=self.profile/'save';folder.mkdir();slot=folder/'save_010.xml';slot.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(RuntimeError,'no ownership'):w.stage(self.root,self.profile,self.world/'bundle')
        self.assertTrue(slot.exists());self.assertFalse((self.world/'slot.json').exists())
    def test_extra_or_symlink_save_rejects_without_removal(self):
        path=w.stage(self.root,self.profile,self.world/'bundle');other=path.parent/'save_002.xml';other.write_bytes(b'keep')
        with self.assertRaises(RuntimeError):w.remove_slot(self.root,self.state)
        self.assertTrue(path.exists());self.assertTrue(other.exists())
    def test_real_tcp_transfer_and_invalid_auth_do_not_stop_server(self):
        server=w.WorldServer(self.root,self.session,bind='127.0.0.1',port=0)
        address=server.listener.getsockname()
        with server:
            with socket.create_connection(address,timeout=2) as c:
                with self.assertRaises(seed.SeedError):seed.receive_bundle(c,self.root/'rejected','d'*32,**self.identity)
            with socket.create_connection(address,timeout=2) as c:
                result=seed.receive_bundle(c,self.root/'received',self.session['token'],**self.identity)
            self.assertEqual(result,self.manifest)
        self.assertFalse(server.thread.is_alive())
    def test_server_refuses_a_bundle_without_native_preparation_receipt(self):
        (self.world/'prepared.json').unlink()
        with self.assertRaisesRegex(RuntimeError,'PREPARE-WORLD'):w.WorldServer(self.root,self.session,port=0)

    def test_missing_timelines_refuses_before_game_or_profile_writes(self):
        selection=self.profile/'content.xml';selection.write_text('<content><extension id="ego_dlc_timelines" enabled="false"/></content>')
        with self.assertRaisesRegex(RuntimeError,'Timelines'):w.context(self.root,self.package,self.session)
        self.assertFalse((self.profile/'save').exists())

    def test_server_refuses_foreign_world(self):
        with self.assertRaisesRegex(RuntimeError,'matching completed'):w.WorldServer(self.root,{**self.session,'bubble':'b'*32},port=0)
    def test_interrupted_copy_intent_can_complete_but_different_intent_cannot(self):
        path=self.profile/'save/save_010.xml'
        w.write(self.world/'slot.json',dict(path=str(path),sha256=self.manifest['sha256'],seed_id=self.session['bubble']))
        self.assertEqual(w.stage(self.root,self.profile,self.world/'bundle'),path)
        path.unlink();w.write(self.world/'slot.json',dict(path=str(path),sha256='b'*64,seed_id=self.session['bubble']))
        with self.assertRaisesRegex(RuntimeError,'ownership differs'):w.stage(self.root,self.profile,self.world/'bundle')
    def test_capture_completion_order_and_status_are_required_before_sealing(self):
        shutil.rmtree(self.world/'bundle');(self.world/'prepared.json').unlink()
        run=self.world/'capture';run.mkdir();cfg=self.target/'ui/config.lua';old=cfg.read_bytes();(run/'config-before.lua').write_bytes(old)
        cfg.write_bytes(w.capture_config(self.identity));w.write(run/'ownership.json',dict(identity=self.identity,capture_config_sha256=w.digest(cfg),before_config_sha256=w.digest(run/'config-before.lua')))
        saves=self.profile/'save';saves.mkdir();(saves/'save_010.xml').write_bytes(self.source.read_bytes())
        key=self.session['bubble'];lines=['WORLD_CAPTURE DOCKS_MARKED id='+key,'WORLD_CAPTURE SAVE_REQUESTED id='+key,'WORLD_CAPTURE SAVED id='+key+' slot=save_010','WORLD_CAPTURE EXIT id='+key]
        logfile=self.profile/'x4lan-debug.txt'
        for bad in (lines[1:],lines[1:]+lines[:1],lines[::-1],lines[:2],lines+['WORLD_CAPTURE STOPPED failed'],lines+lines):
            logfile.write_text('\n'.join(bad))
            with self.assertRaises(RuntimeError):w.finish_capture(self.root,self.state,self.target,self.identity)
            self.assertFalse((self.world/'bundle').exists())
        logfile.write_text('\n'.join(lines));w.finish_capture(self.root,self.state,self.target,self.identity)
        self.assertEqual(cfg.read_bytes(),old);self.assertTrue((self.world/'prepared.json').exists())
        w.validate_slot(self.root,self.profile,self.manifest)
    def test_guest_cached_content_mismatch_never_stages_or_launches(self):
        self.package['slot']='p2';m=self.world/'bundle/manifest.json';value=w.read(m);value['extensions']=[];w.write(m,value)
        with patch.object(w,'launch') as launch,self.assertRaisesRegex(RuntimeError,'DLC/mod'):
            w.start(self.root,self.package,self.session,lambda:None)
        launch.assert_not_called();self.assertFalse((self.profile/'save').exists())
    def test_failed_native_capture_is_reported_and_retry_retains_evidence(self):
        shutil.rmtree(self.world)
        key=self.session['bubble'];log=self.profile/'x4lan-debug.txt'
        def fail_launch(state):
            log.write_text('WORLD_CAPTURE BEGIN id='+key+'\nWORLD_CAPTURE STOPPED content mismatch')
        from unittest.mock import Mock
        closed=Mock(side_effect=[None,RuntimeError('running')])
        with patch.object(w,'launch',side_effect=fail_launch),self.assertRaisesRegex(RuntimeError,'content mismatch'):
            w.prepare(self.root,self.package,self.session,closed,timeout=1)
        self.assertTrue((self.world/'capture/failed-native.log').is_file())
        self.assertFalse((self.profile/'save').exists())
        def successful_launch(state):
            saves=self.profile/'save';saves.mkdir();(saves/'save_010.xml').write_bytes(self.source.read_bytes())
            log.write_text('WORLD_CAPTURE BEGIN id='+key+'\nWORLD_CAPTURE DOCKS_MARKED id='+key+'\nWORLD_CAPTURE SAVE_REQUESTED id='+key+
                           '\nWORLD_CAPTURE SAVED id='+key+' slot=save_010\nWORLD_CAPTURE EXIT id='+key)
        closed=Mock(side_effect=[None,RuntimeError('running'),None])
        with patch.object(w,'launch',side_effect=successful_launch),patch.object(w.time,'sleep'):
            w.prepare(self.root,self.package,self.session,closed,timeout=1)
        self.assertTrue((self.world/'prepared.json').is_file())
        self.assertEqual(len(list(self.world.glob('failed-capture-*'))),1)

    def test_settings_reimport_accepts_only_the_matching_owned_snapshot(self):
        from distribution import profile_settings
        w.write(self.root/'package.json',dict(channel='internal',package_id='lan-owned'))
        source=self.profile.parent/'12345';source.mkdir();(source/'config.xml').write_text('<root><res_width>5120</res_width></root>')
        slot=w.stage(self.root,self.profile,self.world/'bundle')
        check=lambda profile:w.validate_slot(self.root,profile,self.manifest)
        profile_settings.import_settings(self.root,source=source,slot_validator=check)
        self.assertEqual(slot.read_bytes(),self.source.read_bytes())
        slot.write_bytes(b'changed by another program')
        before=(self.profile/'config.xml').read_bytes()
        with self.assertRaises(RuntimeError):profile_settings.import_settings(self.root,source=source,slot_validator=check)
        self.assertEqual(before,(self.profile/'config.xml').read_bytes())

    def test_windows_start_keeps_the_existing_native_reparse_guard(self):
        with patch.object(w.sys,'platform','win32'),patch.object(w.subprocess,'run') as run:
            w.owned_installation(self.root,self.package)
        self.assertEqual(run.call_args.args[0][-2:],['-Action','CheckWorldProfile'])

    def test_launch_preserves_steam_prefix_and_required_flags_on_both_platforms(self):
        for steam in (['flatpak','run','com.valvesoftware.Steam'],'C:\\Steam\\steam.exe'):
            with patch.object(w.subprocess,'run') as run:w.launch({'steam':steam})
            argv=run.call_args.args[0];self.assertIn('-noworkshopsync',argv);self.assertIn('-nocputhrottle',argv);self.assertEqual(argv[-1],'-noworkshopsync')
            self.assertEqual(argv[0],steam[0] if isinstance(steam,list) else steam)

    def test_play_start_preserves_each_prior_log_even_with_same_clock_value(self):
        log = self.profile / 'x4lan-debug.txt'
        first = b'previous guest session\xff\x00\nkey=' + self.session['token'].encode()
        second = b'next host session\n'
        log.write_bytes(first)
        source_mtime = log.stat().st_mtime_ns
        with patch.object(w.time, 'time_ns', return_value=123456789), patch.object(
                w, 'launch', side_effect=lambda state: log.write_bytes(second)) as launch:
            w.start(self.root, self.package, self.session, lambda: None)
            w.start(self.root, self.package, self.session, lambda: None)
        self.assertEqual(launch.call_count, 2)
        records = list((self.root / 'backups/engine-logs').glob('*/manifest.json'))
        self.assertEqual(len(records), 2)
        self.assertFalse((self.root / 'logs').exists())
        self.assertEqual({p.parent.joinpath('x4lan-debug.txt').read_bytes() for p in records}, {first, second})
        for p in records:
            record = json.loads(p.read_text()); data = (p.parent / 'x4lan-debug.txt').read_bytes()
            self.assertEqual(record['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(record['source_size'], len(data))
            self.assertEqual(record['before_operation'], 'start')
            self.assertTrue(record['complete']); self.assertTrue(record['raw_private'])
            self.assertNotIn(self.session['token'], p.read_text())
            if data == first: self.assertEqual(record['source_mtime_ns'], source_mtime)
        self.assertEqual(log.read_bytes(), second)

    def test_world_preparation_keeps_discoverable_and_capture_log_backups(self):
        shutil.rmtree(self.world)
        log = self.profile / 'x4lan-debug.txt'; original = b'previous human guest evidence\xff\n'
        log.write_bytes(original)
        def native_start(state):
            log.write_bytes(b'new preparation log')
            raise RuntimeError('simulated launch failure')
        with patch.object(w, 'launch', side_effect=native_start), self.assertRaisesRegex(RuntimeError, 'simulated'):
            w.prepare(self.root, self.package, self.session, lambda: None)
        self.assertEqual((self.world / 'capture/previous.log').read_bytes(), original)
        records = list((self.root / 'backups/engine-logs').glob('*/manifest.json'))
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0].parent / 'x4lan-debug.txt').read_bytes(), original)
        self.assertEqual(w.read(records[0])['before_operation'], 'prepare')

    def test_log_archive_failure_refuses_before_staging_or_capture_configuration(self):
        log = self.profile / 'x4lan-debug.txt'; log.write_bytes(b'unarchived evidence')
        config = self.target / 'ui/config.lua'; before = config.read_bytes()
        for action in (w.start, w.prepare):
            if action is w.prepare: shutil.rmtree(self.world)
            with self.subTest(action=action.__name__), patch.object(w.os, 'fsync', side_effect=OSError('disk full')), \
                    patch.object(w, 'launch') as launch, self.assertRaisesRegex(RuntimeError, 'X4 was not launched'):
                action(self.root, self.package, self.session, lambda: None)
            launch.assert_not_called()
            self.assertEqual(log.read_bytes(), b'unarchived evidence')
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((self.profile / 'save').exists())
            self.assertFalse((self.world / 'capture/ownership.json').exists())
        self.assertFalse(list((self.root / 'backups/engine-logs').glob('*/manifest.json')))
        self.assertEqual(len(list((self.root / 'backups/engine-logs').glob('*/*.partial'))), 2)

    def test_log_change_during_archive_is_not_certified_complete(self):
        log = self.profile / 'x4lan-debug.txt'; log.write_bytes(b'old capture')
        real_fstat = os.fstat; calls = 0
        def changed(fd):
            nonlocal calls
            calls += 1
            if calls == 2: log.write_bytes(b'new longer capture')
            return real_fstat(fd)
        with patch.object(w.os, 'fstat', side_effect=changed), self.assertRaisesRegex(RuntimeError, 'changed while'):
            w.archive_engine_log(self.root, self.profile, 'start')
        self.assertEqual(log.read_bytes(), b'new longer capture')
        self.assertFalse(list((self.root / 'backups/engine-logs').glob('*/manifest.json')))

    def test_missing_log_is_normal_for_first_launch_and_empty_log_can_be_archived(self):
        with patch.object(w, 'launch') as launch:
            w.start(self.root, self.package, self.session, lambda: None)
        launch.assert_called_once()
        self.assertFalse((self.root / 'backups/engine-logs').exists())
        log = self.profile / 'x4lan-debug.txt'; log.write_bytes(b'')
        archive = w.archive_engine_log(self.root, self.profile, 'start')
        self.assertEqual(archive.read_bytes(), b'')
        self.assertEqual(w.read(archive.parent / 'manifest.json')['source_size'], 0)

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires Windows developer privileges')
    def test_log_archive_refuses_linked_input_and_destination(self):
        unrelated = Path(self.temp.name) / 'unrelated'; unrelated.mkdir()
        other = unrelated / 'log'; other.write_bytes(b'preserve')
        log = self.profile / 'x4lan-debug.txt'; log.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'regular unlinked'): w.archive_engine_log(self.root, self.profile, 'start')
        log.unlink(); log.write_bytes(b'owned')
        (self.root / 'backups').symlink_to(unrelated, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'directory is linked'): w.archive_engine_log(self.root, self.profile, 'start')
        self.assertEqual(list(unrelated.iterdir()), [other]); self.assertEqual(other.read_bytes(), b'preserve')

    def test_windows_reparse_attribute_refuses_without_reading_the_log(self):
        log = self.profile / 'x4lan-debug.txt'; log.write_bytes(b'owned')
        original = Path.lstat
        def attributes(path):
            info = original(path)
            if path == log:
                return SimpleNamespace(st_mode=info.st_mode,
                    st_file_attributes=w.stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        with patch.object(Path, 'lstat', attributes), patch.object(w.os, 'open') as opened, \
                self.assertRaisesRegex(RuntimeError, 'regular unlinked'):
            w.archive_engine_log(self.root, self.profile, 'start')
        opened.assert_not_called()
        self.assertFalse((self.root / 'backups').exists())

if __name__=='__main__':unittest.main()
