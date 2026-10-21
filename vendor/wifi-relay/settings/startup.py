"""Per-user preference for starting the tray extension at desktop login."""
import os
import logging
from pathlib import Path
try:
    from preferences import preference_path as _preference_path, get_boolean, set_boolean
except ModuleNotFoundError:
    from settings.preferences import preference_path as _preference_path, get_boolean, set_boolean

UUID = "wifi-relay@3togo.github.io"
LEGACY_UUID = "wifi-hotspot@erhanzeyrek"


def migrate_extension(settings, sync):
    """Carry forward enabled/disabled choices without re-enabling a disabled tray."""
    changed = False
    for key in ("enabled-extensions", "disabled-extensions"):
        current = settings.get_strv(key)
        if LEGACY_UUID not in current:
            continue
        updated = []
        for uuid in current:
            uuid = UUID if uuid == LEGACY_UUID else uuid
            if uuid not in updated:
                updated.append(uuid)
        if not settings.set_strv(key, updated):
            raise RuntimeError("GNOME extension settings are not writable")
        changed = True
    if changed:
        sync()


def preference_path():
    return _preference_path('startup.json')


def get_auto_start(path=None):
    return get_boolean(path or preference_path(), 'auto_start')


def is_gnome():
    return "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")


def desktop_integration_available(gnome=None, settings_dir=None, extension_paths=None):
    """Optional controls must exist before exposing or launching startup actions."""
    gnome = is_gnome() if gnome is None else gnome
    settings_dir = Path(settings_dir) if settings_dir is not None else Path(__file__).resolve().parent
    if not gnome:
        return (settings_dir / 'tray.py').is_file()
    if extension_paths is None:
        roots = [Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')]
        roots.extend(Path(root) for root in os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':') if root)
        extension_paths = [root / 'gnome-shell/extensions' / UUID for root in roots]
        extension_paths.append(settings_dir.parent / 'extension')  # source development
    return any((Path(path) / 'metadata.json').is_file() for path in extension_paths)


def launch_tray():
    if not desktop_integration_available(gnome=False):
        raise RuntimeError('Install gnome-wifi-hotspot-tray to enable desktop tray controls')
    from gi.repository import Gio
    Gio.Subprocess.new(["/usr/bin/python3", str(Path(__file__).resolve().parent / "tray.py")],
                       Gio.SubprocessFlags.NONE)


def set_auto_start(enabled, settings, sync, path=None):
    path = path or preference_path()
    original = None
    if settings is not None:
        original = {key: settings.get_strv(key) for key in ('enabled-extensions', 'disabled-extensions')}
    try:
        if settings is not None:
            current = original['enabled-extensions']
            updated = [uuid for uuid in current if uuid not in {UUID, LEGACY_UUID}]
            if enabled:
                updated.append(UUID)
                disabled = original['disabled-extensions']
                if any(uuid in disabled for uuid in (UUID, LEGACY_UUID)) and not settings.set_strv(
                        'disabled-extensions', [uuid for uuid in disabled if uuid not in {UUID, LEGACY_UUID}]):
                    raise RuntimeError('GNOME extension settings are not writable')
            if updated != current and not settings.set_strv('enabled-extensions', updated):
                raise RuntimeError('GNOME extension settings are not writable')
            sync()
        set_boolean(path, 'auto_start', enabled)
    except Exception:
        # Keep the extension choice aligned with the previous saved preference
        # if a disk write or a later GSettings operation fails.
        if original is not None:
            try:
                for key, values in original.items():
                    if settings.get_strv(key) != values and not settings.set_strv(key, values):
                        raise RuntimeError('GNOME extension settings are not writable')
                sync()
            except Exception:
                logging.getLogger(__name__).exception('Unable to restore GNOME startup preference')
        raise
