"""Compare native Rust schema against the immutable Python reference, without GI.

The example executable is a test adapter; production does not invoke this module.
"""
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
ORACLE = Path(os.environ.get('RELAYFORGE_CONFIG_ORACLE', ROOT / 'target/debug/examples/config_oracle'))
spec = importlib.util.spec_from_file_location('reference_configuration', ROOT / 'vendor/wifi-relay/daemon/configuration.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


class ConfigurationParityTests(unittest.TestCase):
    def test_all_configuration_fields_and_cross_field_cases(self):
        cases = [{}, reference.CONFIG_DEFAULTS, {'SSID': None}, {'HIDDEN': True}, {'WIFI_IFACE': 12},
                 {'USE_PSK': '1', 'PASSPHRASE': 'a' * 64}, {'USE_PSK': '1'},
                 {'USE_PSK': '0', 'PASSPHRASE': 'a' * 64},
                 {'SSID': '测' * 10}, {'SSID': '测' * 11},
                 {'PASSPHRASE': " leading literal =#'\\$() trailing "}]
        values = ['', '0', '1', '2', '3', '5', 'auto', '2.4', 'default',
                  '001', '196', '197', '-1', '999', 'wlp2s0', 'wlan0:1', '--help',
                  '192.168.12.1', '127.0.0.1', '224.1.2.3', '0.0.0.0',
                  '192.168.12.255', '192.168.12.0', '01.2.3.4', '192.168.1.2,8.8.8.8',
                  '/etc/hostapd/accept', '/etc/hostapd/../passwd', '/etc/hostapd/',
                  'CN', 'cn', 'password', 'a' * 63, 'a' * 64, 'b' * 65,
                  'x\nINJECT=1', 'nonbreaking\u00a0space', 'zero\u200bwidth', 'private\ue000',
                  'unassigned\u0378', 'line\u2028separator', 'nul\0', '界', 'a' * 4097]
        for key in [*reference.CONFIG_DEFAULTS, 'COUNTRY', 'UNKNOWN']:
            cases.extend({key: value} for value in values)
        rng = random.Random(314159)
        for _ in range(250):
            config = {key: rng.choice(values) for key in rng.sample(list(reference.CONFIG_DEFAULTS), 3)}
            cases.append(config)
        payload = ''.join(json.dumps({'config': case}) + '\n' for case in cases)
        completed = subprocess.run([str(ORACLE)], input=payload, text=True, capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        actual = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(actual), len(cases))
        for index, (case, result) in enumerate(zip(cases, actual)):
            try:
                validated = reference.validate_config(case)
            except (ValueError, TypeError):
                self.assertFalse(result['valid'], f'case {index}, keys {list(case)}')
            else:
                self.assertTrue(result['valid'], f'case {index}, keys {list(case)}')
                self.assertEqual(result['config'], validated)

    def test_unicode_printability_matches_python_for_every_scalar(self):
        completed = subprocess.run([str(ORACLE), '--printable-bitmap'], capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0)
        expected = bytes(int(chr(n).isprintable()) for n in range(0x110000))
        self.assertEqual(len(completed.stdout), len(expected))
        mismatches = [hex(index) for index, (a, b) in enumerate(zip(completed.stdout, expected)) if a != b]
        self.assertEqual(mismatches[:30], [], f'{len(mismatches)} Unicode classification differences')

    def test_legacy_file_syntax_is_normalized_without_shell_evaluation(self):
        texts = ['# comment\r\nSSID=Old\nSSID=New\r\nPASSPHRASE=literal=#\\$() password\n',
                 ' WIFI_IFACE =wlp2s0\nINTERNET_IFACE=eth0\n',
                 '\n# retained defaults\n', 'USE_PSK=1\nPASSPHRASE=' + 'f' * 64 + '\n']
        payload = ''.join(json.dumps({'file': text}) + '\n' for text in texts)
        result = subprocess.run([str(ORACLE)], input=payload, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text, line in zip(texts, result.stdout.splitlines()):
            parsed = {}
            for record in text.split('\n'):
                record = record.rstrip('\r\n')
                if record and not record.lstrip().startswith('#') and '=' in record:
                    key, value = record.split('=', 1)
                    parsed[key.strip()] = value
            self.assertEqual(json.loads(line)['config'], reference.validate_config(parsed))
