#!/usr/bin/python3
"""Check that every reused file still matches the pinned source-only snapshot."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'vendor/manifest.json').read_text())
actual = {str(path.relative_to(root / 'vendor/wifi-relay')): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in (root / 'vendor/wifi-relay').rglob('*')
          if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
if actual != manifest['files']:
    raise SystemExit('Vendored snapshot differs from the recorded source manifest')
print(f"Verified {len(actual)} pinned Wi-Fi Relay files at {manifest['revision']}")
