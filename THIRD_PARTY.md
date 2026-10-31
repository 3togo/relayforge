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
patches are included in the vendor snapshot. Separate reviewed integration imports
are described below. Cargo.lock identifies Rust dependencies;
their individual licenses remain applicable.

Native configuration validation uses unicode-general-category 1.1.0
(Apache-2.0), with Unicode 16.0 data, pinned in Cargo.lock. This preserves the
reference validator's printable-character semantics without a Python runtime
dependency in the Rust schema.

The historical native Bash adapter generator remains in tools/stage-native.py
for protocol/parity tests. It derives an adapter from the pinned BSD-2-Clause
script, checking the source hash and transformation boundaries. The original
source and notices remain in the vendor tree. New native staging copies only
the Rust ELF service/helper and policy files; it no longer ships a derived Bash
script. Compatibility mode retains the original vendor backend and its notices.

`desktop/tray.py`, `desktop/main.py` and `desktop/extension.js` adapt the
MIT-licensed pinned Wi-Fi Relay desktop controls for RelayForge.
The original notice remains in LICENSE and the unmodified source remains under
`vendor/wifi-relay/settings/tray.py`.

`desktop/main.py` similarly adapts the pinned MIT-licensed settings window,
adding descriptive device selectors. Its original remains under
`vendor/wifi-relay/settings/main.py`.

Optional downstream applet and GNOME Settings integrations under `integration/`
are GPL-2.0-or-later; the NetworkManager patch also includes LGPL-2.1-or-later
code. Their source revisions, hashes, adaptations and licenses are recorded in
`integration/manifest.json`, `integration/README.md` and `integration/licenses/`.
They are separate from the MIT Rust service and are not installed by its package.

The retained desktop regression sources under `tests/desktop/` have import
provenance in their manifest. `compat/factory.conf` is the exact MIT-licensed
factory template from the same pinned Wi-Fi Relay revision; it is migration
comparison data, never a credential fallback for a running service.
