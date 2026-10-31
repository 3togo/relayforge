#!/usr/bin/env python3
"""Destructive qualification inside the marked, disposable QEMU guest only."""
import ast
import json
import os
from pathlib import Path
import subprocess
import time


def run(*args, ok=True, timeout=120):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    print('$', *args, '\n', p.stdout, flush=True)
    if ok and p.returncode:
        raise RuntimeError(f'{args[0]} exited {p.returncode}')
    return p


def verify_local_policy():
    require_guest()
    run('systemctl', 'stop', 'getty@tty1.service')
    code = """import os, subprocess
state = subprocess.check_output(['loginctl','show-session','self','-p','Active','-p','Remote','-p','Seat','-p','Type','-p','Class','-p','TTY'], text=True)
print(state, flush=True)
assert 'Active=yes' in state and 'Remote=no' in state and 'Seat=seat0' in state
for action, expected in [('control', 0), ('manage', 2)]:
    p = subprocess.run(['pkcheck','--action-id','io.github.erhanzeyrek.WifiHotspot.'+action,
                        '--process',str(os.getpid())], text=True, capture_output=True)
    print(action, p.returncode, p.stdout, p.stderr, flush=True)
    assert p.returncode == expected
"""
    result = run('systemd-run', '--wait', '--collect',
               '--unit=relayforge-local-policy-check', '-p', 'User=ubuntu',
               '-p', 'PAMName=login', '-p', 'TTYPath=/dev/tty1',
               '-p', 'StandardInput=tty-force', '-p', 'StandardOutput=file:/home/ubuntu/results/local-policy.txt',
               '-p', 'RuntimeMaxSec=30',
               '--setenv=XDG_SEAT=seat0', '--setenv=XDG_VTNR=1',
               '--setenv=XDG_SESSION_TYPE=tty', '--setenv=XDG_SESSION_CLASS=user',
               '/usr/bin/python3', '-c', code, ok=False)
    print(Path('/home/ubuntu/results/local-policy.txt').read_text(), flush=True)
    if result.returncode:
        raise RuntimeError('Local active-session authorization fixture failed')
    return result


def require_guest():
    if os.geteuid() != 0:
        raise RuntimeError('Guest qualification requires guest root')
    marker = Path('/run/relayforge-validation-guest')
    if not marker.is_file() or marker.read_text().strip() != 'disposable-task18':
        raise RuntimeError('Refusing package operations outside the marked disposable guest')
    if run('systemd-detect-virt').stdout.strip() not in ('kvm', 'qemu'):
        raise RuntimeError('This check requires a disposable QEMU/KVM guest')
    if list(Path('/sys/class/ieee80211').glob('*')):
        raise RuntimeError('Guest must have no Wi-Fi radios')

