# Reused components

The Wi-Fi Relay snapshot is derived from Erhan Zeyrek's GNOME Wi-Fi Hotspot and
3togo's maintained fork. Its revision and exact bytes are in vendor/manifest.json.

| Files | License and attribution |
| --- | --- |
| Python daemon, settings, GNOME extension and other application assets | MIT; original Erhan Zeyrek notice retained in LICENSE |
| vendor/wifi-relay/daemon/create_ap | BSD-2-Clause; oblique and lakinduaksh; embedded notices and both notices under vendor/wifi-relay/packaging/licenses |
| vendor/wifi-relay/settings/qrcodegen.py | MIT; Project Nayuki; full notice in source and QR-ENCODER.md |
| vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.metainfo.xml | CC0-1.0, as recorded by the original Debian copyright file |

The copied Debian copyright inventory is retained as provenance under
vendor/wifi-relay/packaging/licenses/copyright. Its integration entries describe
the upstream project; no NetworkManager, applet, or GNOME Settings integration
patches are included in this snapshot. Cargo.lock identifies Rust dependencies;
their individual licenses remain applicable.

Native configuration validation uses unicode-general-category 1.1.0
(Apache-2.0), with Unicode 16.0 data, pinned in Cargo.lock. This preserves the
reference validator's printable-character semantics without a Python runtime
dependency in the Rust schema.

The native create_ap payload derives its Bash adapter from the pinned BSD-2-Clause
script. Staging changes only the recovery-helper location and six invocations to
use the Rust runtime; the original source and notices remain in the vendor tree.
