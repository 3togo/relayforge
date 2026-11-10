"""Offline checks for version-locked, non-installing integration builds."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_integration', ROOT / 'tools/build-integration.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class IntegrationBuildTests(unittest.TestCase):
    def source(self, root, component, version=None):
        definition = builder.COMPONENTS[component]
        directory = root / 'input'
        (directory / 'debian').mkdir(parents=True)
        (directory / 'debian/changelog').write_text(
            f"{definition['source']} ({version or definition['version']}) stonking; urgency=medium\n")
        return directory

    def test_all_plans_require_the_exact_source_and_keep_nm_dependency_locks(self):
        for component, definition in builder.COMPONENTS.items():
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                source = self.source(Path(temporary), component)
                before = (source / 'debian/changelog').read_bytes()
                plan = builder.plan(component, source)
                self.assertEqual(plan['downstream_version'], definition['version'] + '+relayforge1')
                self.assertEqual((source / 'debian/changelog').read_bytes(), before)
                self.assertEqual(plan['source'], definition['source'])
                self.assertEqual(len(plan['integration_manifest_sha256']), 64)
                self.assertFalse((source / 'build.log').exists())

    def test_wrong_or_already_patched_source_is_rejected_before_copy(self):
        for version in ('1.36.0-4ubuntu2', '1.36.0-4ubuntu1+relay1'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self.source(root, 'nm-applet', version)
                with self.assertRaisesRegex(ValueError, 'requires unmodified source'):
                    builder.plan('nm-applet', source)
                self.assertEqual(list(root.iterdir()), [source])

    def test_release_version_must_identify_a_new_downstream_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.source(Path(temporary), 'nm-applet')
            for version in ('1.36.0-4ubuntu1', '1.35.0-1', '1.36.0;touch injected'):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    builder.plan('nm-applet', source, version)

    def test_failed_patch_preserves_the_original_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source(root, 'nm-core')
            original = (source / 'debian/changelog').read_bytes()
            definition = builder.plan('nm-core', source)
            with patch.object(builder.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'patch')):
                with self.assertRaises(subprocess.CalledProcessError):
                    builder.prepare(definition, source, root / 'copy')
            self.assertEqual((source / 'debian/changelog').read_bytes(), original)

    def test_nm_dependency_minimization_is_explicitly_unsupported(self):
        helper = builder.module('dependency_helper', ROOT / 'integration/minimize-dependencies.py')
        with self.assertRaises(KeyError):
            helper.minimize_control('Depends: libnm0 (= ${binary:Version})', 'nm-core', '1.58.1-1ubuntu3')
        with self.assertRaisesRegex(ValueError, 'Unexpected dependency template'):
            helper.minimize_control('unexpected', 'applet', '1.36.0-4ubuntu1')


if __name__ == '__main__':
    unittest.main()
