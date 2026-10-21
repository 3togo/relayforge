#!/usr/bin/python3
"""Private create_ap recovery snapshots and PID-reuse-safe child termination.

Snapshots contain only an allowlisted set of shell values, never credentials or
executable shell text. Recovery emits quoted assignments for the bundled helper.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import stat
import subprocess
import tempfile
import time

RUNTIME = Path('/run/wifi-relay/create-ap')
PROC = Path('/proc')
SYS_NET = Path('/sys/class/net')
FIELDS = frozenset('CONFDIR COMMON_CONFDIR WIFI_IFACE VWIFI_IFACE INTERNET_IFACE '
                   'BRIDGE_IFACE OLD_MACADDR NEW_MACADDR NO_VIRT SHARE_METHOD '
                   'NO_DNS DNS_PORT GATEWAY RUNNING_AS_DAEMON DAEMON_PIDFILE '
                   'ADDED_UNMANAGED RULE_COMMENT BRIDGE_CREATED PHYSICAL_CONFIGURED'.split())
ARRAYS = frozenset({'IP_ADDRS', 'ROUTE_ADDRS'})
DIRECTORY_PATTERN = r'create_ap\.[A-Za-z0-9_.:-]{1,15}\.conf\.[A-Za-z0-9]{8}'


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError('Unsafe legacy recovery record.')
        data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError('Oversized legacy recovery record.')
        return json.loads(data)


def write_private(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def process_identity(pid):
    process = PROC / str(pid)
    # comm can contain spaces and ')'; starttime is field 22, after the final ')'.
    fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ProcessLookupError(pid)
    return {'pid': int(pid), 'starttime': fields[19],
            'cmdline_sha256': hashlib.sha256((process / 'cmdline').read_bytes()).hexdigest(),
            # Replacing an installed executable adds this kernel suffix without
            # changing the running process. It must still be stopped on upgrade.
            'exe': os.readlink(process / 'exe').removesuffix(' (deleted)')}


def same_process(identity):
    try:
        return process_identity(identity['pid']) == identity
    except (FileNotFoundError, ProcessLookupError):
        return False


def record_child(pidfile, expected_executable):
    pid = int(pidfile.read_text().strip())
    if pid <= 1:
        raise ValueError('Invalid child PID.')
    deadline = time.monotonic() + 1
    while True:
        identity = process_identity(pid)
        if Path(identity['exe']).name == expected_executable:
            if expected_executable in {'hostapd', 'dnsmasq', 'haveged'}:
                arguments = (PROC / str(pid) / 'cmdline').read_bytes().split(b'\0')
                if not any(str(pidfile.parent).encode() + b'/' in argument for argument in arguments):
                    raise RuntimeError('Child does not use this instance\'s private configuration.')
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Child executable did not match its PID file.')
        time.sleep(.01)
    write_private(pidfile.with_suffix('.identity'), identity)


def stop_child(record, grace=2):
    identity = read_private(record)
    if not same_process(identity):
        return  # Exited or PID reused: the replacement must never be signalled.
    try:
        descriptor = os.pidfd_open(identity['pid'])
    except ProcessLookupError:
        return
    try:
        if not same_process(identity):
            return
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            if not select.select([descriptor], [], [], grace)[0]:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                if not select.select([descriptor], [], [], grace)[0]:
                    raise RuntimeError('Legacy child did not exit.')
        except ProcessLookupError:
            pass
    finally:
        os.close(descriptor)


def stop_children(directory):
    # A crash can occur between daemonization and recording the child. Recover
    # that narrow window only if the executable and private config path agree.
    expected = {'hostapd': 'hostapd', 'dnsmasq': 'dnsmasq', 'haveged': 'haveged'}
    for pidfile in sorted(directory.glob('*.pid')):
        if not pidfile.with_suffix('.identity').exists() and pidfile.stem in expected:
            try:
                record_child(pidfile, expected[pidfile.stem])
            except (FileNotFoundError, ProcessLookupError):
                pass
    for record in sorted(directory.glob('*.identity')):
        stop_child(record)


def verify_cleanup(directory):
    data = read_private(directory / 'recovery.json')
    fields = data['fields']
    for pidfile in directory.glob('*.pid'):
        if not pidfile.with_suffix('.identity').exists():
            try:
                process_identity(int(pidfile.read_text().strip()))
            except (FileNotFoundError, ProcessLookupError):
                continue
            raise RuntimeError('Unverified legacy child survived cleanup.')
    for record in directory.glob('*.identity'):
        if same_process(read_private(record)):
            raise RuntimeError('Legacy child survived cleanup.')
    virtual = fields.get('VWIFI_IFACE')
    if virtual and (SYS_NET / virtual).exists():
        raise RuntimeError('Legacy AP interface survived cleanup.')
    bridge = fields.get('BRIDGE_IFACE')
    if bridge and bridge != fields.get('INTERNET_IFACE') and (SYS_NET / bridge).exists():
        raise RuntimeError('Legacy bridge survived cleanup.')
    for table in ('filter', 'nat'):
        rules = subprocess.run(['iptables', '-w', '-t', table, '-S'],
                               capture_output=True, text=True, timeout=5, check=True).stdout
        if fields['RULE_COMMENT'] in rules:
            raise RuntimeError('Legacy firewall rules survived cleanup.')


def recover_owned(pidfile, executable):
    for directory in pending_directories(pidfile):
        # Stop verified broadcasters even if a later interface identity check
        # prevents restoring networking that an administrator changed meanwhile.
        stop_children(directory)
        subprocess.run([str(executable), '--recover', str(directory)],
                       check=True, timeout=40, capture_output=True, text=True)
        if directory.exists():
            raise RuntimeError('Legacy cleanup incomplete; recovery record retained.')


def interface_identity(iface):
    net = SYS_NET / iface
    return {'ifindex': int((net / 'ifindex').read_text()),
            'mac': (net / 'address').read_text().strip()}


def save(directory, pid, values):
    fields = {}
    arrays = {name: [] for name in ARRAYS}
    for entry in values:
        key, value = entry.split('=', 1)
        if key.endswith('[]') and key[:-2] in ARRAYS:
            arrays[key[:-2]].append(value)
        elif key in FIELDS:
            fields[key] = value
        else:
            raise ValueError('Unsupported recovery field.')
    if fields.get('CONFDIR') != str(directory):
        raise ValueError('Recovery directory mismatch.')
    try:
        interfaces = read_private(directory / 'recovery.json')['interfaces']
    except FileNotFoundError:
        interfaces = {}
    for key in ('WIFI_IFACE', 'VWIFI_IFACE', 'INTERNET_IFACE', 'BRIDGE_IFACE'):
        iface = fields.get(key)
        if iface:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}', iface):
                raise ValueError('Invalid recovery interface.')
            try:
                interfaces[iface] = interface_identity(iface)
            except FileNotFoundError:
                pass
    write_private(directory / 'recovery.json', {
        'owner': process_identity(pid), 'fields': fields, 'arrays': arrays,
        'interfaces': interfaces})


def snapshot(directory):
    if (directory.parent != RUNTIME or not re.fullmatch(DIRECTORY_PATTERN, directory.name)
            or directory.is_symlink()):
        raise ValueError('Invalid recovery directory.')
    # Every ancestor below /run is service-owned; no user-controlled path is read.
    for path in (RUNTIME.parent, RUNTIME, directory):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise RuntimeError('Unsafe legacy runtime directory.')
    data = read_private(directory / 'recovery.json')
    if (set(data.get('fields', {})) - FIELDS or set(data.get('arrays', {})) - ARRAYS
            or data['fields'].get('CONFDIR') != str(directory)
            or data['fields'].get('COMMON_CONFDIR') != str(RUNTIME / 'create_ap.common.conf')
            or any(not isinstance(value, str) for value in data['fields'].values())
            or any(not isinstance(value, list) or any(not isinstance(item, str) for item in value)
                   for value in data.get('arrays', {}).values())):
        raise ValueError('Invalid recovery snapshot.')
    return data


def pending_directories(pidfile):
    if not RUNTIME.exists():
        return []
    directories = []
    for directory in sorted(RUNTIME.glob('create_ap.*.conf.*')):
        if not (directory / 'recovery.json').exists():
            # An older installed helper cannot provide a trustworthy resource
            # journal. Never turn its surviving service-owned state into a
            # successful Stop just because its shell died. Unmarked historical
            # firewall rules cannot safely be adopted by the new controller.
            try:
                with (directory / 'pid').open('rb') as stream:
                    owner = stream.read(32).strip()
                with Path(pidfile).open('rb') as stream:
                    service_owner = stream.read(32).strip()
            except FileNotFoundError:
                continue
            if owner == service_owner and owner.isdigit():
                raise RuntimeError('Legacy state has no recovery journal; cleanup cannot be verified.')
            continue
        data = snapshot(directory)
        if data['fields'].get('DAEMON_PIDFILE') == str(pidfile) and not same_process(data['owner']):
            directories.append(directory)
    return directories


def validate_interfaces(data):
    fields = data['fields']
    # A journal can precede netdev creation. Accept a newly realized AP only
    # with the MAC recorded before creation; an unrecorded bridge is refused.
    for key in ('VWIFI_IFACE', 'BRIDGE_IFACE'):
        iface = fields.get(key)
        if iface and iface not in data['interfaces']:
            try:
                current = interface_identity(iface)
            except FileNotFoundError:
                continue
            if key != 'VWIFI_IFACE' or current['mac'].lower() != fields.get('NEW_MACADDR', '').lower():
                raise RuntimeError('Unrecorded legacy interface identity; refusing recovery.')
    for iface, expected in data['interfaces'].items():
        try:
            current = interface_identity(iface)
            owned = iface in {fields.get('VWIFI_IFACE'), fields.get('BRIDGE_IFACE')}
            if current['ifindex'] != expected['ifindex'] or (owned and current != expected):
                raise RuntimeError('Legacy interface identity changed; refusing recovery.')
        except FileNotFoundError:
            pass


def assignments(directory):
    data = snapshot(directory)
    if same_process(data['owner']):
        raise RuntimeError('Refusing recovery while the legacy owner is alive.')
    validate_interfaces(data)
    lines = [f'{key}={shlex.quote(value)}' for key, value in data['fields'].items()]
    lines += [f"{key}=({' '.join(shlex.quote(item) for item in values)})"
              for key, values in data['arrays'].items()]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('save', 'load', 'record-child', 'stop-children',
                                          'verify-cleanup', 'check-interfaces', 'recover'))
    parser.add_argument('path', type=Path)
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    if args.action == 'save':
        save(args.path, int(args.values[0]), args.values[1:])
    elif args.action == 'load':
        print(assignments(args.path))
    elif args.action == 'record-child':
        record_child(args.path, args.values[0])
    elif args.action == 'verify-cleanup':
        verify_cleanup(args.path)
    elif args.action == 'check-interfaces':
        validate_interfaces(snapshot(args.path))
    elif args.action == 'recover':
        recover_owned(args.path, Path(__file__).with_name('create_ap'))
    else:
        stop_children(args.path)


if __name__ == '__main__':
    main()
