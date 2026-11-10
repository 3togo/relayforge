"""Autostart must preserve other extensions and subsequent user choices."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "enable_extension", Path(__file__).resolve().parents[2] / "vendor/wifi-relay/settings/enable-extension.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExtensionAutostartTests(unittest.TestCase):
    def setUp(self):
        preference = patch.object(module, 'get_auto_start', return_value=True)
        self.auto_start = preference.start()
        self.addCleanup(preference.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.marker = Path(self.directory.name) / "wifi-hotspot/extension-initialized"
        self.settings = Mock()
        self.settings.get_strv.side_effect = lambda key: (
            ["other@example.com"] if key == "enabled-extensions" else [])
        self.settings.set_strv.return_value = True
        self.sync = Mock()

    def enable(self):
        module.enable_on_first_login(self.settings, self.marker, self.sync)

    def test_first_login_preserves_other_extensions(self):
        self.enable()
        self.settings.set_strv.assert_called_once_with(
            "enabled-extensions", ["other@example.com", module.UUID])
        self.sync.assert_called_once()
        self.assertTrue(self.marker.exists())

    def test_later_login_does_not_reenable_disabled_extension(self):
        self.enable()
        self.settings.reset_mock()
        self.enable()
        self.settings.set_strv.assert_not_called()

    def test_explicit_disable_is_preserved(self):
        self.settings.get_strv.side_effect = lambda key: (
            [module.UUID] if key == "disabled-extensions" else [])
        self.enable()
        self.settings.set_strv.assert_not_called()
        self.assertTrue(self.marker.exists())

    def test_already_enabled_extension_is_not_duplicated(self):
        self.settings.get_strv.side_effect = lambda key: (
            [module.UUID] if key == "enabled-extensions" else [])
        self.enable()
        self.settings.set_strv.assert_not_called()
        self.assertTrue(self.marker.exists())

    def test_failed_write_retries_on_next_login(self):
        self.settings.set_strv.return_value = False
        with self.assertRaises(RuntimeError):
            self.enable()
        self.assertFalse(self.marker.exists())
        self.settings.set_strv.return_value = True
        self.enable()
        self.assertTrue(self.marker.exists())

    def test_startup_off_does_not_enable_extension(self):
        self.auto_start.return_value = False
        self.enable()
        self.settings.get_strv.assert_not_called()
        self.assertFalse(self.marker.exists())
