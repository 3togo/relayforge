# Desktop integrations

These optional downstream packages add Relay controls to the existing desktop network UI. They were imported from Wi-Fi Relay revision `12f9f78e2cf4e9f41abacf94fbf5f5524bf47814` and adapted to RelayForge's installed tray launcher. Copyright 2026 Wi-Fi Relay contributors; GPL-2.0-or-later (see COPYING). The pinned vendor snapshot remains unchanged.

| Integration | Supported Ubuntu source | Output version |
| --- | --- | --- |
| NetworkManager applet | 1.36.0-4ubuntu1 | 1.36.0-4ubuntu1+relayforge1 |
| GNOME Control Center | 1:51.0-1ubuntu1 | 1:51.0-1ubuntu1+relayforge1 |

Run `tools/build-deb.sh --backend create_ap --desktop-integrations both --output-dir artifacts/desktop-debs`, then install all top-level `.deb` files in that directory together with APT. Enable source repositories and install the two source packages' build dependencies first. Builds run as an ordinary user, preserve the distribution packaging and tests, and do not install anything. Source trees and build logs are retained below the output directory. This is a version-specific Ubuntu 26.10 build, not a portable binary patch for arbitrary distributions.

The resulting RelayForge package depends on the exact patched applet and Settings versions. The applet's matching transition package is exported too; the unchanged connection editor and GNOME data package remain distribution packages. NetworkManager itself is unchanged. Default RelayForge builds omit these optional dependencies and retain standalone settings and the Shell extension.

The applet adds a Wi-Fi Relay submenu with the hotspot toggle, status/client count, settings action and tray visibility preference. It uses the service's existing D-Bus API. The package hook reloads an old running applet when its user session can be identified; otherwise log in again.

GNOME Settings adds a Wi-Fi Relay button in the Wi-Fi panel that opens the shared editor. It also uses stable device paths to remove virtual AP tabs correctly after shutdown. Restart an already-open Settings window after installation. The integration marker is owned by the patched GNOME package; it must never be installed independently.

To return to the stock UI on this baseline, first install a RelayForge package built without `--desktop-integrations`, then install the stock applet, transition package and GNOME Control Center versions with APT's `--allow-downgrades`. Restart their UI processes. This keeps the RelayForge service and standalone editor available.

The subdirectory README files retain upstream design and test details; their old build commands and `+relay` versions are historical. Use the commands and versions above for RelayForge.
