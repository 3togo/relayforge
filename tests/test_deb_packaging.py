"""Check the installable tray/login contract without installing a package."""
import configparser
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(all(shutil.which(tool) for tool in
                         ('dpkg', 'dpkg-deb', 'dpkg-shlibdeps')),
                     'Debian packaging tools are required')
class DebPackagingTests(unittest.TestCase):
    def test_tray_launchers_dependencies_and_login_configuration(self):
        # A small native ELF exercises archive assembly without a Rust build.
        binary = Path(shutil.which('true')).resolve()
        for backend in ('networkmanager', 'create_ap'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                result = subprocess.run(
                    ['python3', str(ROOT / 'tools/build-deb.py'), '--binary', str(binary),
                     '--backend', backend, '--output-dir', str(work)],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                package = next(work.glob('*.deb'))
                payload = work / 'payload'
                subprocess.run(['dpkg-deb', '--raw-extract', str(package), str(payload)],
                               check=True, capture_output=True)
                depends = subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Depends'], text=True)
                for dependency in ('gir1.2-gtk-3.0', 'gir1.2-ayatanaappindicator3-0.1'):
                    self.assertIn(dependency, depends)
                launcher = payload / 'usr/bin/wifi-hotspot-tray'
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
                subprocess.run(['sh', '-n', str(launcher)], check=True)
                self.assertTrue((payload / 'usr/lib/relayforge/settings/tray.py').is_file())
                for name, option in (
                    ('usr/share/applications/relayforge-tray.desktop', '--show-icon'),
                    ('etc/xdg/autostart/relayforge-tray.desktop', '--session-start'),
                ):
                    entry = configparser.ConfigParser(interpolation=None)
                    entry.read(payload / name)
                    desktop = entry['Desktop Entry']
                    self.assertEqual(desktop['Exec'], 'wifi-hotspot-tray ' + option)
                    self.assertEqual(desktop['TryExec'], 'wifi-hotspot-tray')
                    self.assertNotIn('NotShowIn', desktop)
                    if shutil.which('desktop-file-validate'):
                        subprocess.run(['desktop-file-validate', str(payload / name)], check=True)
                self.assertIn('/etc/xdg/autostart/relayforge-tray.desktop',
                              (payload / 'DEBIAN/conffiles').read_text().splitlines())
                self.assertFalse((payload / 'etc/wifi-hotspot.conf').exists())
                self.assertFalse((payload / 'DEBIAN/postinst').exists())


if __name__ == '__main__':
    unittest.main()
