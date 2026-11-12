"""A green partial or skipped run must not become release qualification."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('prepare_release', Path(__file__).resolve().parents[1] / 'tools/prepare-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class CandidateTests(unittest.TestCase):
    def test_subprocesses_use_packaging_compatible_umask(self):
        with patch.object(release.subprocess, 'run') as run:
            release.run(['fixture'], Path('/'))
        self.assertEqual(run.call_args.kwargs['umask'], 0o022)
        self.assertTrue(run.call_args.kwargs['check'])

    def test_writable_scratch_ancestor_is_rejected_before_building(self):
        from types import SimpleNamespace
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=release.os.getuid(), st_mode=0o40775)):
            with self.assertRaisesRegex(ValueError, 'Unsafe scratch ancestor'):
                release.check_scratch(Path('/untrusted'))

    def report(self):
        names = ('build', 'rust-tests', 'desktop-contracts', 'vendor-provenance',
                 'integration-provenance', 'applet-optimized', 'applet-sanitized', 'test_dbus')
        return {'full_python_suite': True, 'checks': {
            name: {'exit_code': 0, 'summary': ['OK']} for name in names}}

    def test_complete_success_is_accepted(self):
        release.validate_report(self.report())

    def test_partial_missing_failed_and_skipped_coverage_rejected(self):
        good = self.report()
        partial = copy.deepcopy(good); partial['full_python_suite'] = False
        missing = copy.deepcopy(good); del missing['checks']['applet-sanitized']
        failed = copy.deepcopy(good); failed['checks']['test_dbus']['exit_code'] = 1
        skipped = copy.deepcopy(good); skipped['checks']['test_dbus']['summary'] = ['OK (skipped=1)']
        for report in (partial, missing, failed, skipped, {}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                release.validate_report(report)
