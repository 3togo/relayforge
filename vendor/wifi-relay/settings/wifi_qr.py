"""Wi-Fi QR payload and RGB rendering, without external imaging dependencies."""
import re
try:
    from qrcodegen import QrCode, QrSegment
except ModuleNotFoundError:
    from settings.qrcodegen import QrCode, QrSegment


def wifi_payload(ssid, password, hidden=False):
    if (not isinstance(ssid, str) or not ssid or len(ssid.encode('utf-8')) > 32
            or any(ord(char) < 32 or ord(char) == 127 for char in ssid)):
        raise ValueError('Enter a valid network name before sharing its QR code.')
    if (not isinstance(password, str) or not (8 <= len(password) <= 63
            or re.fullmatch(r'[0-9A-Fa-f]{64}', password))
            or any(not 32 <= ord(char) <= 126 for char in password)):
        raise ValueError('Enter a valid Wi-Fi password before sharing its QR code.')
    if type(hidden) is not bool:
        raise ValueError('Invalid hidden network setting.')
    def escape(value):
        return ''.join('\\' + char if char in '\\;,":' else char for char in value)
    return f'WIFI:T:WPA;S:{escape(ssid)};P:{escape(password)};' + ('H:true;' if hidden else '') + ';'


def qr_rgb(payload):
    """Return an opaque RGB square with integer module scaling and quiet zone."""
    segments = [QrSegment.make_eci(26), QrSegment.make_bytes(payload.encode('utf-8'))]
    qr = QrCode.encode_segments(segments, QrCode.Ecc.MEDIUM)
    border = 4
    modules = qr.get_size() + 2 * border
    scale = max(4, 320 // modules)
    width = modules * scale
    pixels = bytearray()
    for y in range(modules):
        row = b''.join((b'\x00' * 3 if qr.get_module(x - border, y - border) else b'\xff' * 3)
                       * scale for x in range(modules))
        pixels.extend(row * scale)
    return width, bytes(pixels)
