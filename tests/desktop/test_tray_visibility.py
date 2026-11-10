"""Visibility preference is separate from login and sharing preferences."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'visibility', Path(__file__).resolve().parents[2] / 'vendor/wifi-relay/settings/visibility.py')
visibility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visibility)


class VisibilityTests(unittest.TestCase):
    def test_missing_or_invalid_preference_defaults_to_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tray.json'
            self.assertTrue(visibility.get_tray_visible(path))
            for text in ['broken', '[]', 'null', '{"visible":"false"}']:
                path.write_text(text)
                self.assertTrue(visibility.get_tray_visible(path))

    def test_only_explicit_false_hides_icon(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tray.json'
            for text, expected in [('{"visible":false}', False), ('{"visible":true}', True), ('{}', True)]:
                path.write_text(text)
                self.assertEqual(visibility.get_tray_visible(path), expected)
