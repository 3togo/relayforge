"""Native payload staging must preserve existing trees and exclude credentials."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('native_staging',ROOT/'tools/stage-native.py')
staging=importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


class StagingTests(unittest.TestCase):
    def test_payload_contains_native_service_policy_and_activation_but_no_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            binary=root/'binary'
            binary.write_bytes(b'\x7fELFtest-fixture')
            output=root/'payload'
            manifest=staging.stage(binary,output)
            self.assertEqual(len(manifest),6)
            self.assertEqual((output/'usr/lib/relayforge/relayforge-service').read_bytes(),binary.read_bytes())
            self.assertEqual(manifest['usr/lib/relayforge/relayforge-service']['mode'],'0o755')
            self.assertFalse((output/'etc').exists())
            self.assertTrue(all('python' not in path and 'worker' not in path for path in manifest))
            self.assertIn('--native-networkmanager',(output/'usr/lib/systemd/system/relayforge-native.service').read_text())
            with self.assertRaises(FileExistsError):
                staging.stage(binary,output)
            self.assertTrue((output/'manifest.json').exists())

    def test_non_elf_and_symlink_output_never_replace_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            binary=root/'binary'
            binary.write_bytes(b'not an executable')
            with self.assertRaises(ValueError):
                staging.stage(binary,root/'out')
            binary.write_bytes(b'\x7fELFtest-fixture')
            output=root/'out'
            output.symlink_to(root,target_is_directory=True)
            with self.assertRaises(FileExistsError):
                staging.stage(binary,output)
            self.assertTrue(output.is_symlink())
            self.assertEqual(binary.read_bytes(),b'\x7fELFtest-fixture')

    def test_create_ap_payload_contains_native_adapter_without_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            binary=root/'binary'
            binary.write_bytes(b'\x7fELFtest-fixture')
            output=root/'payload'
            manifest=staging.stage(binary,output,'create_ap')
            self.assertEqual(len(manifest),7)
            target='usr/lib/relayforge/native/create_ap'
            self.assertEqual((output/target).read_bytes(),staging.native_engine((ROOT/'vendor/wifi-relay/daemon/create_ap').read_bytes()))
            self.assertEqual(manifest[target]['mode'],'0o755')
            self.assertNotIn('python3',(output/target).read_text())
            self.assertEqual((output/target).read_text().count('--create-ap-runtime'),6)
            self.assertTrue(all(not path.endswith('.py') for path in manifest))
            self.assertTrue(all('worker' not in path and 'wifi-hotspot-daemon.py' not in path for path in manifest))
            unit=(output/'usr/lib/systemd/system/relayforge-create-ap.service').read_text()
            self.assertIn('--native-create-ap --legacy-root /usr/lib/relayforge',unit)
            activation=(output/'usr/share/dbus-1/system-services/io.github.erhanzeyrek.WifiHotspot.service').read_text()
            self.assertIn('SystemdService=relayforge-create-ap.service',activation)
            self.assertFalse((output/'etc').exists())

    def test_native_adapter_rejects_modified_source_even_with_unchanged_helper_calls(self):
        source=(ROOT/'vendor/wifi-relay/daemon/create_ap').read_bytes()
        for modified in (source+b'\necho changed\n',source.replace(b'python3 -I',b'python3 -S',1)):
            with self.subTest(modified=modified[-20:]),self.assertRaises(ValueError):
                staging.native_engine(modified)
