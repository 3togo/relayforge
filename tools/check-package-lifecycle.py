#!/usr/bin/env python3
"""Check package file lifecycle in a disposable dpkg root, without activation.

Dependency checks are forced: this checks file ownership/configuration lifecycle,
not runtime dependencies, systemd, Polkit or hardware. Run in a disposable container.
"""
import argparse
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


def package_field(package, field):
    return subprocess.check_output(['dpkg-deb', '--field', str(package), field],
                                   text=True, timeout=10).strip()


def audit(package, expected):
    if package_field(package, 'Package') != expected:
        raise ValueError(f'Expected {expected} package')
    archive = subprocess.check_output(['dpkg-deb', '--ctrl-tarfile', str(package)], timeout=10)
    with tarfile.open(fileobj=io.BytesIO(archive)) as control:
        names = {Path(name).name for name in control.getnames()}
    if names.intersection({'preinst', 'postinst', 'prerm', 'postrm', 'triggers'}):
        raise ValueError('Lifecycle fixture refuses packages with executable maintainer hooks/triggers')


def file_fixture(package, destination):
    """Copy payload/metadata for file checks without executing maintainer hooks."""
    destination.mkdir()
    unpacked = destination / 'unpacked'
    subprocess.run(['dpkg-deb', '--raw-extract', str(package), str(unpacked)],
                   check=True, timeout=30)
    for name in ('preinst', 'postinst', 'prerm', 'postrm', 'triggers'):
        (unpacked / 'DEBIAN' / name).unlink(missing_ok=True)
    fixture = destination / 'files-only.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(unpacked), str(fixture)],
                   check=True, timeout=30, stdout=subprocess.DEVNULL)
    return fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old_package', type=Path)
    parser.add_argument('new_package', type=Path)
    parser.add_argument('--old-gnome', type=Path, required=True)
    parser.add_argument('--new-gnome', type=Path, required=True)
    parser.add_argument('--old-tray', type=Path,
                        help='Omit only when testing migration from the former bundled package')
    parser.add_argument('--new-tray', type=Path, required=True)
    parser.add_argument('--files-only', action='store_true',
                        help='Check copied package payloads with hooks/triggers removed; never execute them')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Use a disposable container as root; do not run package-manager fixtures on the host')
    if not (Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()):
        parser.error('This check requires a disposable container')
    with tempfile.TemporaryDirectory(prefix='relayforge-file-fixtures-') as temporary:
        originals = {}
        if args.files_only:
            for name in ('old_package', 'new_package', 'old_gnome', 'new_gnome', 'old_tray', 'new_tray'):
                package = getattr(args, name)
                if package is not None:
                    originals[name] = hashlib.sha256(package.read_bytes()).hexdigest()
                    setattr(args, name, file_fixture(package, Path(temporary) / name))
        check(args, originals)


def check(args, originals):
    for package, expected in ((args.old_package, 'relayforge'), (args.new_package, 'relayforge'),
                               (args.old_gnome, 'relayforge-gnome'), (args.new_gnome, 'relayforge-gnome')):
        audit(package, expected)
    audit(args.new_tray, 'relayforge-tray')
    if args.old_tray:
        audit(args.old_tray, 'relayforge-tray')
    old = package_field(args.old_package, 'Version')
    new = package_field(args.new_package, 'Version')
    subprocess.run(['dpkg', '--compare-versions', new, 'gt', old], check=True)
    assert package_field(args.old_gnome, 'Version') == old
    assert package_field(args.new_gnome, 'Version') == new
    assert package_field(args.new_tray, 'Version') == new
    if args.old_tray:
        assert package_field(args.old_tray, 'Version') == old
    with tempfile.TemporaryDirectory(prefix='relayforge-dpkg-root-') as temporary:
        root = Path(temporary)
        (root / 'var/lib/dpkg').mkdir(parents=True)
        (root / 'var/lib/dpkg/status').touch()
        (root / 'etc').mkdir()
        config = root / 'etc/wifi-hotspot.conf'
        secret = b'# administrator-owned configuration\nSSID=Lifecycle fixture\nPASSPHRASE=private-fixture-only\n'
        config.write_bytes(secret)
        config.chmod(0o600)
        preference = root / 'home/fixture/.config/wifi-hotspot/tray.json'
        preference.parent.mkdir(parents=True)
        preference.write_text('{"visible":false,"future":"preserve"}\n')
        saved_preference = preference.read_bytes()
        def dpkg(*options):
            subprocess.run(['dpkg', '--root=' + str(root), '--force-depends', '--force-confold',
                            *map(str, options)], check=True, timeout=30)
        def preserved():
            assert config.read_bytes() == secret
            assert config.stat().st_mode & 0o777 == 0o600
            assert preference.read_bytes() == saved_preference
        old_packages = [args.old_package, args.old_gnome]
        if args.old_tray:
            old_packages.append(args.old_tray)
        dpkg('--install', *old_packages)
        executable = root / 'usr/lib/relayforge/relayforge-service'
        assert executable.exists()
        desktop_files = [root / 'usr/lib/relayforge/settings' / name
                         for name in ('main.py', 'vendor_main.py')]
        tray_script = root / 'usr/lib/relayforge/settings/tray.py'
        assert tray_script.exists()
        assert all(path.exists() for path in desktop_files)
        extension = root / 'usr/share/gnome-shell/extensions/wifi-relay@3togo.github.io/extension.js'
        assert extension.exists()
        gnome_launcher = root / 'usr/bin/wifi-hotspot-enable-extension'
        assert gnome_launcher.exists()
        gnome_autostart = root / 'etc/xdg/autostart/relayforge-gnome.desktop'
        login_controls = gnome_autostart.exists()
        autostart = root / 'etc/xdg/autostart/relayforge-tray.desktop'
        autostart.write_text('[Desktop Entry]\nType=Application\nHidden=true\n')
        saved_autostart = autostart.read_bytes()
        dpkg('--install', args.new_package, args.new_gnome, args.new_tray)
        assert autostart.read_bytes() == saved_autostart
        preserved()
        # Controls can be removed without removing the service/settings package.
        dpkg('--remove', 'relayforge-gnome')
        assert not extension.exists() and executable.exists()
        assert not gnome_launcher.exists()
        dpkg('--install', args.new_gnome)
        assert extension.exists()
        dpkg('--remove', 'relayforge-tray')
        assert not tray_script.exists() and executable.exists()
        assert all(path.exists() for path in desktop_files)
        assert not (root / 'usr/bin/wifi-hotspot-tray').exists()
        assert autostart.read_bytes() == saved_autostart
        dpkg('--install', args.new_tray)
        assert tray_script.exists() and autostart.read_bytes() == saved_autostart
        dpkg('--remove', 'relayforge-gnome', 'relayforge-tray', 'relayforge')
        assert not executable.exists() and not extension.exists()
        assert all(not path.exists() for path in desktop_files)
        assert autostart.exists()
        preserved()
        dpkg('--purge', 'relayforge-gnome', 'relayforge-tray', 'relayforge')
        assert not autostart.exists()
        if login_controls:
            assert not gnome_autostart.exists()
        # Explicitly initialized administrator files are not package conffiles;
        # purge does not silently delete them or per-user preference data.
        preserved()
    print(json.dumps({'file_lifecycle': 'passed', 'old_version': old, 'new_version': new,
                      'original_package_sha256': originals,
                      'maintainer_hooks': 'Excluded from copied fixtures' if originals else 'Refused',
                      'limits': 'Disposable dpkg root with dependency forcing; no maintainer-hook execution, runtime activation or dependency certification.'}))


if __name__ == '__main__':
    main()
