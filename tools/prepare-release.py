#!/usr/bin/env python3
"""Build and validate a committed candidate; never publish or claim hardware qualification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(command, cwd):
    environment = dict(os.environ, TMPDIR=str(cwd.parent),
                       CARGO_TARGET_DIR=str(cwd / 'target'))
    subprocess.run(command, cwd=cwd, check=True, umask=0o022, env=environment)


def prepare(repository, revision, output, version=None):
    scratch = Path.home() / 'git/temp'
    scratch.mkdir(parents=True, exist_ok=True)
    check_scratch(scratch)
    output = output.resolve()
    output.mkdir()  # Preserve existing candidates, including failed runs.
    with tempfile.TemporaryDirectory(prefix='relayforge-release-', dir=scratch) as temporary:
        work = Path(temporary)
        run([sys.executable, str(repository / 'tools/export-source.py'),
             '--revision', revision, '--output', str(output / 'source.tar.gz')], work)
        source = json.loads((output / 'source.tar.gz.json').read_text())
        with tarfile.open(output / 'source.tar.gz') as archive:
            archive.extractall(work, filter='data')
        root = work / source['archive_prefix'].rstrip('/')
        validation = output / 'validation'
        run(['xvfb-run', '-a', sys.executable, 'tools/check-offline.py',
             '--with-applet', '--output-dir', str(validation)], root)
        reports = list(validation.glob('offline-*/validation.json'))
        if len(reports) != 1:
            raise ValueError('Expected one full validation report')
        report = json.loads(reports[0].read_text())
        validate_report(report)
        binary = root / 'target/offline-checks' / report['build_source_id'] / 'release/relayforge-service'
        for backend in ('networkmanager', 'create_ap'):
            command = [sys.executable, 'tools/build-deb.py', '--binary', str(binary),
                       '--backend', backend, '--with-gnome', '--output-dir', str(output / backend)]
            if version:
                command += ['--version', version]
            run(command, root)
        files = sorted(path for path in output.rglob('*') if path.is_file())
        receipt = {'source': source, 'binary_sha256': digest(binary),
                   'files': {str(path.relative_to(output)): digest(path) for path in files},
                   'qualification': 'Offline checks and package assembly only. Installed-system and physical qualification required for this exact candidate.'}
        (output / 'candidate.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
        return receipt


def check_scratch(path):
    for ancestor in (path.resolve(), *path.resolve().parents):
        stat = ancestor.stat()
        sticky_root = stat.st_uid == 0 and stat.st_mode & 0o1000
        if stat.st_uid not in (0, os.getuid()) or (stat.st_mode & 0o022 and not sticky_root):
            raise ValueError(f'Unsafe scratch ancestor {ancestor}: deployment fixtures require trusted ownership and no group/world write permission')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_report(report):
    if report.get('full_python_suite') is not True:
        raise ValueError('Partial Python coverage cannot qualify a candidate')
    checks = report.get('checks', {})
    required = {'build', 'rust-tests', 'desktop-contracts', 'vendor-provenance',
                'integration-provenance', 'applet-optimized', 'applet-sanitized'}
    if not required.issubset(checks) or not any(name.startswith('test_') for name in checks):
        raise ValueError('Missing required offline checks')
    for name, check in checks.items():
        if check.get('exit_code') != 0:
            raise ValueError('Failed offline check: ' + name)
        # unittest reports skips on its final OK line, even with exit status zero.
        if any('skipped=' in line for line in check.get('summary', [])):
            raise ValueError('Skipped coverage requires review: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', help='Explicit Debian candidate version')
    args = parser.parse_args()
    try:
        prepare(ROOT, args.revision, args.output, args.version)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'prepare-release: {error}\n')
    print(args.output.resolve() / 'candidate.json')


if __name__ == '__main__':
    main()
