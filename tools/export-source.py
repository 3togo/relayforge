#!/usr/bin/env python3
"""Export a committed RelayForge revision without Git history or workspace files."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def git(repository, *arguments):
    return subprocess.check_output(['git', '-C', str(repository), *arguments])


def export(repository, revision, output):
    commit = git(repository, 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}').decode().strip()
    tree = git(repository, 'rev-parse', commit + '^{tree}').decode().strip()
    version = tomllib.loads(git(repository, 'show', commit + ':Cargo.toml').decode())['workspace']['package']['version']
    if not re.fullmatch(r'[A-Za-z0-9.+~_-]+', version):
        raise ValueError('Unsafe workspace version for archive prefix')
    # Only a committed tree is eligible. Untracked/ignored/dirty workspace files
    # are never read. Reject accidentally tracked build products rather than
    # silently excluding files from the claimed source revision.
    names = git(repository, 'ls-tree', '-r', '--name-only', '-z', commit).decode().split('\0')
    forbidden = {'target', 'artifacts', '__pycache__', '.git'}
    for name in filter(None, names):
        path = Path(name)
        if forbidden.intersection(path.parts) or path.suffix == '.pyc':
            raise ValueError('Committed tree contains a generated or private-workspace path: ' + name)
    output = output.resolve()
    receipt_path = output.with_name(output.name + '.json')
    if output.exists() or receipt_path.exists():
        raise FileExistsError('Refusing to overwrite archive or receipt')
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = 'relayforge-' + version + '/'
    with tempfile.TemporaryDirectory(prefix='relayforge-export-') as temporary:
        raw = Path(temporary) / 'source.tar'
        git(repository, 'archive', '--format=tar', '--prefix=' + prefix,
            '--output=' + str(raw), commit)
        packed = Path(temporary) / 'source.tar.gz'
        with packed.open('wb') as destination:
            with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as archive:
                with raw.open('rb') as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        archive.write(chunk)
        content = packed.read_bytes()
        receipt = {'commit': commit, 'tree': tree, 'workspace_version': version,
                   'archive_prefix': prefix, 'sha256': hashlib.sha256(content).hexdigest(),
                   'bytes': len(content),
                   'scope': 'Committed Git archive only; workspace edits and Git history excluded. Not a secret or license audit.'}
        with output.open('xb') as destination:
            destination.write(content)
        output.chmod(0o644)
        with receipt_path.open('x') as destination:
            destination.write(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
        receipt_path.chmod(0o644)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD', help='Committed revision (default: HEAD; ignores workspace edits)')
    parser.add_argument('--output', required=True, type=Path, help='New .tar.gz output; adjacent JSON receipt is also created')
    args = parser.parse_args()
    if not args.output.name.endswith('.tar.gz'):
        parser.error('--output must end in .tar.gz')
    try:
        print(json.dumps(export(ROOT, args.revision, args.output), indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'export-source: {error}\n')


if __name__ == '__main__':
    main()
