"""Regressions for source identity when validation archives preserve timestamps."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('offline_validation', ROOT / 'tools/check-offline.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


class ValidationCacheTests(unittest.TestCase):
    def test_changed_source_cannot_reuse_the_previous_candidate_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old, old_identity = validation.candidate_target(base, {'main.rs': 'old-content-sha'})
            new, new_identity = validation.candidate_target(base, {'main.rs': 'new-content-sha'})
            old.mkdir(parents=True)
            sentinel = old / 'existing-build'
            sentinel.write_text('preserve')
            self.assertNotEqual(old_identity, new_identity)
            self.assertNotEqual(old, new)
            self.assertTrue(new.is_relative_to(base))
            self.assertFalse(new.exists())
            self.assertEqual(sentinel.read_text(), 'preserve')

    def test_same_contents_have_the_same_identity_independent_of_manifest_order(self):
        base = Path('/tmp/unused-validation-target')
        self.assertEqual(validation.candidate_target(base, {'a.rs': 'a', 'b.rs': 'b'}),
                         validation.candidate_target(base, {'b.rs': 'b', 'a.rs': 'a'}))


if __name__ == '__main__':
    unittest.main()
