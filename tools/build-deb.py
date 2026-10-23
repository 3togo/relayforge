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

ROOT = Path(__file__).resolve().parents[1]


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
    if package.exists():
        raise FileExistsError(f'Refusing to overwrite {package}')
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
        doc = 'usr/share/doc/relayforge/'
        for name in ('README.md', 'THIRD_PARTY.md'):
            copy(payload, doc + name, ROOT / name)
        copy(payload, doc + 'copyright', ROOT / 'vendor/wifi-relay/packaging/licenses/copyright')
        for source in (ROOT / 'vendor/wifi-relay/packaging/licenses').iterdir():
            if source.is_file():
                copy(payload, doc + 'licenses/' + source.name, source)
        for source in (ROOT / 'vendor/wifi-relay/settings').rglob('*'):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix != '.pyc':
                copy(payload, 'usr/lib/relayforge/settings/' + str(source.relative_to(ROOT / 'vendor/wifi-relay/settings')), source)
        # Keep the imported snapshot intact; ship RelayForge's maintained tray.
        copy(payload, 'usr/lib/relayforge/settings/tray.py', ROOT / 'desktop/tray.py')
        put(payload, 'usr/bin/wifi-hotspot-settings',
            '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/relayforge/settings/launcher.py "$@"\n', 0o755)
        copy(payload, 'usr/bin/wifi-hotspot-tray', ROOT / 'packaging/wifi-hotspot-tray')
        (payload / 'usr/bin/wifi-hotspot-tray').chmod(0o755)
        copy(payload, 'usr/share/applications/relayforge-tray.desktop',
             ROOT / 'packaging/relayforge-tray.desktop')
        copy(payload, 'etc/xdg/autostart/relayforge-tray.desktop',
             ROOT / 'packaging/relayforge-tray-autostart.desktop')
        put(payload, 'DEBIAN/conffiles', '/etc/xdg/autostart/relayforge-tray.desktop\n')
        copy(payload, 'usr/share/applications/io.github.erhanzeyrek.WifiHotspot.desktop',
             ROOT / 'vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.desktop')
        copy(payload, 'usr/share/icons/hicolor/scalable/apps/io.github.erhanzeyrek.WifiHotspot.svg',
             ROOT / 'vendor/wifi-relay/data/icons/hotspot.svg')
        put(work, 'debian/control', 'Source: relayforge\n\nPackage: relayforge\nArchitecture: any\nDescription: Wi-Fi sharing\n')
        shared = run('dpkg-shlibdeps', '-O', '-e' + str(payload / 'usr/lib/relayforge/relayforge-service'), cwd=work, capture_output=True)
        if shared.stderr:
            print(shared.stderr, end='', file=sys.stderr)
        depends = next(line.split('=', 1)[1] for line in shared.stdout.splitlines() if line.startswith('shlibs:Depends='))
        dependencies = [depends] if depends else []
        dependencies += ['dbus', 'polkitd', 'network-manager', 'iw', 'iproute2',
                         'python3 (>= 3.10)', 'python3-gi', 'gir1.2-gtk-4.0', 'gir1.2-adw-1',
                         'gir1.2-gtk-3.0', 'gir1.2-ayatanaappindicator3-0.1']
        if args.backend == 'create_ap':
            dependencies += ['hostapd', 'dnsmasq-base', 'iptables']
        size = sum(path.stat().st_size for path in payload.rglob('*') if path.is_file())
        put(payload, 'DEBIAN/control',
            f'Package: relayforge\nVersion: {version}\nSection: net\nPriority: optional\n'
            f'Architecture: {arch}\nMaintainer: RelayForge contributors <noreply@users.noreply.github.com>\n'
            f'Installed-Size: {(size + 1023) // 1024}\nDepends: {", ".join(dependencies)}\n'
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
        # Exclusive creation also protects against another build finishing first.
        with package.open('xb') as destination, built.open('rb') as source:
            shutil.copyfileobj(source, destination)
        package.chmod(0o644)
    print(package)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, help='Use an existing release ELF instead of running Cargo')
    parser.add_argument('--backend', choices=('networkmanager', 'create_ap'), default='networkmanager')
    parser.add_argument('--version', help='Debian version (default: workspace version)')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'artifacts/deb')
    args = parser.parse_args()
    try:
        build(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, end='', file=sys.stderr)
        parser.exit(1, f'build-deb: {error}\n')


if __name__ == '__main__':
    main()
