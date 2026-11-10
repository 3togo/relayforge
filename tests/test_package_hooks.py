"""Exercise service lifecycle hooks with command doubles, never host services."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
UNITS = ('relayforge-native.service', 'relayforge-create-ap.service')


class PackageHookTests(unittest.TestCase):
    def hook_calls(self, hook, *arguments):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            log = work / 'commands.jsonl'
            command = work / 'command'
            command.write_text(
                '#!/usr/bin/python3\n'
                'import json, os, sys\n'
                'with open(os.environ["HOOK_LOG"], "a") as log:\n'
                '    log.write(json.dumps(sys.argv) + "\\n")\n'
                '# Simulate disabled and inactive units.\n'
                'raise SystemExit(1 if any(a in ("is-enabled", "is-active") '
                'for a in sys.argv[1:]) else 0)\n')
            command.chmod(0o755)
            for name in ('systemctl', 'deb-systemd-helper', 'deb-systemd-invoke',
                         'aa-complain', 'apparmor_parser'):
                (work / name).symlink_to(command)
            subprocess.run(['/bin/sh', str(ROOT / 'packaging' / hook), *arguments],
                           env=dict(os.environ, PATH=str(work), HOOK_LOG=str(log)),
                           check=True, timeout=5)
            return [[Path(args[0]).name, *args[1:]]
                    for args in map(json.loads, log.read_text().splitlines())]

    def test_install_and_upgrade_preserve_disabled_services(self):
        for arguments in (('configure', ''), ('configure', '0.1.1'),
                          ('abort-upgrade', '0.1.1')):
            with self.subTest(arguments=arguments):
                calls = self.hook_calls('postinst', *arguments)
                for call in calls:
                    self.assertFalse(set(call[1:]) & {'enable', 'start', 'restart'}, call)

    def test_removal_stops_both_backends(self):
        calls = self.hook_calls('prerm', 'remove')
        stopped = {arg for call in calls if call[0] == 'deb-systemd-invoke'
                   and call[1] == 'stop' for arg in call[2:]}
        self.assertEqual(stopped, set(UNITS))

    def test_purge_forgets_both_backends(self):
        calls = self.hook_calls('postrm', 'purge')
        purged = {arg for call in calls if call[:2] == ['deb-systemd-helper', 'purge']
                  for arg in call[2:]}
        self.assertEqual(purged, set(UNITS))
