"""File-only package fixtures must preserve inputs and never execute hooks."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_lifecycle', ROOT / 'tools/check-package-lifecycle.py')
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


@unittest.skipUnless(shutil.which('dpkg-deb'), 'Debian archive tool required')
class FileFixtureTests(unittest.TestCase):
    def test_hooks_are_excluded_only_from_copies_and_payload_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            package = work / 'original.deb'
            source = work / 'source'
            control = source / 'DEBIAN'
            control.mkdir(parents=True)
            (control / 'control').write_text(
                'Package: relayforge\nVersion: 1.0\nArchitecture: all\n'
                'Maintainer: Fixture <fixture@example.invalid>\nDescription: Fixture\n')
            sentinel = work / 'must-not-be-created'
            hook = control / 'postinst'
            hook.write_text('#!/bin/sh\ntouch ' + str(sentinel) + '\n')
            hook.chmod(0o755)
            (control / 'triggers').write_text('interest fixture-trigger\n')
            payload = source / 'usr/share/fixture'
            payload.mkdir(parents=True)
            (payload / 'data').write_bytes(b'preserve payload\n')
            subprocess.run(['dpkg-deb', '--build', str(source), str(package)],
                           check=True, capture_output=True)
            original_hash = hashlib.sha256(package.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'maintainer hooks'):
                lifecycle.audit(package, 'relayforge')
            fixture = lifecycle.file_fixture(package, work / 'copy')
            lifecycle.audit(fixture, 'relayforge')
            self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(), original_hash)
            self.assertTrue(hook.exists())
            self.assertFalse(sentinel.exists())
            extracted = work / 'extracted'
            subprocess.run(['dpkg-deb', '--extract', str(fixture), str(extracted)], check=True)
            self.assertEqual((extracted / 'usr/share/fixture/data').read_bytes(), b'preserve payload\n')
            with self.assertRaisesRegex(ValueError, 'Expected relayforge-tray'):
                lifecycle.audit(fixture, 'relayforge-tray')
