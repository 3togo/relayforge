"""Check the installable tray/login contract without installing a package."""
import argparse
import importlib.util
from unittest.mock import patch
import configparser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


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
                backend='create_ap', output_dir=output, force=False, desktop_integrations='both', with_gnome=False, gnome_login_controls=False))
            package = next(output.glob('relayforge_*_*.deb'))
            depends = subprocess.check_output(['dpkg-deb', '--field', str(package), 'Depends'], text=True)
            self.assertIn('network-manager-applet (= 1.36.0-4ubuntu1+relayforge1)', depends)
            self.assertIn('gnome-control-center (= 1:51.0-1ubuntu1+relayforge1)', depends)
            self.assertEqual([Path(args[1]).parent.name for args, _ in builds],
                             ['nm-applet', 'gnome-settings'])
            for _, kwargs in builds:
                self.assertEqual(kwargs['env']['RELAYFORGE_INTEGRATION_OUTPUT'], str(output))

    def test_existing_package_is_preserved_unless_force_is_given(self):
        binary = Path(shutil.which('dash') or shutil.which('true')).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            command = ['python3', str(ROOT / 'tools/build-deb.py'), '--binary', str(binary),
                       '--version', '0.0.0~test', '--output-dir', str(work)]
            subprocess.run(command, check=True, capture_output=True)
            package = next(work.glob('relayforge_*_*.deb'))
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

    def test_tray_launchers_dependencies_and_login_configuration(self):
        # dash is small even on systems where true is a large multicall binary.
        # Exercise archive assembly without a Rust build.
        binary = Path(shutil.which('dash') or shutil.which('true')).resolve()
        for backend in ('networkmanager', 'create_ap'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                result = subprocess.run(
                    ['python3', str(ROOT / 'tools/build-deb.py'), '--binary', str(binary),
                     '--backend', backend, '--output-dir', str(work), '--with-gnome'],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                package = next(work.glob('relayforge_*_*.deb'))
                payload = work / 'payload'
                subprocess.run(['dpkg-deb', '--raw-extract', str(package), str(payload)],
                               check=True, capture_output=True)
                depends = subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Depends'], text=True)
                tray_package = next(work.glob('relayforge-tray_*_all.deb'))
                tray = work / 'tray'
                subprocess.run(['dpkg-deb', '--raw-extract', str(tray_package), str(tray)],
                               check=True, capture_output=True)
                tray_depends = subprocess.check_output(
                    ['dpkg-deb', '--field', str(tray_package), 'Depends'], text=True)
                policy = ET.parse(payload / 'usr/share/polkit-1/actions/io.github.erhanzeyrek.WifiHotspot.policy')
                actions = {a.attrib['id'].rsplit('.', 1)[1]: a for a in policy.findall('action')}
                self.assertEqual(actions['control'].findtext('defaults/allow_active'), 'yes')
                self.assertEqual(actions['manage'].findtext('defaults/allow_active'), 'auth_admin_keep')
                for action in actions.values():
                    self.assertEqual(action.findtext('defaults/allow_any'), 'no')
                    self.assertEqual(action.findtext('defaults/allow_inactive'), 'no')
                for dependency in ('gir1.2-gtk-3.0', 'gir1.2-ayatanaappindicator3-0.1'):
                    self.assertNotIn(dependency, depends)
                    self.assertIn(dependency, tray_depends)
                self.assertFalse((payload / 'usr/lib/relayforge/settings/tray.py').exists())
                self.assertFalse((payload / 'etc/xdg/autostart').exists())
                launcher = tray / 'usr/bin/wifi-hotspot-tray'
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
                subprocess.run(['sh', '-n', str(launcher)], check=True)
                self.assertEqual((tray / 'usr/lib/relayforge/settings/tray.py').read_bytes(),
                                 (ROOT / 'desktop/tray.py').read_bytes())
                self.assertEqual((payload / 'usr/lib/relayforge/settings/main.py').read_bytes(),
                                 (ROOT / 'desktop/main.py').read_bytes())
                for name in ('upstream_main.py', 'device_info.py', 'adapter_limits.py'):
                    self.assertEqual((payload / 'usr/lib/relayforge/settings' / name).read_bytes(),
                                     (ROOT / 'desktop' / name).read_bytes())
                self.assertEqual((payload / 'usr/lib/relayforge/settings/vendor_main.py').read_bytes(),
                                 (ROOT / 'vendor/wifi-relay/settings/main.py').read_bytes())
                self.assertEqual((payload / 'usr/share/doc/relayforge/docs/native-feature-policy.md').read_bytes(),
                                 (ROOT / 'docs/native-feature-policy.md').read_bytes())
                self.assertTrue((payload / 'usr/share/doc/relayforge/docs/native-bridge-design.md').is_file())
                self.assertEqual((payload / 'usr/share/doc/relayforge/docs/native-driver-security-audit.md').read_bytes(),
                                 (ROOT / 'docs/native-driver-security-audit.md').read_bytes())
                self.assertEqual((payload / 'usr/share/doc/relayforge/docs/native-regulatory-firewall-policy.md').read_bytes(),
                                 (ROOT / 'docs/native-regulatory-firewall-policy.md').read_bytes())
                units=list((payload / 'usr/lib/systemd').rglob('*.service'))
                self.assertTrue(units)
                self.assertTrue(all('--allow-band-switch' not in unit.read_text() for unit in units))
                self.assertTrue(all('--allow-existing-bridge' not in unit.read_text() for unit in units))
                for name, option in (
                    ('usr/share/applications/relayforge-tray.desktop', '--show-icon'),
                    ('etc/xdg/autostart/relayforge-tray.desktop', '--session-start'),
                ):
                    entry = configparser.ConfigParser(interpolation=None)
                    entry.read(tray / name)
                    desktop = entry['Desktop Entry']
                    self.assertEqual(desktop['Exec'], 'wifi-hotspot-tray ' + option)
                    self.assertEqual(desktop['TryExec'], 'wifi-hotspot-tray')
                    self.assertNotIn('NotShowIn', desktop)
                    if shutil.which('desktop-file-validate'):
                        subprocess.run(['desktop-file-validate', str(tray / name)], check=True)
                self.assertIn('/etc/xdg/autostart/relayforge-tray.desktop',
                              (tray / 'DEBIAN/conffiles').read_text().splitlines())
                self.assertFalse((payload / 'etc/wifi-hotspot.conf').exists())
                for name in ('postinst', 'prerm', 'postrm'):
                    script = payload / 'DEBIAN' / name
                    self.assertTrue(script.exists())
                    subprocess.run(['sh', '-n', str(script)], check=True)
                self.assertFalse((payload / 'usr/share/gnome-shell/extensions').exists())
                self.assertIn('gnome-wifi-hotspot', subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Conflicts'], text=True))
                gnome_package = next(work.glob('relayforge-gnome_*_all.deb'))
                gnome = work / 'gnome'
                subprocess.run(['dpkg-deb', '--raw-extract', str(gnome_package), str(gnome)],
                               check=True, capture_output=True)
                metadata = json.loads((ROOT / 'vendor/wifi-relay/extension/metadata.json').read_text())
                extension = gnome / 'usr/share/gnome-shell/extensions' / metadata['uuid']
                for source in (ROOT / 'vendor/wifi-relay/extension').iterdir():
                    if source.name == 'metadata.json':
                        installed = json.loads((extension / source.name).read_text())
                        self.assertEqual(installed['uuid'], metadata['uuid'])
                        self.assertEqual(installed['shell-version'], metadata['shell-version'])
                        self.assertEqual(installed['url'], 'https://github.com/3togo/relayforge')
                        self.assertNotIn('auto-switch', installed['description'])
                        continue
                    expected = ROOT / 'desktop/extension.js' if source.name == 'extension.js' else source
                    self.assertEqual((extension / source.name).read_bytes(), expected.read_bytes())
                version = subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Version'], text=True).strip()
                project = json.loads((payload / 'usr/lib/relayforge/settings/project.json').read_text())
                self.assertEqual(project, {'name': 'RelayForge', 'version': version})
                metadata_xml = payload / 'usr/share/metainfo/io.github.erhanzeyrek.WifiHotspot.metainfo.xml'
                component = ET.parse(metadata_xml).getroot()
                self.assertEqual(component.findtext('name'), 'RelayForge')
                self.assertEqual(component.findtext('launchable'), 'io.github.erhanzeyrek.WifiHotspot.desktop')
                self.assertEqual(component.findtext("url[@type='homepage']"), 'https://github.com/3togo/relayforge')
                self.assertIsNone(component.find('releases'))
                self.assertIn('Name=RelayForge\n', (payload / 'usr/share/applications/io.github.erhanzeyrek.WifiHotspot.desktop').read_text())
                for name, section in (('wifi-hotspot-settings', '1'), ('relayforge-service', '8')):
                    self.assertTrue((payload / f'usr/share/man/man{section}/{name}.{section}').is_file())
                self.assertIn(f'relayforge (= {version})', tray_depends)
                self.assertEqual(subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Recommends'], text=True).strip(),
                    f'relayforge-tray (= {version})')
                # No shared files can be removed by uninstalling either control.
                base_files = {p.relative_to(payload) for p in payload.rglob('*') if p.is_file()
                              and 'DEBIAN' not in p.parts}
                tray_files = {p.relative_to(tray) for p in tray.rglob('*') if p.is_file()
                              and 'DEBIAN' not in p.parts}
                self.assertFalse(base_files & tray_files)
                self.assertFalse((tray / 'DEBIAN/postinst').exists())
                self.assertEqual(subprocess.check_output(
                    ['dpkg-deb', '--field', str(gnome_package), 'Depends'], text=True).strip(),
                    f'relayforge (= {version}), gnome-shell (>= 45), gnome-shell (<< 52~)')
                for path in ('DEBIAN/postinst', 'DEBIAN/prerm', 'usr/lib/systemd',
                             'etc/xdg/autostart', 'etc/wifi-hotspot.conf'):
                    self.assertFalse((gnome / path).exists(), path)
                # A collision must fail before replacing either existing artifact.
                original = {path.name: path.read_bytes() for path in work.glob('*.deb')}
                retry = subprocess.run(
                    ['python3', str(ROOT / 'tools/build-deb.py'), '--binary', str(binary),
                     '--backend', backend, '--output-dir', str(work), '--with-gnome'],
                    capture_output=True, text=True)
                self.assertNotEqual(retry.returncode, 0)
                self.assertEqual(original, {path.name: path.read_bytes() for path in work.glob('*.deb')})

    def test_default_build_does_not_require_or_ship_gnome_shell(self):
        binary = Path(shutil.which('dash') or shutil.which('true')).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(['python3', str(ROOT / 'tools/build-deb.py'), '--binary', str(binary),
                            '--output-dir', temporary], check=True, capture_output=True)
            packages = list(Path(temporary).glob('*.deb'))
            self.assertEqual(len(packages), 2)
            for package in packages:
                dependencies = subprocess.check_output(
                    ['dpkg-deb', '--field', str(package), 'Depends'], text=True)
                self.assertNotIn('gnome-shell', dependencies)

    def test_gnome_login_setup_is_an_explicit_opt_in_without_install_hooks(self):
        binary = Path(shutil.which('dash') or shutil.which('true')).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            command = ['python3', str(ROOT / 'tools/build-deb.py'), '--binary', str(binary),
                       '--output-dir', temporary, '--gnome-login-controls']
            rejected = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(list(work.glob('*.deb')))
            subprocess.run(command + ['--with-gnome'], check=True, capture_output=True)
            package = next(work.glob('relayforge-gnome_*_all.deb'))
            payload = work / 'gnome'
            subprocess.run(['dpkg-deb', '--raw-extract', str(package), str(payload)],
                           check=True, capture_output=True)
            desktop = payload / 'etc/xdg/autostart/relayforge-gnome.desktop'
            entry = configparser.ConfigParser(interpolation=None)
            entry.read(desktop)
            self.assertEqual(entry['Desktop Entry']['OnlyShowIn'], 'GNOME;')
            self.assertEqual(entry['Desktop Entry']['Exec'], 'wifi-hotspot-enable-extension')
            self.assertIn('/etc/xdg/autostart/relayforge-gnome.desktop',
                          (payload / 'DEBIAN/conffiles').read_text().splitlines())
            launcher = payload / 'usr/bin/wifi-hotspot-enable-extension'
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
            subprocess.run(['sh', '-n', str(launcher)], check=True)
            self.assertFalse((payload / 'DEBIAN/postinst').exists())


if __name__ == '__main__':
    unittest.main()
