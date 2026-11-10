#!/usr/bin/env python3
"""Build a native RelayForge Debian package without installing or starting it."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
import gzip

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_PACKAGES = {
    'nm-applet': ('network-manager-applet', '1.36.0-4ubuntu1+relayforge1'),
    'gnome-settings': ('gnome-control-center', '1:51.0-1ubuntu1+relayforge1'),
}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def put(root, name, content, mode=0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def copy(root, name, source):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, path)
    path.chmod(0o644)


def elf_arch(binary):
    with binary.open('rb') as handle:
        header = handle.read(20)
    if len(header) != 20 or header[:4] != b'\x7fELF' or header[5] not in (1, 2):
        raise ValueError('Expected a Linux ELF executable')
    machine = int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big')
    arches = {(62, 2, 1): 'amd64', (3, 1, 1): 'i386', (183, 2, 1): 'arm64',
              (243, 2, 1): 'riscv64', (21, 2, 1): 'ppc64el', (22, 2, 2): 's390x'}
    try:
        return arches[(machine, header[4], header[5])]
    except KeyError as error:
        raise ValueError('Unsupported ELF architecture; do not guess a Debian ABI') from error


def gnome_metadata():
    metadata = json.loads((ROOT / 'vendor/wifi-relay/extension/metadata.json').read_text())
    uuid = metadata['uuid']
    versions = [int(version) for version in metadata['shell-version']]
    if (not re.fullmatch(r'[A-Za-z0-9_.@-]+', uuid) or not versions
            or sorted(versions) != list(range(min(versions), max(versions) + 1))):
        raise ValueError('Review GNOME extension identity and supported Shell versions')
    return uuid, min(versions), max(versions) + 1


def build_gnome(work, version, target, login_controls=False):
    """Build optional controls without enabling the extension or contacting Shell."""
    uuid, minimum, upper = gnome_metadata()
    payload = work / 'gnome/relayforge-gnome'
    for source in (ROOT / 'vendor/wifi-relay/extension').iterdir():
        if source.is_file() and source.suffix in ('.js', '.json', '.css'):
            copy(payload, f'usr/share/gnome-shell/extensions/{uuid}/{source.name}', source)
    metadata = json.loads((ROOT / 'vendor/wifi-relay/extension/metadata.json').read_text())
    metadata.update(description='Hotspot status, failure explanations and Quick Settings controls for RelayForge.',
                    url='https://github.com/3togo/relayforge')
    put(payload, f'usr/share/gnome-shell/extensions/{uuid}/metadata.json',
        json.dumps(metadata, indent=2) + '\n')
    copy(payload, f'usr/share/gnome-shell/extensions/{uuid}/extension.js', ROOT / 'desktop/extension.js')
    put(payload, 'usr/bin/wifi-hotspot-enable-extension',
        '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/relayforge/settings/enable-extension.py "$@"\n', 0o755)
    copy(payload, 'usr/share/man/man1/wifi-hotspot-enable-extension.1', ROOT / 'packaging/man/wifi-hotspot-enable-extension.1')
    if login_controls:
        copy(payload, 'etc/xdg/autostart/relayforge-gnome.desktop',
             ROOT / 'vendor/wifi-relay/data/wifi-relay-gnome.desktop')
        put(payload, 'DEBIAN/conffiles', '/etc/xdg/autostart/relayforge-gnome.desktop\n')
    copy(payload, 'usr/share/doc/relayforge-gnome/copyright', ROOT / 'LICENSE')
    put(payload, 'DEBIAN/control',
        f'Package: relayforge-gnome\nVersion: {version}\nSection: gnome\nPriority: optional\n'
        'Architecture: all\nMaintainer: RelayForge contributors <noreply@users.noreply.github.com>\n'
        f'Depends: relayforge (= {version}), gnome-shell (>= {minimum}), gnome-shell (<< {upper}~)\n'
        'Conflicts: gnome-wifi-hotspot-gnome, gnome-wifi-hotspot-bundle\n'
        'Homepage: https://github.com/3togo/relayforge\n'
        'Description: Optional GNOME Shell controls for RelayForge\n'
        ' Quick Settings and panel controls using the shared hotspot service.\n'
        ' Installation never enables the extension or starts sharing.\n')
    for path in payload.rglob('*'):
        if path.is_dir():
            path.chmod(0o755)
    run('dpkg-deb', '--root-owner-group', '--build', str(payload), str(target))


def build_tray(work, version, target):
    """Keep tray-only dependencies and login files in removable controls."""
    payload = work / 'tray/relayforge-tray'
    copy(payload, 'usr/lib/relayforge/settings/tray.py', ROOT / 'desktop/tray.py')
    copy(payload, 'usr/bin/wifi-hotspot-tray', ROOT / 'packaging/wifi-hotspot-tray')
    (payload / 'usr/bin/wifi-hotspot-tray').chmod(0o755)
    copy(payload, 'usr/share/applications/relayforge-tray.desktop',
         ROOT / 'packaging/relayforge-tray.desktop')
    copy(payload, 'etc/xdg/autostart/relayforge-tray.desktop',
         ROOT / 'packaging/relayforge-tray-autostart.desktop')
    autostart = payload / 'etc/xdg/autostart/relayforge-tray.desktop'
    autostart.write_text(autostart.read_text().replace(
        'Exec=wifi-hotspot-enable-extension', 'Exec=wifi-hotspot-tray --session-start').replace(
        'TryExec=wifi-hotspot-tray --session-start', 'TryExec=wifi-hotspot-tray'))
    put(payload, 'DEBIAN/conffiles', '/etc/xdg/autostart/relayforge-tray.desktop\n')
    copy(payload, 'usr/share/man/man1/wifi-hotspot-tray.1', ROOT / 'packaging/man/wifi-hotspot-tray.1')
    copy(payload, 'usr/share/doc/relayforge-tray/copyright', ROOT / 'LICENSE')
    put(payload, 'DEBIAN/control',
        f'Package: relayforge-tray\nVersion: {version}\nSection: net\nPriority: optional\n'
        'Architecture: all\nMaintainer: RelayForge contributors <noreply@users.noreply.github.com>\n'
        f'Depends: relayforge (= {version}), gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1\n'
        f'Breaks: relayforge (<< {version})\nReplaces: relayforge (<= {version})\n'
        'Conflicts: gnome-wifi-hotspot-tray, gnome-wifi-hotspot-bundle\n'
        'Homepage: https://github.com/3togo/relayforge\n'
        'Description: Optional system tray controls for RelayForge\n'
        ' Status, failure guidance and desktop login controls for the shared service.\n'
        ' Installation never starts sharing or changes network settings.\n')
    for path in payload.rglob('*'):
        if path.is_dir():
            path.chmod(0o755)
    run('dpkg-deb', '--root-owner-group', '--build', str(payload), str(target))


def build(args):
    for tool in ('dpkg', 'dpkg-deb', 'dpkg-shlibdeps'):
        if not shutil.which(tool):
            raise ValueError(f'Missing build tool: {tool} (install dpkg-dev)')
    if os.geteuid() == 0:
        raise ValueError('Build as an ordinary user; root is not needed')
    version = args.version or tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']['package']['version']
    run('dpkg', '--validate-version', version)
    if not re.fullmatch(r'[A-Za-z0-9.+:~\-]+', version):
        raise ValueError('Invalid package version')
    if args.binary:
        binary = args.binary.resolve(strict=True)
    else:
        run('cargo', 'build', '--release', '--locked', '-p', 'relayforge-service', cwd=ROOT)
        metadata = json.loads(run('cargo', 'metadata', '--no-deps', '--format-version=1',
                                  '--locked', cwd=ROOT, capture_output=True).stdout)
        binary = Path(metadata['target_directory']) / 'release/relayforge-service'
    arch = elf_arch(binary)
    host_arch = run('dpkg', '--print-architecture', capture_output=True).stdout.strip()
    if arch != host_arch:
        raise ValueError(f'Build on a matching Debian host: binary is {arch}, host is {host_arch}')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    package = output / f'relayforge_{version.replace(":", "%3a")}_{arch}.deb'
    if package.exists() and not args.force:
        raise FileExistsError(f'Refusing to overwrite {package}')
    tray_package = output / f'relayforge-tray_{version.replace(":", "%3a")}_all.deb'
    if tray_package.exists() and not args.force:
        raise FileExistsError(f'Refusing to overwrite {tray_package}')
    gnome_package = output / f'relayforge-gnome_{version.replace(":", "%3a")}_all.deb'
    if args.with_gnome:
        gnome_metadata()
        if gnome_package.exists() and not args.force:
            raise FileExistsError(f'Refusing to overwrite {gnome_package}')
    integrations = (list(DESKTOP_PACKAGES) if args.desktop_integrations == 'both'
                    else [] if args.desktop_integrations == 'none'
                    else [args.desktop_integrations])
    for integration in integrations:
        run('bash', str(ROOT / 'integration' / integration / 'build-deb.sh'),
            env={**os.environ, 'RELAYFORGE_INTEGRATION_OUTPUT': str(output)})
    with tempfile.TemporaryDirectory(prefix='relayforge-deb-') as temporary:
        work = Path(temporary)
        payload = work / 'debian/relayforge'
        payload.parent.mkdir()
        spec = importlib.util.spec_from_file_location('stage_native', ROOT / 'tools/stage-native.py')
        staging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(staging)
        staging.stage(binary, payload, args.backend)
        payload.chmod(0o755)
        (payload / 'manifest.json').unlink()
        service_binary = payload / 'usr/lib/relayforge/relayforge-service'
        # Ship a stripped ELF; an unstripped object fails the Debian archive gate.
        if shutil.which('strip'):
            run('strip', str(service_binary))
            native_helper = payload / 'usr/lib/relayforge/native/create_ap'
            if native_helper.exists():
                run('strip', str(native_helper))
        else:
            print('warning: strip not found; binary left unstripped', file=sys.stderr)
        doc = 'usr/share/doc/relayforge/'
        for name in ('README.md', 'THIRD_PARTY.md'):
            copy(payload, doc + name, ROOT / name)
        copy(payload, doc + 'docs/native-feature-policy.md', ROOT / 'docs/native-feature-policy.md')
        copy(payload, doc + 'docs/native-bridge-design.md', ROOT / 'docs/native-bridge-design.md')
        copy(payload, doc + 'docs/native-driver-security-audit.md', ROOT / 'docs/native-driver-security-audit.md')
        copy(payload, doc + 'docs/native-regulatory-firewall-policy.md', ROOT / 'docs/native-regulatory-firewall-policy.md')
        copy(payload, doc + 'copyright', ROOT / 'vendor/wifi-relay/packaging/licenses/copyright')
        for source in (ROOT / 'vendor/wifi-relay/packaging/licenses').iterdir():
            if source.is_file():
                copy(payload, doc + 'licenses/' + source.name, source)
        # Native Debian packages must ship a gzipped changelog.
        stamp = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        changelog = (
            f'relayforge ({version}) stable; urgency=medium\n'
            '\n'
            '  * Fix AP lifecycle and desktop hotspot integration; harden firewall\n'
            '    persistence (firewalld permanent rules) and AppArmor child confinement.\n'
            '\n'
            f' -- RelayForge contributors <noreply@users.noreply.github.com>  {stamp}\n'
        )
        with open(payload / (doc + 'changelog.gz'), 'wb') as handle:
            with gzip.GzipFile(fileobj=handle, mode='wb', mtime=0) as archive:
                archive.write(changelog.encode())
        (payload / (doc + 'changelog.gz')).chmod(0o644)
        # Maintainer scripts give clean install/upgrade/rollback behavior and
        # load the MAC profile in complain mode.
        for name in ('postinst', 'prerm', 'postrm'):
            source = ROOT / 'packaging' / name
            if source.exists():
                put(payload, 'DEBIAN/' + name, source.read_text(), 0o755)
        # Mandatory access control skeleton (AppArmor), enforced as complain.
        apparmor = ROOT / 'packaging/apparmor'
        if apparmor.is_dir():
            for source in apparmor.iterdir():
                if source.is_file():
                    copy(payload, 'etc/apparmor.d/' + source.name, source)
        for source in (ROOT / 'vendor/wifi-relay/settings').rglob('*'):
            if (source.is_file() and '__pycache__' not in source.parts
                    and source.suffix != '.pyc' and source.name != 'tray.py'):
                copy(payload, 'usr/lib/relayforge/settings/' + str(source.relative_to(ROOT / 'vendor/wifi-relay/settings')), source)
        # Keep the imported snapshot intact; maintained controls live outside it.
        copy(payload, 'usr/lib/relayforge/settings/vendor_main.py', ROOT / 'vendor/wifi-relay/settings/main.py')
        copy(payload, 'usr/lib/relayforge/settings/main.py', ROOT / 'desktop/main.py')
        put(payload, 'usr/lib/relayforge/settings/project.json',
            json.dumps({'name': 'RelayForge', 'version': version}) + '\n')
        put(payload, 'usr/bin/wifi-hotspot-settings',
            '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/relayforge/settings/launcher.py "$@"\n', 0o755)
        for name in ('upstream_main.py', 'device_info.py', 'adapter_limits.py'):
            copy(payload, 'usr/lib/relayforge/settings/' + name, ROOT / 'desktop' / name)
        put(payload, 'DEBIAN/conffiles',
            '/etc/apparmor.d/usr.lib.relayforge.relayforge-service\n')
        copy(payload, 'usr/share/applications/io.github.erhanzeyrek.WifiHotspot.desktop',
             ROOT / 'vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.desktop')
        entry = payload / 'usr/share/applications/io.github.erhanzeyrek.WifiHotspot.desktop'
        entry.write_text(entry.read_text().replace('Name=Wi-Fi Relay\n', 'Name=RelayForge\n'))
        copy(payload, 'usr/share/metainfo/io.github.erhanzeyrek.WifiHotspot.metainfo.xml',
             ROOT / 'packaging/relayforge.metainfo.xml')
        for name, section in (('wifi-hotspot-settings', '1'), ('relayforge-service', '8')):
            copy(payload, f'usr/share/man/man{section}/{name}.{section}',
                 ROOT / f'packaging/{name}.{section}')
        copy(payload, 'usr/share/icons/hicolor/scalable/apps/io.github.erhanzeyrek.WifiHotspot.svg',
             ROOT / 'vendor/wifi-relay/data/icons/hotspot.svg')
        put(work, 'debian/control', 'Source: relayforge\n\nPackage: relayforge\nArchitecture: any\nDescription: Wi-Fi sharing\n')
        shared = run('dpkg-shlibdeps', '-O', '-e' + str(payload / 'usr/lib/relayforge/relayforge-service'), cwd=work, capture_output=True)
        if shared.stderr:
            print(shared.stderr, end='', file=sys.stderr)
        depends = next(line.split('=', 1)[1] for line in shared.stdout.splitlines() if line.startswith('shlibs:Depends='))
        dependencies = [depends] if depends else []
        dependencies += ['dbus', 'polkitd', 'network-manager', 'iw', 'iproute2',
                         'python3 (>= 3.10)', 'python3-gi', 'gir1.2-gtk-4.0', 'gir1.2-adw-1']
        dependencies += [f'{DESKTOP_PACKAGES[name][0]} (= {DESKTOP_PACKAGES[name][1]})'
                         for name in integrations]
        if args.backend == 'create_ap':
            dependencies += ['hostapd', 'dnsmasq-base', 'iptables']
        size = sum(path.stat().st_size for path in payload.rglob('*') if path.is_file())
        put(payload, 'DEBIAN/control',
            f'Package: relayforge\nVersion: {version}\nSection: net\nPriority: optional\n'
            f'Architecture: {arch}\nMaintainer: RelayForge contributors <noreply@users.noreply.github.com>\n'
            f'Installed-Size: {(size + 1023) // 1024}\nDepends: {", ".join(dependencies)}\n'
            f'Recommends: relayforge-tray (= {version})\n'
            'Conflicts: gnome-wifi-hotspot, gnome-wifi-hotspot-bundle\n'
            'Homepage: https://github.com/3togo/relayforge\n'
            'Description: Native Linux Wi-Fi sharing with GTK settings\n'
            f' Rust service using the {args.backend} backend and Wi-Fi Relay desktop controls.\n'
            ' Configuration and service startup are explicit administrator steps.\n')
        # Normalize directory modes even with a private caller umask.
        for path in payload.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
        built = work / 'relayforge.deb'
        run('dpkg-deb', '--root-owner-group', '--build', str(payload), str(built))
        tray_built = work / 'relayforge-tray.deb'
        build_tray(work, version, tray_built)
        gnome_built = work / 'relayforge-gnome.deb'
        if args.with_gnome:
            build_gnome(work, version, gnome_built, args.gnome_login_controls)
        outputs = [(built, package), (tray_built, tray_package)]
        if args.with_gnome:
            outputs.append((gnome_built, gnome_package))
        for built_file, destination_file in outputs:
            if args.force and destination_file.exists():
                handle, temporary = tempfile.mkstemp(dir=destination_file.parent,
                                                     prefix='.relayforge-', suffix='.deb')
                os.close(handle)
                try:
                    shutil.copyfile(built_file, temporary)
                    os.chmod(temporary, 0o644)
                    os.replace(temporary, destination_file)
                except BaseException:
                    Path(temporary).unlink(missing_ok=True)
                    raise
            else:
                with destination_file.open('xb') as destination, built_file.open('rb') as source:
                    shutil.copyfileobj(source, destination)
                destination_file.chmod(0o644)
    print(package)
    print(tray_package)
    if args.with_gnome:
        print(gnome_package)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, help='Use an existing release ELF instead of running Cargo')
    parser.add_argument('--backend', choices=('networkmanager', 'create_ap'), default='networkmanager')
    parser.add_argument('--version', help='Debian version (default: workspace version)')
    parser.add_argument('--with-gnome', action='store_true',
                        help='Also build the optional relayforge-gnome Shell controls package')
    parser.add_argument('--gnome-login-controls', action='store_true',
                        help='With --with-gnome, opt into first-login setup honoring saved startup/disable preferences')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'artifacts/deb')
    parser.add_argument('--force', action='store_true',
                        help='Replace an existing package of the same name atomically')
    parser.add_argument('--desktop-integrations', choices=('none', 'nm-applet', 'gnome-settings', 'both'),
                        default='none', help='Build matching patched desktop packages and require them at installation')
    args = parser.parse_args()
    if args.gnome_login_controls and not args.with_gnome:
        parser.error('--gnome-login-controls requires --with-gnome')
    try:
        build(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, end='', file=sys.stderr)
        parser.exit(1, f'build-deb: {error}\n')


if __name__ == '__main__':
    main()