def main():
    require_guest()
    results = {'os_release': Path('/etc/os-release').read_text(), 'checks': []}
    out = Path('/home/ubuntu/results')
    if out.exists():
        out.rename(out.with_name('results-previous-'+str(time.time_ns())))
    run('install', '-d', '-o', 'ubuntu', '-g', 'ubuntu', str(out))
    previous_config = Path('/etc/wifi-hotspot.conf')
    if previous_config.exists():
        if b'WIFI_IFACE=rftest0\n' not in previous_config.read_bytes():
            raise RuntimeError('Refusing to reset an unknown guest configuration')
        run('systemctl', 'stop', 'relayforge-native.service', 'relayforge-create-ap.service', ok=False)
        run('dpkg', '--purge', 'relayforge', 'relayforge-tray', 'relayforge-gnome')
        previous_config.rename(Path('/root/relayforge-previous-'+str(time.time_ns())+'.conf'))
    baseline = None
    autostart = Path('/etc/xdg/autostart/relayforge-tray.desktop')
    prefs = Path('/home/ubuntu/.config/wifi-hotspot/tray.json')
    def check(name, fn):
        value = fn()
        results['checks'].append(name)
        (out/'report.json').write_text(json.dumps(results, indent=2)+'\n')
        return value
    run('apt-get', 'update', timeout=300)
    run('apt-get', 'install', '-y', '--no-install-recommends', 'gnome-shell',
        'gnome-session', 'dbus-x11', 'mesa-utils', 'libgl1-mesa-dri',
        'xwayland', 'xvfb', 'xauth', 'policykit-1-gnome', timeout=600)
    for series in ('create_ap-1', 'create_ap-2', 'create_ap-1', 'networkmanager-2'):
        packages = sorted(Path('/home/ubuntu/packages', series).glob('*.deb'))
        assert len(packages) == 3
        # Every operation uses apt's actual dependency resolver, including rollback.
        check('apt-'+series, lambda: run('apt-get', 'install', '-y',
              '-o', 'Dpkg::Options::=--force-confold',
              '--allow-downgrades', *map(str, packages), timeout=600))
        if baseline is not None:
            assert Path('/etc/wifi-hotspot.conf').read_bytes() == baseline
            assert autostart.read_text().endswith('Hidden=true\n')
            assert prefs.read_text() == '{"visible":false,"future":"preserve"}\n'
        owner = run('gdbus', 'call', '--system', '--dest', 'org.freedesktop.DBus',
                    '--object-path', '/org/freedesktop/DBus', '--method',
                    'org.freedesktop.DBus.NameHasOwner', 'io.github.erhanzeyrek.WifiHotspot')
        assert 'false' in owner.stdout, 'Package install unexpectedly activated the service'
        results['checks'].append('no-install-activation-'+series)
        run('systemctl', 'daemon-reload')
        if not Path('/etc/wifi-hotspot.conf').exists():
            run('/usr/lib/relayforge/relayforge-service', '--init-config',
                '/etc/wifi-hotspot.conf', '--wifi-interface', 'rftest0')
        config = Path('/etc/wifi-hotspot.conf')
        assert config.stat().st_mode & 0o777 == 0o600
        if baseline is None:
            baseline = config.read_bytes()
            autostart.write_text(autostart.read_text()+'Hidden=true\n')
            prefs.parent.mkdir(parents=True, exist_ok=True)
            prefs.write_text('{"visible":false,"future":"preserve"}\n')
            run('chown', '-R', 'ubuntu:ubuntu', str(prefs.parent.parent))
        if series.startswith('networkmanager'):
            # Backend selection is an explicit administrator operation. Apt must
            # preserve the existing private configuration rather than rewrite it.
            config.write_bytes(config.read_bytes().replace(b'BACKEND=create_ap', b'BACKEND=networkmanager'))
            baseline = config.read_bytes()
        unit = 'relayforge-native' if series.startswith('networkmanager') else 'relayforge-create-ap'
        run('systemctl', 'stop', 'relayforge-native.service', 'relayforge-create-ap.service', ok=False)
        bus = ['gdbus', 'call', '--system', '--dest', 'io.github.erhanzeyrek.WifiHotspot',
               '--object-path', '/io/github/erhanzeyrek/WifiHotspot', '--method']
        status = check('activation-'+series, lambda: run(*bus, 'io.github.erhanzeyrek.WifiHotspot.GetStatus'))
        assert json.loads(ast.literal_eval(status.stdout)[0])['active'] is False
        check('systemd-'+series, lambda: run('systemctl', 'is-active', unit))
        check('root-polkit-'+series, lambda: run(*bus, 'io.github.erhanzeyrek.WifiHotspot.GetConfig'))
        denied = run('runuser', '-u', 'nobody', '--', *bus,
                     'io.github.erhanzeyrek.WifiHotspot.GetConfig', ok=False)
        assert denied.returncode and 'AccessDenied' in denied.stdout
        results['checks'].append('unprivileged-credential-denial-'+series)
        run('systemctl', 'stop', unit)
    check('active-local-password-free-control-policy', verify_local_policy)
    with (out/'desktop-dbus.log').open('w') as buslog:
        monitor = subprocess.Popen(['dbus-monitor', '--system',
            "type='method_call',interface='io.github.erhanzeyrek.WifiHotspot'"],
            stdout=buslog, stderr=subprocess.STDOUT)
        try:
            check('desktop', lambda: run('runuser', '-u', 'ubuntu', '--', 'dbus-run-session',
                                         '--', 'bash', '/home/ubuntu/desktop.sh', timeout=180))
        finally:
            monitor.terminate(); monitor.wait(timeout=5)
    traffic = (out/'desktop-dbus.log').read_text()
    assert 'member=GetStatus' in traffic and 'member=GetCapabilities' in traffic
    assert 'member=GetConfig' not in traffic and 'member=SetConfig' not in traffic
    results['checks'].append('desktop-does-not-request-protected-config')
    run('systemctl', 'stop', 'relayforge-native.service')
    saved = Path('/etc/wifi-hotspot.conf').read_bytes()
    check('companion-remove', lambda: run('apt-get', 'remove', '-y',
                                          'relayforge-tray', 'relayforge-gnome'))
    assert Path('/usr/lib/relayforge/relayforge-service').exists()
    assert not Path('/usr/bin/wifi-hotspot-tray').exists()
    check('companion-reinstall', lambda: run('apt-get', 'install', '-y',
          *map(str, sorted(Path('/home/ubuntu/packages/networkmanager-2').glob('*.deb')))))
    assert autostart.read_text().endswith('Hidden=true\n')
    assert Path('/etc/wifi-hotspot.conf').read_bytes() == saved

    check('remove', lambda: run('apt-get', 'remove', '-y', 'relayforge',
                               'relayforge-tray', 'relayforge-gnome'))
    assert Path('/etc/wifi-hotspot.conf').read_bytes() == saved
    assert prefs.exists()
    assert not Path('/usr/bin/wifi-hotspot-tray').exists()
    check('purge', lambda: run('dpkg', '--purge', 'relayforge',
                              'relayforge-tray', 'relayforge-gnome'))
    assert Path('/etc/wifi-hotspot.conf').read_bytes() == saved
    assert prefs.exists()
    assert not autostart.exists()
    assert not Path('/usr/share/gnome-shell/extensions/wifi-relay@3togo.github.io/extension.js').exists()
    results['checks'].append('explicit-config-preserved')
    results['versions'] = run('dpkg-query', '-W', '-f=${Package} ${Version}\n',
                              'systemd', 'dbus', 'polkitd', 'gnome-shell',
                              'libgtk-4-1', 'libgtk-3-0t64', 'python3').stdout
    results['complete'] = True
    (out/'report.json').write_text(json.dumps(results, indent=2)+'\n')


if __name__ == '__main__':
    main()
