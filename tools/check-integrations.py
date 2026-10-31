#!/usr/bin/env python3
"""Check the reviewed optional integration sources without building or installing."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify():
    manifest = json.loads((ROOT / 'integration/manifest.json').read_text())
    for name, record in manifest['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] != 'integration':
            raise ValueError('Unsafe integration manifest path')
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != record['sha256']:
            raise ValueError(f'Integration source differs from the reviewed manifest: {name}')
    expected = {name for name in manifest['files'] if Path(name).suffix in ('.c', '.h', '.patch')}
    actual = {str(path.relative_to(ROOT)) for path in (ROOT / 'integration').rglob('*')
              if path.suffix in ('.c', '.h', '.patch')}
    if actual != expected:
        raise ValueError('Integration patch/source inventory differs from the reviewed manifest')
    return manifest


if __name__ == '__main__':
    manifest = verify()
    print(f"Verified {len(manifest['files'])} integration files from {manifest['revision']}")
