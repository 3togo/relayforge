#!/usr/bin/python3
"""Enable the installed tray extension once, in the logged-in user's session."""
import os
from pathlib import Path
import sys

try:
    from startup import get_auto_start, is_gnome, migrate_extension, desktop_integration_available
except ModuleNotFoundError:
    from settings.startup import get_auto_start, is_gnome, migrate_extension, desktop_integration_available

UUID = "wifi-relay@3togo.github.io"


def enable_on_first_login(settings, marker, sync):
    if not get_auto_start():
        return
    migrate_extension(settings, sync)
    if marker.exists():
        return

    # Preserve an explicit choice made in GNOME Extensions before first login.
    disabled = settings.get_strv("disabled-extensions")
    enabled = settings.get_strv("enabled-extensions")
    if UUID not in disabled and UUID not in enabled:
        if not settings.set_strv("enabled-extensions", [*enabled, UUID]):
            raise RuntimeError("GNOME extension settings are not writable")
        sync()

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def main():
    if not desktop_integration_available() or not get_auto_start():
        return 0
    if not is_gnome():
        os.execv("/usr/bin/python3", ["/usr/bin/python3", str(Path(__file__).resolve().parent / "tray.py")])
    from gi.repository import Gio

    state_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    marker = state_dir / "wifi-hotspot" / "extension-initialized"
    try:
        enable_on_first_login(Gio.Settings.new("org.gnome.shell"), marker, Gio.Settings.sync)
    except Exception as error:
        print(f"[Hotspot] Could not enable tray extension: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
