"""Verify installed assets and exercise the installed service on a private bus."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

variant = sys.argv[1]
service = Path('/usr/lib/relayforge/relayforge-service')
helper = Path('/usr/lib/relayforge/native/create_ap')
assert service.is_file() and os.access(service, os.X_OK)
assert hashlib.sha256(service.read_bytes()).digest() == hashlib.sha256(helper.read_bytes()).digest()
settings = Path('/usr/lib/relayforge/settings')
for name in ('main.py', 'device_info.py', 'adapter_limits.py', 'tray.py'):
    source = settings / name
    assert source.stat().st_mode & 0o777 == 0o644
    compile(source.read_text(), str(source), 'exec')
assert Path('/usr/share/gnome-shell/extensions/wifi-relay@3togo.github.io/extension.js').is_file()
if variant == 'desktop-integrated':
    assert Path('/usr/share/gnome-control-center/wifi-relay-entrypoint').read_text().strip() == '1'
    assert subprocess.check_output(['dpkg-query','-W','-f=${Version}','network-manager-applet'],text=True) == '1.36.0-4ubuntu1+relayforge1'
    assert subprocess.check_output(['dpkg-query','-W','-f=${Version}','gnome-control-center'],text=True) == '1:51.0-1ubuntu1+relayforge1'
else:
    assert not Path('/usr/share/gnome-control-center/wifi-relay-entrypoint').exists()
subprocess.run(['dbus-run-session', '--', 'python3', '-c', r'''
import json, subprocess, time
from gi.repository import Gio
p = subprocess.Popen(['/usr/lib/relayforge/relayforge-service','--demo'])
try:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION,None)
    def call(name):
        value = bus.call_sync('io.github.erhanzeyrek.WifiHotspot','/io/github/erhanzeyrek/WifiHotspot','io.github.erhanzeyrek.WifiHotspot',name,None,None,0,5000,None).unpack()[0]
        return json.loads(value) if isinstance(value,str) else value
    deadline = time.monotonic() + 10
    while True:
        try:
            assert not call('GetStatus')['active']
            break
        except Exception:
            if p.poll() is not None or time.monotonic() > deadline: raise
            time.sleep(.1)
    assert call('Start')['success']
    assert call('GetStatus')['active']
    assert call('Stop') is True
    assert not call('GetStatus')['active']
finally:
    p.terminate()
    p.wait(timeout=10)
'''], check=True, timeout=30)
