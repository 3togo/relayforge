#!/usr/bin/env python3
"""Launch the desktop's network GUI, with one shared Relay editor as fallback."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

INTEGRATION_MARKER = Path('/usr/share/gnome-control-center/wifi-relay-entrypoint')


def network_settings_available(desktop=None, marker=INTEGRATION_MARKER, find=shutil.which):
    """Only redirect to a Wi-Fi panel that actually offers Relay controls."""
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '') if desktop is None else desktop
    if 'GNOME' not in desktop.upper().split(':'):
        return False
    try:
        integrated = marker.read_text(encoding='utf-8').strip() == '1'
    except (OSError, UnicodeError):
        return False
    return integrated and find('gnome-control-center') is not None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--standalone', action='store_true',
                        help='Open Relay controls directly (used by the network GUI)')
    parser.add_argument('--about', action='store_true',
                        help='Show About Wi-Fi Relay without opening network settings')
    args = parser.parse_args(argv)
    if not args.about and not args.standalone and network_settings_available():
        # A failing integrated panel must not silently open a competing editor.
        try:
            return subprocess.run(['gnome-control-center', 'wifi'], check=False).returncode
        except OSError as error:
            print(f'Unable to open GNOME Wi-Fi settings: {error}', file=sys.stderr)
            return 1
    # Import GTK only for the editor; opening GNOME Settings needs no GTK process.
    try:
        from main import HotspotSettingsApp, HotspotAboutApp
    except ModuleNotFoundError:
        from settings.main import HotspotSettingsApp, HotspotAboutApp
    app = HotspotAboutApp() if args.about else HotspotSettingsApp()
    return app.run([sys.argv[0]])


if __name__ == '__main__':
    sys.exit(main())
