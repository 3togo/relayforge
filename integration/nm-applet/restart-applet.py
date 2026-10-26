#!/usr/bin/python3
"""Reload an old nm-applet binary after the patched deb is configured."""
import os
from pathlib import Path
import pwd
import signal
import subprocess
import sys
import time

APPLET = Path('/usr/bin/nm-applet')
ENV_KEYS = ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR',
            'DBUS_SESSION_BUS_ADDRESS', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE',
            'XDG_DATA_DIRS', 'XDG_CONFIG_HOME', 'LANG')


def stale_applet(pid, current_inode, proc_root=Path('/proc')):
    process = proc_root / str(pid)
    try:
        executable = os.readlink(process / 'exe')
        return (executable in (str(APPLET), str(APPLET) + ' (deleted)')
                and (process / 'exe').stat().st_ino != current_inode)
    except OSError:
        return False


def desktop_environment(pid, account, proc_root=Path('/proc')):
    process = proc_root / str(pid)
    try:
        if process.stat().st_uid != account.pw_uid:
            return None
        raw = (process / 'environ').read_bytes()[:131072]
        values = dict(item.split(b'=', 1) for item in raw.split(b'\0')
                      if b'=' in item)
        environment = {key: values[key.encode()].decode('utf-8', 'surrogateescape')
                       for key in ENV_KEYS if key.encode() in values}
    except (OSError, ValueError):
        return None
    if (not (environment.get('DISPLAY') or environment.get('WAYLAND_DISPLAY'))
            or not environment.get('DBUS_SESSION_BUS_ADDRESS')):
        return None
    environment['HOME'] = account.pw_dir
    environment['PATH'] = '/usr/bin:/bin'
    environment.setdefault('XDG_RUNTIME_DIR', f'/run/user/{account.pw_uid}')
    return environment


def restart_old_applets():
    if os.geteuid() != 0:
        raise PermissionError('The package hook must run as root.')
    current_inode = APPLET.stat().st_ino
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        pid = int(process.name)
        if not stale_applet(pid, current_inode):
            continue
        try:
            account = pwd.getpwuid(process.stat().st_uid)
        except (OSError, KeyError):
            continue
        if account.pw_uid == 0:
            continue
        environment = desktop_environment(pid, account)
        if environment is None:
            continue
        # Recheck just before signalling in case the PID was reused.
        if not stale_applet(pid, current_inode):
            continue
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not process.exists():
                break
            time.sleep(0.1)
        if process.exists():
            print(f'Wi-Fi Relay: nm-applet {pid} did not exit; restart it at next login.', file=sys.stderr)
            continue
        command = ['/usr/sbin/runuser', '-u', account.pw_name, '--',
                   '/usr/bin/env', '-i', *(f'{key}={value}' for key, value in environment.items()),
                   str(APPLET)]
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


if __name__ == '__main__':
    try:
        restart_old_applets()
    except (OSError, PermissionError, subprocess.SubprocessError) as error:
        print(f'Wi-Fi Relay: could not reload nm-applet: {error}', file=sys.stderr)
