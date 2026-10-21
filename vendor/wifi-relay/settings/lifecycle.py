"""Detect desktop-code upgrades and removals without depending on GTK."""
import time


class CodeRevision:
    def __init__(self, paths):
        self.paths = tuple(paths)
        self.initial = self._snapshot()
        self._missing_since = None

    def _snapshot(self):
        try:
            return tuple((path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size)
                         for path in self.paths)
        except OSError:
            return None

    def changed(self):
        current = self._snapshot()
        # An upgrade can briefly remove a file; wait for the complete set.
        return self.initial is not None and current is not None and current != self.initial

    def removed(self, now=None, grace_seconds=10):
        """Allow brief upgrade gaps, then exit a removed desktop integration."""
        now = time.monotonic() if now is None else now
        if self._snapshot() is not None:
            self._missing_since = None
            return False
        if self._missing_since is None:
            self._missing_since = now
        return now - self._missing_since >= grace_seconds


def replace_tray_instance(bus_name, script, timeout=5):
    """Retire an older tray GUI as its user, without invoking hotspot Stop."""
    import os
    import select
    import signal
    from pathlib import Path
    from gi.repository import Gio, GLib

    if os.geteuid() == 0:
        raise PermissionError('Tray replacement must run as the desktop user')
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(method, value):
        return bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                             'org.freedesktop.DBus', method,
                             GLib.Variant('(s)', (value,)), None,
                             Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]

    descriptor = None
    try:
        if not call('NameHasOwner', bus_name):
            return
        owner = call('GetNameOwner', bus_name)
        if call('GetConnectionUnixUser', owner) != os.geteuid():
            raise PermissionError('Existing tray belongs to a different user')
        pid = call('GetConnectionUnixProcessID', owner)
        if pid == os.getpid():
            return
        descriptor = os.pidfd_open(pid)
        arguments = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if len(arguments) < 2 or Path(os.fsdecode(arguments[1])).resolve() != Path(script).resolve():
            raise PermissionError('Existing application is not the installed tray')
        # Recheck the unique bus owner after opening the process descriptor.
        # The descriptor targets the original process even if its PID is reused.
        if call('GetNameOwner', bus_name) != owner:
            return
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        if not select.select([descriptor], [], [], timeout)[0]:
            raise TimeoutError('The previous tray did not close; try starting it again')
    except (ProcessLookupError, FileNotFoundError):
        pass  # The previous instance already exited during upgrade.
    except GLib.Error:
        if call('NameHasOwner', bus_name):
            raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
