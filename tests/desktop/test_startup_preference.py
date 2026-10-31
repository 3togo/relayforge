from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from settings.startup import UUID, LEGACY_UUID, get_auto_start, set_auto_start, is_gnome, migrate_extension


class StartupPreferenceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'wifi-hotspot/startup.json'
        self.values = {
            'enabled-extensions': ['other@example.com', UUID],
            'disabled-extensions': [],
        }
        self.settings = Mock()
        self.settings.get_strv.side_effect = lambda key: self.values[key][:]
        def write(key, value):
            self.values[key] = value
            return True
        self.settings.set_strv.side_effect = write

    def set_startup(self, enabled):
        set_auto_start(enabled, self.settings, Mock(), self.path)

    def test_default_is_yes(self):
        self.assertTrue(get_auto_start(self.path))

    def test_xfce_setting_does_not_require_gnome_settings(self):
        set_auto_start(False, None, Mock(), self.path)
        self.assertFalse(get_auto_start(self.path))
        set_auto_start(True, None, Mock(), self.path)
        self.assertTrue(get_auto_start(self.path))

    def test_desktop_selection(self):
        for desktop, expected in [('XFCE', False), ('ubuntu:GNOME', True), ('GNOME', True)]:
            with self.subTest(desktop=desktop), patch.dict('os.environ', XDG_CURRENT_DESKTOP=desktop):
                self.assertEqual(is_gnome(), expected)

    def test_disable_is_persistent_and_preserves_other_extensions(self):
        self.set_startup(False)
        self.assertFalse(get_auto_start(self.path))
        self.assertEqual(self.values['enabled-extensions'], ['other@example.com'])

    def test_reenable_restores_extension_and_clears_its_disabled_entry(self):
        self.set_startup(False)
        self.values['disabled-extensions'] = [UUID, 'disabled@example.com']
        self.set_startup(True)
        self.assertTrue(get_auto_start(self.path))
        self.assertEqual(self.values['enabled-extensions'], ['other@example.com', UUID])
        self.assertEqual(self.values['disabled-extensions'], ['disabled@example.com'])

    def test_migration_retains_enabled_choice_without_duplicates(self):
        self.values['enabled-extensions'] = ['other@example.com', LEGACY_UUID, UUID]
        migrate_extension(self.settings, Mock())
        self.assertEqual(self.values['enabled-extensions'], ['other@example.com', UUID])

    def test_migration_retains_disabled_choice(self):
        self.values['enabled-extensions'] = ['other@example.com']
        self.values['disabled-extensions'] = [LEGACY_UUID, 'disabled@example.com']
        migrate_extension(self.settings, Mock())
        self.assertEqual(self.values['enabled-extensions'], ['other@example.com'])
        self.assertEqual(self.values['disabled-extensions'], [UUID, 'disabled@example.com'])

    def test_disabling_startup_removes_legacy_enabled_entry(self):
        self.values['enabled-extensions'] = ['other@example.com', LEGACY_UUID]
        self.set_startup(False)
        self.assertEqual(self.values['enabled-extensions'], ['other@example.com'])

    def test_migration_failure_is_reported(self):
        self.values['enabled-extensions'] = [LEGACY_UUID]
        self.settings.set_strv.side_effect = None
        self.settings.set_strv.return_value = False
        with self.assertRaises(RuntimeError):
            migrate_extension(self.settings, Mock())

    def test_failed_write_does_not_save_new_choice(self):
        self.settings.set_strv.side_effect = None
        self.settings.set_strv.return_value = False
        with self.assertRaises(RuntimeError):
            self.set_startup(False)
        self.assertTrue(get_auto_start(self.path))


class OptionalIntegrationTests(unittest.TestCase):
    def test_missing_and_installed_desktop_controls(self):
        from settings.startup import desktop_integration_available
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(desktop_integration_available(False, root))
            self.assertFalse(desktop_integration_available(True, root, [root / 'extension']))
            (root / 'tray.py').touch()
            self.assertTrue(desktop_integration_available(False, root))
            (root / 'extension').mkdir()
            (root / 'extension/metadata.json').write_text('{}')
            self.assertTrue(desktop_integration_available(True, root, [root / 'extension']))

    def test_missing_tray_rejected_before_gi_import_or_spawn(self):
        from settings.startup import launch_tray
        with patch('settings.startup.desktop_integration_available', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'gnome-wifi-hotspot-tray'):
                launch_tray()
