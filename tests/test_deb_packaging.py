"""Check the installable tray/login contract without installing a package."""
import configparser
import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(all(shutil.which(tool) for tool in
                         ('dpkg', 'dpkg-deb', 'dpkg-shlibdeps')),
                     'Debian packaging tools are required')
class DebPackagingTests(unittest.TestCase):
    def test_integrated_package_requires_matching_desktop_packages(self):
        spec = importlib.util.spec_from_file_location('desktop_deb_builder', ROOT / 'tools/build-deb.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        original_run = builder.run
        builds = []

        def run(*args, **kwargs):
            # The actual desktop source builds are exercised separately; this
            # test checks archive assembly and its installation dependency contract.
            if args[0] == 'bash':
                builds.append((args, kwargs))
                return
            return original_run(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary, patch.object(builder, 'run', run):
            output = Path(temporary)
            builder.build(argparse.Namespace(
                version='0.0.0~test', binary=Path(shutil.which('dash') or shutil.which('true')),
                backend='create_ap', output_dir=output, force=False, desktop_integrations='both'))
            package = next(output.glob('*.deb'))
            depends = subprocess.check_output(['dpkg-deb', '--field', str(package), 'Depends'], text=True)
            self.assertIn('network-manager-applet (= 1.36.0-4ubuntu1+relayforge1)', depends)
            self.assertIn('gnome-control-center (= 1:51.0-1ubuntu1+relayforge1)', depends)
            self.assertEqual([Path(args[1]).parent.name for args, _ in builds],
                             ['nm-applet', 'gnome-settings'])
            for _, kwargs in builds:
                self.assertEqual(kwargs['env']['RELAYFORGE_INTEGRATION_OUTPUT'], str(output))

    def test_tray_launchers_dependencies_and_login_configuration(self):
        # dash is small even on systems where true is a large multicall binary.
        # Exercise archive assembly without a Rust build.
        binary = Path(shutil.which('dash') or shutil.which('true')).resolve()
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
                version = subprocess.check_output(['dpkg-deb', '--field', str(package), 'Version'], text=True).strip()
                self.assertEqual((payload / 'usr/lib/relayforge/settings/build_info.py').read_text(),
                                 f'VERSION = {version!r}\n')
                depends = subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Depends'], text=True)
                for dependency in ('gir1.2-gtk-3.0', 'gir1.2-ayatanaappindicator3-0.1'):
                    self.assertIn(dependency, depends)
                launcher = payload / 'usr/bin/wifi-hotspot-tray'
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
                subprocess.run(['sh', '-n', str(launcher)], check=True)
                # GNOME enables the bundled Shell extension at first login; KDE and
                # XFCE start the StatusNotifier tray. One dispatcher covers both.
                dispatcher = payload / 'usr/bin/wifi-hotspot-enable-extension'
                self.assertEqual(dispatcher.stat().st_mode & 0o777, 0o755)
                subprocess.run(['sh', '-n', str(dispatcher)], check=True)
                self.assertEqual((payload / 'usr/lib/relayforge/settings/tray.py').read_bytes(),
                                 (ROOT / 'desktop/tray.py').read_bytes())
                for name in ('main.py', 'device_info.py', 'adapter_limits.py'):
                    self.assertEqual((payload / 'usr/lib/relayforge/settings' / name).read_bytes(),
                                     (ROOT / 'desktop' / name).read_bytes())
                for name, executable in (
                    ('usr/share/applications/relayforge-tray.desktop', 'wifi-hotspot-tray --show-icon'),
                    ('etc/xdg/autostart/relayforge-tray.desktop', 'wifi-hotspot-enable-extension'),
                ):
                    entry = configparser.ConfigParser(interpolation=None)
                    entry.read(payload / name)
                    desktop = entry['Desktop Entry']
                    self.assertEqual(desktop['Exec'], executable)
                    self.assertEqual(desktop['TryExec'], executable.split()[0])
                    self.assertNotIn('NotShowIn', desktop)
                    if shutil.which('desktop-file-validate'):
                        subprocess.run(['desktop-file-validate', str(payload / name)], check=True)
                # The GNOME Shell extension ships from the pinned vendor tree.
                extension = payload / 'usr/share/gnome-shell/extensions/wifi-relay@3togo.github.io'
                for name in ('metadata.json', 'extension.js', 'prefs.js', 'startup.js', 'stylesheet.css'):
                    self.assertEqual((extension / name).stat().st_mode & 0o777, 0o644)
                self.assertIn('"wifi-relay@3togo.github.io"', (extension / 'metadata.json').read_text())
                self.assertIn('/etc/xdg/autostart/relayforge-tray.desktop',
                              (payload / 'DEBIAN/conffiles').read_text().splitlines())
                self.assertFalse((payload / 'etc/wifi-hotspot.conf').exists())
                self.assertFalse((payload / 'DEBIAN/postinst').exists())
                # The first-login dispatcher ships beside the extension it enables.
                shipped = {path.name for path in (payload / 'usr/lib/relayforge/settings').rglob('*')
                           if path.is_file()}
                self.assertIn('enable-extension.py', shipped)
                self.assertIn('launcher.py', shipped)
                self.assertIn('tray.py', shipped)
                # The XFCE panel needs this plugin to show any StatusNotifier icon.
                recommends = subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Recommends'], text=True)
                self.assertIn('xfce4-statusnotifier-plugin', recommends)
                # A fresh install has no /etc/wifi-hotspot.conf. The service exits with
                # a dedicated status that the unit must never restart, so a desktop
                # client polling GetStatus cannot re-trigger activation forever.
                unit = (payload / 'usr/lib/systemd/system'
                        / ('relayforge-native.service' if backend == 'networkmanager'
                           else 'relayforge-create-ap.service')).read_text()
                self.assertIn('RestartPreventExitStatus=17', unit)
                # Owned resources are recovered after every stop, including a crash.
                self.assertIn('ExecStopPost=', unit)
                self.assertIn('--recover-all', unit)
                # RestartSec must stay inside the rate-limit window or the limit never trips.
                interval = int(unit.split('StartLimitIntervalSec=')[1].split()[0])
                self.assertGreaterEqual(interval, 60)
                self.assertIn('StartLimitBurst=', unit)
                # The package ships its own Polkit action so the desktop controls do
                # not prompt; remote and inactive sessions must still be denied.
                policy = (payload / 'usr/share/polkit-1/actions'
                          / 'io.github.erhanzeyrek.WifiHotspot.policy').read_text()
                manage = policy.split('.manage')[1].split('</action>')[0]
                credentials = policy.split('.credentials')[1].split('</action>')[0]
                # Start/Stop stay prompt-free for the active session.
                self.assertIn('<allow_active>yes</allow_active>', manage)
                # Reading or rewriting the password requires authenticating first.
                self.assertIn('<allow_active>auth_self_keep</allow_active>', credentials)
                for action in (manage, credentials):
                    self.assertIn('<allow_any>no</allow_any>', action)
                    self.assertIn('<allow_inactive>no</allow_inactive>', action)


    def test_existing_package_is_preserved_unless_force_is_given(self):
        binary = Path(shutil.which('dash') or shutil.which('true')).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            command = ['python3', str(ROOT / 'tools/build-deb.py'), '--binary', str(binary),
                       '--version', '0.0.0~test', '--output-dir', str(work)]
            subprocess.run(command, check=True, capture_output=True)
            package = next(work.glob('*.deb'))
            original = package.read_bytes()
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('Refusing to overwrite', refused.stderr)
            self.assertEqual(package.read_bytes(), original)
            inode = package.stat().st_ino
            forced = subprocess.run([*command, '--force'], capture_output=True, text=True)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            # Renamed into place: a new inode, and still a valid package.
            self.assertNotEqual(package.stat().st_ino, inode)
            self.assertEqual(
                subprocess.check_output(['dpkg-deb', '--field', str(package), 'Package'],
                                        text=True).strip(), 'relayforge')
            # A forced replacement is renamed into place, never left half-written.
            self.assertEqual([p.name for p in work.iterdir() if p.name.startswith('.relayforge-')], [])


if __name__ == '__main__':
    unittest.main()
