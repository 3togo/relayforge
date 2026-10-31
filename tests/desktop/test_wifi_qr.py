"""Wi-Fi payload correctness and optional independent image decoding."""
import unittest
from settings.wifi_qr import wifi_payload, qr_rgb


class WifiQrTests(unittest.TestCase):
    def test_standard_credentials_and_hidden_flag(self):
        self.assertEqual(wifi_payload('Hotspot', '12345678'), 'WIFI:T:WPA;S:Hotspot;P:12345678;;')
        self.assertEqual(wifi_payload('Hotspot', '12345678', True),
                         'WIFI:T:WPA;S:Hotspot;P:12345678;H:true;;')

    def test_special_characters_are_escaped(self):
        self.assertEqual(wifi_payload('A;B:C', 'pass\\word,";:'),
                         'WIFI:T:WPA;S:A\\;B\\:C;P:pass\\\\word\\,\\"\\;\\:;;')

    def test_incomplete_or_invalid_credentials_do_not_use_factory_fallback(self):
        for ssid, password in [('', '12345678'), ('Valid', ''), ('Valid', 'short'),
                               ('x' * 33, '12345678'), ('Valid', 'nonasciié'), ('bad\nname', '12345678')]:
            with self.subTest(ssid=ssid), self.assertRaises(ValueError):
                wifi_payload(ssid, password)

    def test_image_has_white_quiet_border_and_black_modules(self):
        width, pixels = qr_rgb(wifi_payload('Hotspot', '12345678'))
        self.assertEqual(len(pixels), width * width * 3)
        self.assertEqual(pixels[:width * 3], b'\xff' * (width * 3))
        self.assertEqual(pixels[-width * 3:], b'\xff' * (width * 3))
        self.assertIn(b'\x00\x00\x00', pixels)

    def test_independent_decoder_recovers_ascii_unicode_and_escaped_payloads(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('Optional development decoder OpenCV is not installed')
        for ssid, password, hidden in [('Hotspot', '12345678', False),
                                       ('网络 café', 'pass;word:"', True),
                                       ('x' * 32, 'a' * 63, False)]:
            with self.subTest(ssid=ssid):
                expected = wifi_payload(ssid, password, hidden)
                width, pixels = qr_rgb(expected)
                image = np.frombuffer(pixels, dtype=np.uint8).reshape(width, width, 3)
                decoded, points, _ = cv2.QRCodeDetector().detectAndDecode(image)
                self.assertIsNotNone(points)
                self.assertEqual(decoded, expected)
