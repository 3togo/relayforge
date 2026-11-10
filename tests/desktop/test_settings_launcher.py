"""Desktop dispatch must not strand users in a panel without Relay controls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from settings import launcher


class SettingsLauncherTests(unittest.TestCase):
    def test_requires_gnome_executable_and_matching_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'bridge'
            find = Mock(return_value='/usr/bin/gnome-control-center')
            self.assertFalse(launcher.network_settings_available('GNOME', marker, find))
            for content, expected in [('1\n', True), ('2\n', False), ('', False)]:
                marker.write_text(content)
                self.assertEqual(launcher.network_settings_available('ubuntu:GNOME', marker, find), expected)
            marker.write_text('1\n')
            for desktop in ['XFCE', 'KDE', '', 'GNOME-Classic']:
                self.assertFalse(launcher.network_settings_available(desktop, marker, find))
            find.return_value = None
            self.assertFalse(launcher.network_settings_available('GNOME', marker, find))
            marker.write_bytes(b'\xff')
            self.assertFalse(launcher.network_settings_available('GNOME', marker, find))

    @patch.object(launcher, 'subprocess')
    @patch.object(launcher, 'network_settings_available', return_value=True)
    def test_integrated_launch_preserves_failure(self, available, subprocess):
        subprocess.run.return_value.returncode = 7
        self.assertEqual(launcher.main([]), 7)
        subprocess.run.assert_called_once_with(['gnome-control-center', 'wifi'], check=False)

    @patch.object(launcher, 'subprocess')
    @patch.object(launcher, 'network_settings_available', return_value=True)
    def test_standalone_bypasses_panel_without_recursion(self, available, subprocess):
        app = Mock()
        with patch.dict('sys.modules', {'main': Mock(HotspotSettingsApp=app)}):
            launcher.main(['--standalone'])
        available.assert_not_called()
        subprocess.run.assert_not_called()
        app.return_value.run.assert_called_once()

    @patch.object(launcher, 'network_settings_available', return_value=False)
    def test_stock_desktop_uses_shared_editor(self, available):
        app = Mock()
        with patch.dict('sys.modules', {'main': Mock(HotspotSettingsApp=app)}):
            launcher.main([])
        app.return_value.run.assert_called_once()

    @patch.object(launcher, 'subprocess')
    @patch.object(launcher, 'network_settings_available', return_value=True)
    def test_removed_executable_does_not_launch_competing_editor(self, available, subprocess):
        subprocess.run.side_effect = FileNotFoundError('gnome-control-center')
        with patch('sys.stderr'):
            self.assertEqual(launcher.main([]), 1)

    @patch.object(launcher, 'network_settings_available', return_value=True)
    def test_about_bypasses_integrated_settings(self, available):
        about = Mock()
        editor = Mock()
        with patch.dict('sys.modules', {'main': Mock(HotspotAboutApp=about, HotspotSettingsApp=editor)}):
            launcher.main(['--about'])
        available.assert_not_called()
        editor.assert_not_called()
        about.return_value.run.assert_called_once()
