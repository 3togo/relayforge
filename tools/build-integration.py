#!/usr/bin/env python3
"""Build an optional downstream integration from an exact, prepared Ubuntu source.

Copies the source to a fresh directory; never installs packages or changes services.
"""
import argparse
from email.utils import formatdate
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    'nm-applet': {'source': 'network-manager-applet', 'version': '1.36.0-4ubuntu1',
                  'patches': ['relay-menu.patch', 'hotspot-icon.patch'],
                  'exports': ['network-manager-applet', 'network-manager-gnome']},
    'gnome-settings': {'source': 'gnome-control-center', 'version': '1:51.0-1ubuntu1',
                       'patches': ['stable-wifi-device-tabs.patch', 'relay-entrypoint.patch'],
                       'exports': ['gnome-control-center']},
    'nm-core': {'source': 'network-manager', 'version': '1.58.1-1ubuntu3',
                'patches': ['native-relay.patch'], 'exports': ['network-manager', 'libnm0']},
}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def source_identity(source):
    # Read only the authoritative first changelog stanza, never execute source.
    line = (source / 'debian/changelog').read_text().splitlines()[0]
    match = re.fullmatch(r'([a-z0-9+.-]+) \(([^)]+)\) [^;]+;.*', line)
    if not match:
        raise ValueError('Invalid distribution source changelog')
    return match.group(1, 2)


def plan(component, source, version=None):
    definition = COMPONENTS[component]
    if source_identity(source) != (definition['source'], definition['version']):
        raise ValueError(f"{component} requires unmodified source {definition['source']}={definition['version']}")
    version = version or definition['version'] + '+relayforge1'
    if not re.fullmatch(r'[A-Za-z0-9.+:~\-]+', version):
        raise ValueError('Invalid downstream package version')
    subprocess.run(['dpkg', '--validate-version', version], check=True)
    if subprocess.run(['dpkg', '--compare-versions', version, 'gt', definition['version']]).returncode:
        raise ValueError('Downstream version must be newer than the source baseline')
    module('integration_manifest', ROOT / 'tools/check-integrations.py').verify()
    return dict(definition, component=component, downstream_version=version,
                integration_manifest_sha256=hashlib.sha256(
                    (ROOT / 'integration/manifest.json').read_bytes()).hexdigest())


def prepare(definition, source, destination):
    """Mutate only a new copy; patch rejects never alter the supplied source."""
    # Dereference source links into independent files, so patching a copied
    # control/changelog can never follow a link back into the supplied tree.
    shutil.copytree(source, destination, symlinks=False,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    component = definition['component']
    if component in ('nm-applet', 'gnome-settings'):
        helper = module('integration_dependencies', ROOT / 'integration/minimize-dependencies.py')
        control = destination / 'debian/control'
        control.write_text(helper.minimize_control(control.read_text(),
                          'applet' if component == 'nm-applet' else 'settings', definition['version']))
    for patch in definition['patches']:
        with (ROOT / 'integration' / component / patch).open('rb') as stream:
            subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1'],
                           stdin=stream, cwd=destination, check=True)
    if component == 'nm-applet':
        for name in ('wifi-relay.c', 'wifi-relay.h'):
            shutil.copyfile(ROOT / 'integration/nm-applet' / name, destination / 'src' / name)
        # Deliberately retain distribution lifecycle scripts: no custom GUI
        # restart hook is installed by this source builder.
    elif component == 'gnome-settings':
        (destination / 'debian/wifi-relay-entrypoint').write_text('1\n')
        with (destination / 'debian/gnome-control-center.install').open('a') as stream:
            stream.write('\ndebian/wifi-relay-entrypoint usr/share/gnome-control-center\n')
    changelog = destination / 'debian/changelog'
    changelog.write_text(
        f"{definition['source']} ({definition['downstream_version']}) stonking; urgency=medium\n\n"
        '  * Build the reviewed optional RelayForge integration.\n\n'
        ' -- RelayForge contributors <noreply@users.noreply.github.com>  '
        + formatdate(localtime=False) + '\n\n' + changelog.read_text())


def build(args):
    source = args.source_dir.resolve(strict=True)
    definition = plan(args.component, source, args.version)
    if args.plan:
        print(json.dumps(definition, indent=2))
        return
    output = args.output_dir.resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError('Output directory must be outside the supplied source')
    if args.jobs < 1 or args.jobs > 256:
        raise ValueError('Build jobs must be between 1 and 256')
    output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=args.component + '-', dir=output))
    candidate = work / 'source'
    prepare(definition, source, candidate)
    command = ['dpkg-buildpackage', '-b', '-uc', '-us', f'-j{args.jobs}']
    # Retain the build log and prepared source even on failure. Do not alter
    # global proxy settings, packages, networking or service configuration.
    with (work / 'build.log').open('wb') as log:
        subprocess.run(command, cwd=candidate, stdout=log, stderr=subprocess.STDOUT,
                       timeout=args.timeout, check=True)
    version = definition['downstream_version'].split(':')[-1]
    exports = list(work.glob('*.deb')) if args.all_packages else [
        path for package in definition['exports'] for path in work.glob(f'{package}_{version}_*.deb')]
    if not exports or (not args.all_packages and len(exports) != len(definition['exports'])):
        raise ValueError(f'Expected package exports are missing; inspect {work}')
    receipt = dict(definition, packages={})
    for package in exports:
        receipt['packages'][package.name] = hashlib.sha256(package.read_bytes()).hexdigest()
    (work / 'validation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    # Publish only into this fresh build directory; never overwrite old packages.
    print(work)
    for package in exports:
        print(package)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=COMPONENTS)
    parser.add_argument('--source-dir', required=True, type=Path,
                        help='Unpatched, unpacked exact Ubuntu source; copied before patching')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'artifacts/integrations')
    parser.add_argument('--version')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--all-packages', action='store_true')
    parser.add_argument('--plan', action='store_true', help='Validate the baseline and print the build plan only')
    args = parser.parse_args()
    try:
        build(args)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f'build-integration: {error}\n')


if __name__ == '__main__':
    main()
