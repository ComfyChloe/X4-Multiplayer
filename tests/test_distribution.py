from pathlib import Path
import importlib.util,tempfile,unittest,xml.etree.ElementTree as ET,json
from unittest.mock import patch
from types import SimpleNamespace
D=Path(__file__).resolve().parents[1]/'distribution'
spec=importlib.util.spec_from_file_location('linux_setup',D/'linux_setup.py');setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
class DistributionTests(unittest.TestCase):
 def test_desktop_server_opens_terminal_without_shell_interpolation(self):
  with patch.object(setup,'ROOT',Path('/tmp/package with spaces')),patch.object(setup.sys.stdout,'isatty',return_value=False),patch.object(setup.shutil,'which',side_effect=lambda x:'/usr/bin/konsole' if x=='konsole' else None),patch.object(setup.subprocess,'Popen') as popen:
   setup.server()
   argv=popen.call_args.args[0]
   self.assertEqual(argv[:3],['/usr/bin/konsole','--separate','-e'])
   self.assertIn('/tmp/package with spaces/setup.py',argv)
   self.assertEqual(argv[-2:],['server','--console'])
   self.assertTrue(popen.call_args.kwargs['start_new_session'])
 def test_missing_terminal_explains_manual_start(self):
  with patch.object(setup.sys.stdout,'isatty',return_value=False),patch.object(setup.shutil,'which',return_value=None):
   with self.assertRaisesRegex(RuntimeError,'bash 2-START-SERVER.sh'):setup.server()
 def test_profile_only_changes_test_config_and_preserves_other_files(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=root/'45841';p.mkdir();(p/'config.xml').write_text('<root><language>49</language><autosave>true</autosave></root>');(root/'save.xml.gz').write_bytes(b'original');before=(p/'config.xml').read_bytes()
   setup.configure_profile(p,root/'backup')
   tree=ET.parse(p/'config.xml').getroot();self.assertEqual(tree.findtext('shaderquality'),'1');self.assertEqual(tree.findtext('texturequality'),'high');self.assertEqual(tree.findtext('autosave'),'false');self.assertEqual(tree.findtext('uisafemode'),'false');self.assertEqual(tree.findtext('language'),'49');self.assertEqual((root/'backup/config.xml').read_bytes(),before);self.assertEqual((root/'save.xml.gz').read_bytes(),b'original')
 def test_existing_test_save_and_symlink_refused_before_change(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=root/'45841';(p/'save').mkdir(parents=True);s=p/'save/slot.xml.gz';s.write_bytes(b'preserve')
   with self.assertRaises(RuntimeError):setup.configure_profile(p,root/'backup')
   self.assertEqual(s.read_bytes(),b'preserve');self.assertFalse((p/'config.xml').exists());s.unlink()
   (root/'original.xml').write_text('<root/>');(p/'config.xml').symlink_to(root/'original.xml')
   with self.assertRaises(RuntimeError):setup.configure_profile(p,root/'backup')
   self.assertEqual((root/'original.xml').read_text(),'<root/>')
 def test_native_and_flatpak_library_discovery(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);steam=root/'.var/app/com.valvesoftware.Steam/.local/share/Steam';(steam/'steamapps').mkdir(parents=True);lib=root/'Second Library';game=lib/'steamapps/common/X4 Foundations';game.mkdir(parents=True);(game/'X4').write_bytes(b'\x7fELFfake');(game/'version.dat').write_text('900');(lib/'steamapps/appmanifest_392160.acf').write_text('"installdir" "X4 Foundations"\n"buildid" "23660954"')
   (steam/'steamapps/libraryfolders.vdf').write_text('"path" "'+str(lib)+'"');found=setup.discover(root);self.assertEqual(found,[('flatpak',steam,game)]);setup.check_game(game,'23660954')
   with self.assertRaises(RuntimeError):setup.check_game(game,'wrong')

 def test_full_install_start_and_remove_only_owned_files(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);home=root/'home';package=root/'package';package.mkdir()
   game=home/'.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/X4 Foundations';game.mkdir(parents=True)
   (game/'X4').write_bytes(b'\x7fELFfake');(game/'version.dat').write_text('900')
   (game.parent.parent/'appmanifest_392160.acf').write_text('"installdir" "X4 Foundations"\n"buildid" "23660954"')
   source=package/'extension/x4_lan_lab';source.mkdir(parents=True);(source/'content.xml').write_text('fixture')
   (package/'package.json').write_text(json.dumps({'package_id':'fixture','steam_build':'23660954'}))
   original=home/'.var/app/com.valvesoftware.Steam/.config/EgoSoft/X4/70000001/save/main.xml.gz';original.parent.mkdir(parents=True);original.write_bytes(b'original');before=original.stat().st_mtime_ns
   with patch.object(setup,'ROOT',package),patch.object(Path,'home',return_value=home),patch.object(setup,'ensure_closed'),patch.object(setup.shutil,'which',return_value='/mock/flatpak'):
    setup.install(SimpleNamespace(game=str(game),steam_kind='flatpak'))
    fresh=ET.parse(home/'.var/app/com.valvesoftware.Steam/.config/EgoSoft/X4/45841/config.xml').getroot()
    self.assertEqual(fresh.findtext('texturequality'),'high');self.assertEqual(fresh.findtext('autosave'),'false')
    with self.assertRaises(RuntimeError):setup.install(SimpleNamespace(game=str(game),steam_kind='flatpak'))
    with patch.object(setup.subprocess,'run') as run:
     setup.start();self.assertIn('45841',run.call_args.args[0]);self.assertIn('custom_creative',run.call_args.args[0])
    marker=game/'extensions/x4_lan_lab/.x4lan-package-id';marker.write_text('foreign')
    with self.assertRaises(RuntimeError):setup.uninstall()
    self.assertTrue(marker.exists());marker.write_text('fixture');setup.uninstall()
    self.assertFalse(marker.parent.exists());self.assertEqual(original.read_bytes(),b'original');self.assertEqual(original.stat().st_mtime_ns,before)
