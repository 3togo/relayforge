import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('device_info', ROOT / 'desktop/device_info.py')
info = importlib.util.module_from_spec(spec)
spec.loader.exec_module(info)

class DeviceInfoTests(unittest.TestCase):
    def test_generated_ap_is_hidden_but_physical_and_upstream_interfaces_remain(self):
        names = ['wlo1', 'rfap8fb0ad97', 'eno2', 'tailscale0']
        self.assertEqual(info.selectable_interfaces(names), ['wlo1', 'eno2', 'tailscale0'])
        self.assertEqual(info.selectable_interfaces(['rfap8fb0ad97']), [])
        self.assertEqual(info.selectable_interfaces([]), [])

    def test_hardware_type_and_details_without_changing_interface_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            link = root / 'net/eno2'
            (link / 'device').mkdir(parents=True)
            (link / 'ifindex').write_text('2\n')
            (link / 'type').write_text('1\n')
            udev = root / 'udev'
            udev.mkdir()
            (udev / 'n2').write_text('E:ID_VENDOR_FROM_DATABASE=Intel\nE:ID_MODEL_FROM_DATABASE=I226-V\nE:ID_NET_DRIVER=igc\n')
            title, hardware, tooltip = info.describe_device('eno2', sys_net=root/'net', udev=udev)
            self.assertEqual(title, 'Ethernet — eno2')
            self.assertEqual(hardware, 'Intel I226-V')
            self.assertIn('Driver: igc', tooltip)
            self.assertEqual(info.describe_device('eno2', wifi=True, sys_net=root/'net', udev=udev)[0], 'Wi-Fi — eno2')

    def test_virtual_and_missing_devices_are_not_called_ethernet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, marker, expected in [('br0', 'bridge', 'Network bridge'), ('tailscale0', 'tun_flags', 'VPN / tunnel')]:
                (root/name/marker).mkdir(parents=True)
                self.assertEqual(info.describe_device(name, sys_net=root)[0], expected + ' — ' + name)
            self.assertEqual(info.describe_device('gone0', sys_net=root)[0], 'Network interface — gone0')
            self.assertEqual(info.describe_device('../etc', sys_net=root)[0], 'Unknown interface')

if __name__ == '__main__':
    unittest.main()
