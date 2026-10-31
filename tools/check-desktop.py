#!/usr/bin/env python3
"""Run retained desktop regressions on a private bus, without host networking.

GTK4 settings and GTK3 tray tests must run in separate processes.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / 'tests/desktop/manifest.json').read_text())
    for name, record in manifest['files'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != record['sha256']:
            raise ValueError(f'Review imported desktop test changes before running: {name}')
    environment = os.environ.copy()
    environment.update(PYTHONPATH=os.pathsep.join((str(ROOT / 'desktop'),
                                                 str(ROOT / 'vendor/wifi-relay'))),
                       GIO_USE_VFS='local', NO_AT_BRIDGE='1')
    # Remap the system bus inside the new session before importing the tests.
    # This also prevents an accidental future regression test from reaching the
    # installed hotspot service through Gio's default system-bus connection.
    runner = (
        "import os,unittest; "
        "os.environ['DBUS_SYSTEM_BUS_ADDRESS']=os.environ['DBUS_SESSION_BUS_ADDRESS']; "
        "suite=unittest.defaultTestLoader.discover('tests/desktop',pattern='test_*.py'); "
        "result=unittest.TextTestRunner(verbosity=2).run(suite); "
        "raise SystemExit(not result.wasSuccessful())"
    )
    subprocess.run(['dbus-run-session', '--', sys.executable, '-c', runner], cwd=ROOT,
                   env=environment, timeout=60, check=True)
    subprocess.run(['node', 'tests/desktop/test_tray.mjs'], cwd=ROOT,
                   env=environment, timeout=30, check=True)


if __name__ == '__main__':
    main()
