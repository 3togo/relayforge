#!/usr/bin/env python3
"""Install local RelayForge packages while preserving daemon boot enablement.

Use this for rollback to releases whose maintainer scripts enable the daemon.
It does not preserve uninterrupted sharing or restart sharing after installation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

UNITS = ('relayforge-native.service', 'relayforge-create-ap.service')


def run(*args):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False)


def unit_state(unit):
    result = run('systemctl', 'is-enabled', unit)
    value = result.stdout.strip()
    if (value, result.returncode) not in {('enabled', 0), ('disabled', 1),
                                        ('not-found', 4)}:
        raise RuntimeError(f'Unsupported boot state for {unit}: {value!r}; no automatic conversion')
    return value


def install(packages, receipt):
    if os.geteuid() != 0:
        raise RuntimeError('Run as root on the installation host')
    # Validate all arguments before invoking APT. Only explicit local packages
    # are accepted; APT options, repository expressions and unrelated packages
    # cannot be smuggled into the transaction.
    files = []
    names = set()
    target_units = set()
    for argument in packages:
        path = Path(argument).resolve(strict=True)
        if not path.is_file() or path.suffix != '.deb':
            raise ValueError('Supply local .deb files')
        metadata = run('dpkg-deb', '--field', str(path), 'Package')
        name = metadata.stdout.strip()
        if metadata.returncode or name not in {'relayforge', 'relayforge-tray', 'relayforge-gnome'} or name in names:
            raise ValueError(f'Unexpected or duplicate package: {path.name}')
        names.add(name)
        if name == 'relayforge':
            contents = run('dpkg-deb', '--contents', str(path))
            if contents.returncode:
                raise ValueError('Cannot inspect the base package payload')
            paths = {line.split()[-1] for line in contents.stdout.splitlines() if line.split()}
            target_units = {unit for unit in UNITS
                            if './usr/lib/systemd/system/' + unit in paths}
        files.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    if 'relayforge' not in names:
        raise ValueError('The base relayforge package is required')
    before = {unit: unit_state(unit) for unit in UNITS}
    if all(value == 'not-found' for value in before.values()):
        raise ValueError('This command requires an existing native installation')
    if target_units != {unit for unit, value in before.items() if value != 'not-found'}:
        raise ValueError('Package backend differs from the installed units; use a reviewed migration')
    report = {'before': before, 'packages': files, 'complete': False}
    # Persist the recovery policy before APT can replace this checkout or any
    # installed file. An existing receipt is never overwritten.
    with open(receipt, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        def save():
            stream.seek(0)
            json.dump(report, stream, indent=2)
            stream.write('\n')
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        save()
        try:
            result = run('apt-get', 'install', '-y', '--allow-downgrades',
                         '-o', 'Dpkg::Options::=--force-confold',
                         *(entry['path'] for entry in files))
            print(result.stdout, end='')
            report['apt_exit_code'] = result.returncode
        finally:
            # A failing package hook can still have altered enablement. Restore
            # every unit, even if restoring another unit fails; never stop/start
            # the service as a side effect of restoring its boot policy.
            errors = []
            for unit, wanted in before.items():
                try:
                    actual = unit_state(unit)
                    if wanted == 'not-found':
                        if actual != 'not-found':
                            raise RuntimeError('Backend changed; review its new boot policy')
                    elif actual != wanted:
                        if actual == 'not-found':
                            raise RuntimeError('Original backend unit was removed')
                        operation = 'enable' if wanted == 'enabled' else 'disable'
                        fixed = run('systemctl', operation, unit)
                        if fixed.returncode:
                            raise RuntimeError(fixed.stdout.strip())
                    if unit_state(unit) != wanted:
                        raise RuntimeError('Boot policy verification failed')
                except Exception as error:
                    errors.append(f'{unit}: {error}')
            report['restoration_errors'] = errors
            report['boot_policy_preserved'] = not errors
            report['complete'] = not errors and report.get('apt_exit_code') == 0
            save()
        if not report['complete']:
            raise RuntimeError(f'Installation or boot-policy restoration failed; inspect {receipt}')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', required=True, type=Path,
                        help='New private JSON file recording original policy and verification')
    parser.add_argument('packages', nargs='+', type=Path)
    args = parser.parse_args()
    try:
        install(args.packages, args.receipt)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'{error}\n')
    print('Installed packages; original daemon boot policy verified. Sharing may be idle.')


if __name__ == '__main__':
    main()
