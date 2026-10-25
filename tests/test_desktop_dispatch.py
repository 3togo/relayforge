"""Desktop-dispatch contract for the first-login helper.

`enable-extension.py` decides at login between enabling the bundled GNOME Shell
extension and starting the StatusNotifier tray. These checks pin the decision
inputs from the pinned vendor snapshot without needing a graphical session.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor/wifi-relay/settings'))
import startup


class DesktopDetectionTests(unittest.TestCase):
    def test_gnome_is_recognized_among_colon_separated_values(self):
        original = os.environ.get('XDG_CURRENT_DESKTOP')
        try:
            for desktop, expected in (
                ('GNOME', True), ('ubuntu:GNOME', True), ('GNOME-Classic:GNOME', True),
                ('KDE', False), ('XFCE', False), ('X-Cinnamon', False), ('', False),
            ):
                os.environ['XDG_CURRENT_DESKTOP'] = desktop
                self.assertEqual(startup.is_gnome(), expected, desktop)
        finally:
            if original is None:
                os.environ.pop('XDG_CURRENT_DESKTOP', None)
            else:
                os.environ['XDG_CURRENT_DESKTOP'] = original

    def test_gnome_needs_the_installed_extension(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-ext-') as directory:
            root = Path(directory)
            self.assertFalse(
                startup.desktop_integration_available(gnome=True, extension_paths=[root]))
            (root / 'metadata.json').write_text('{}')
            self.assertTrue(
                startup.desktop_integration_available(gnome=True, extension_paths=[root]))

    def test_other_desktops_only_need_the_tray_script(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-tray-') as directory:
            settings = Path(directory)
            self.assertFalse(
                startup.desktop_integration_available(gnome=False, settings_dir=settings))
            (settings / 'tray.py').write_text('# tray\n')
            self.assertTrue(
                startup.desktop_integration_available(gnome=False, settings_dir=settings))


if __name__ == '__main__':
    unittest.main()
