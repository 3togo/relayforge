"""Differential radio fixtures: no adapter, system bus or network mutation."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
ORACLE = Path(os.environ.get('RELAYFORGE_RADIO_ORACLE', ROOT / 'target/debug/examples/radio_oracle'))
sys.path.insert(0, str(ROOT / 'vendor/wifi-relay/daemon'))
spec = importlib.util.spec_from_file_location('radio_reference', ROOT / 'vendor/wifi-relay/daemon/wifi-hotspot-daemon.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def capture():
    return dict(wifi_iface='demo0', info='Interface demo0\n\ttype managed\n\twiphy 0\n',
                phy='''Wiphy phy0
\tSupported interface modes:
\t\t * managed
\t\t * AP
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412.0 MHz [1] (20.0 dBm)
\t\t\t* 2437 MHz [6] (20.0 dBm)
\t\t\t* 2484 MHz [14] (disabled)
\tBand 2:
\t\tFrequencies:
\t\t\t* 5180 MHz [36] (20.0 dBm) (no IR)
\t\t\t* 5200 MHz [40] (20.0 dBm) (radar detection)
\t\t\t* 5745 MHz [149] (20.0 dBm)
\tvalid interface combinations:
\t\t * #{ managed } <= 1, #{ AP, P2P-client, P2P-GO } <= 1,
\t\t   total <= 3, #channels <= 1
\tHT Capability overrides:
''', link='Not connected.\n')


def native(records):
    result = subprocess.run([str(ORACLE)], input=''.join(json.dumps(r) + '\n' for r in records),
                            text=True, capture_output=True, timeout=10)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [json.loads(line) for line in result.stdout.splitlines()]


def legacy(record):
    # Construct without initialization: no bus registration, probing or service state.
    daemon = reference.WifiHotspotDaemon.__new__(reference.WifiHotspotDaemon)
    daemon._read_config_dict = lambda: {'WIFI_IFACE': record['wifi_iface']}
    daemon._run_cmd = Mock(side_effect=[(0, record[k], '') for k in ('info', 'phy', 'link')])
    return daemon.get_capabilities()


class RadioParityTests(unittest.TestCase):
    def test_complete_capability_contract_matches_reference_for_normal_captures(self):
        records = []
        for mode in ('AP', 'monitor'):
            for combination in (
                    '#{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1',
                    '#{ managed, AP } <= 1, total <= 2, #channels <= 1',
                    '#{ managed, AP } <= 2, total <= 2, #channels <= 1',
                    '#{ managed } <= 0, #{ AP } <= 1, total <= 2, #channels <= 1'):
                for frequency in (0, 2412, 5180, 5745):
                    record = capture()
                    record['phy'] = record['phy'].replace('* AP\n', f'* {mode}\n')
                    start = record['phy'].index('\tvalid interface combinations:')
                    record['phy'] = record['phy'][:start] + '\tvalid interface combinations:\n\t\t * ' + combination + '\n'
                    if frequency:
                        record['link'] = f'Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Name:with:colons\n\tfreq: {frequency}.0\n'
                    records.append(record)
        for record, actual in zip(records, native(records), strict=True):
            self.assertTrue(actual['valid'])
            self.assertEqual(actual['capabilities'], legacy(record))

    def test_unknown_restrictions_and_contradictory_duplicates_fail_closed(self):
        records = []
        for flag in ('indoor only', 'unknown restriction', 'no initiate radiation'):
            record = capture()
            record['phy'] = record['phy'].replace('(20.0 dBm)', f'(20.0 dBm) ({flag})')
            records.append(record)
        record = capture()
        record['phy'] = record['phy'].replace('* 2437 MHz [6] (20.0 dBm)',
            '* 2437 MHz [6] (disabled)').replace('* 2412.0 MHz [1] (20.0 dBm)',
            '* 2412.0 MHz [1] (20.0 dBm)\n\t\t\t* 2412 MHz [1] (disabled)')
        records.append(record)
        for record, actual in zip(records, native(records), strict=True):
            self.assertTrue(actual['valid'])
            self.assertFalse(actual['capabilities']['ap_2ghz'])
            self.assertTrue(legacy(record)['ap_2ghz'])

    def test_partial_identity_link_and_nonfinite_frequency_are_rejected(self):
        records = []
        for key, text in (('info', 'Interface other0\n\ttype managed\n\twiphy 0\n'),
                          ('phy', capture()['phy'].replace('Wiphy phy0', 'Wiphy phy2')),
                          ('link', 'Connected to aa:bb:cc:dd:ee:ff\n'),
                          ('link', 'unknown state\n'),
                          ('link', 'Connected to aa:bb:cc:dd:ee:ff\n\tSSID: x\n\tfreq: NaN\n')):
            record = copy.deepcopy(capture())
            record[key] = text
            records.append(record)
        self.assertTrue(all(not r['valid'] for r in native(records)))
