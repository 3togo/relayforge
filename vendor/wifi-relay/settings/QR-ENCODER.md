# Bundled QR encoder

qrcodegen.py is Project Nayuki's pure Python QR Code generator, version 1.8.0.
Its full MIT notice is retained in the file. Only trailing whitespace is normalized.

Upstream: https://github.com/nayuki/QR-Code-generator
Source: https://raw.githubusercontent.com/nayuki/QR-Code-generator/v1.8.0/python/qrcodegen.py
Original SHA256: b089855caf16185c61421ea4927c1b213cf9468940d71fa8ab11ef83662dcc84

wifi_qr.py builds the Wi-Fi payload and renders RGB bytes. GTK4 displays those
bytes directly; Pillow, OpenCV, qrencode, and python3-qrcode are not runtime
requirements. OpenCV may be used separately to verify decoding during development.
Wi-Fi payload escaping follows https://github.com/zxing/zxing/wiki/Barcode-Contents .
