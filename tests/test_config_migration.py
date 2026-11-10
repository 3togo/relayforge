"""Exercise the offline migration CLI with disposable private files only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('RELAYFORGE_BINARY', ROOT / 'target/debug/relayforge-service'))


class LegacyMigrationTests(unittest.TestCase):
    def command(self, path, *options):
        return subprocess.run([str(BINARY), '--migrate-config', str(path), *options],
                              capture_output=True, text=True, timeout=5)

    def test_factory_dry_run_apply_and_repeat_without_exposing_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config'
            original = (ROOT / 'compat/factory.conf').read_bytes()
            path.write_bytes(original)
            path.chmod(0o600)
            planned = self.command(path, '--dry-run')
            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertEqual(json.loads(planned.stdout),
                             {'factory_password': True, 'interfaces': False, 'applied': False})
            self.assertEqual(path.read_bytes(), original)
            applied = self.command(path)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertTrue(json.loads(applied.stdout)['applied'])
            self.assertNotEqual(path.read_bytes(), original)
            password = next(line.split('=', 1)[1] for line in path.read_text().splitlines()
                            if line.startswith('PASSPHRASE='))
            self.assertEqual(len(password), 32)
            self.assertNotIn(password, applied.stdout + applied.stderr)
            before = path.read_bytes()
            repeated = self.command(path)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout),
                             {'factory_password': False, 'interfaces': False, 'applied': False})
            self.assertEqual(path.read_bytes(), before)

    def test_customized_config_and_ambiguous_options_preserve_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config'
            original = (ROOT / 'compat/factory.conf').read_bytes().replace(
                b'SSID=Hotspot', b'SSID=Customized')
            path.write_bytes(original)
            path.chmod(0o600)
            result = self.command(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['applied'])
            result = self.command(path, '--detect-interface', '--wifi-interface', 'unused0')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not both', result.stderr)
            self.assertEqual(path.read_bytes(), original)

    def test_insecure_and_symlink_targets_fail_without_modifying_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config'
            original = (ROOT / 'compat/factory.conf').read_bytes()
            path.write_bytes(original)
            path.chmod(0o644)
            self.assertNotEqual(self.command(path).returncode, 0)
            path.chmod(0o600)
            alias = Path(temporary) / 'alias'
            alias.symlink_to(path)
            self.assertNotEqual(self.command(alias).returncode, 0)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
