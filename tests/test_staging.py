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
