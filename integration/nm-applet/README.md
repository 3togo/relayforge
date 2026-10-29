# Wi-Fi Relay inside the XFCE network menu

This is a downstream patch for Ubuntu's NetworkManager Applet 1.36.0-4ubuntu1.
It adds a **Wi-Fi Relay** submenu beside VPN Connections in the existing network
indicator. The submenu has a Hotspot checkbox, service status and client count,
and a settings launcher. The **Show Wi-Fi Relay icon** checkbox controls the
separate desktop tray icon without stopping sharing. Its per-user choice survives
login and restarting the applet; showing the icon also launches the tray if needed.
This requires the updated Relay package containing visibility preference support.
The Hotspot checkbox controls the backend already selected in Relay Settings.
The hotspot remains a Wi-Fi AP; no VPN profile or plugin is registered.

The applet calls Relay's existing Polkit-protected Start/Stop methods asynchronously
and reads GetStatus without accessing credentials. It disables the checkbox during
Start/Stop and when the service is unavailable, shows failed operations in a dialog,
and refreshes from status signals plus a five-second poll for client counts. Exiting
or restarting the applet does not stop the service-owned hotspot. Adapter selection
and regulatory restrictions remain the responsibility of Relay and NetworkManager.

## Build and install

Ubuntu 26.10 CI now includes the patched `network-manager-applet` and
`network-manager-gnome` debs in its build artifact, alongside the Relay debs.
Install the matching applet debs with APT; no source patching on the target
machine is needed. To build the same debs locally, enable Ubuntu source
repositories and install the build dependencies:

```sh
sudo apt-get build-dep network-manager-applet
bash integration/nm-applet/build-deb.sh
sudo apt install ./dist/network-manager-applet_1.36.0-4ubuntu1+relay8_amd64.deb \
  ./dist/network-manager-gnome_1.36.0-4ubuntu1+relay8_amd64.deb
```

The transition package requires its matching applet. The compatible stock editor
can remain installed, or APT can fetch it from Ubuntu.
The patched applet deb reloads an already-running old `nm-applet` in an active
graphical session when it can identify that user's display and session bus. If
no such session is active, the new binary loads at the next login. Click the usual network
icon and open **Wi-Fi Relay**. Relay itself must be installed and configured first.
The Relay tray package restarts its user service on upgrade to load updated code.
Installed-menu Show/Hide was verified to change
the tray indicator between Active and Passive while sharing stayed active on the
same interface.
This integration targets XFCE's nm-applet; GNOME Shell uses the repository's separate
Quick Settings extension.

The active hotspot row uses a Wi-Fi device icon and announces “hotspot active”.
A hosted AP has no received-signal measurement; displaying its reported 0%
strength previously made a connected hotspot look inactive. Ordinary station
connections and scanned networks retain their signal-strength icons.

## Verification

With a display, run the isolated GTK/D-Bus test suite:

```sh
make test-nm-menu
SANITIZE=1 make test-nm-menu
```

The test uses a private bus and fake Relay service, and changes no host connections.
Its 17 cases check tray preference defaults, persistence, private file permissions,
menu activation, duplicate requests, Start/Stop reply types,
busy state, denied authorization, client counts, malformed status, service
replacement with pending replies, and cancellation during outstanding requests. A separate installed-app live test should exercise
the exported DBusMenu events against the real Relay service.

```sh
python3 integration/nm-applet/check-live-menu.py --open-settings
# Optional: cycles an active hotspot only if no clients are connected.
python3 integration/nm-applet/check-live-menu.py --cycle
```

The installed patch was tested in the XFCE session: its exported menu matched
the real active hotspot and one connected client, and the Settings action opened
the settings application. A subsequent client-free installed-menu Stop/Start cycle passed on relay3.
The additional cases cover waiting-state parsing and cancelling recovery from the menu.

## Maintenance and rollback

The patch is kept here rather than copying the full upstream source into this repo.
The builder first runs Python/tray and menu tests, then downloads the matching Ubuntu source and preserves its existing patches,
build flags, and packaging. This is a local downstream package, not an upstream merge.
A future distribution update may replace it; rebase and retest the patch before
rebuilding against a new source version.

To restore the distribution applet while this base version remains available:

```sh
sudo apt install --allow-downgrades network-manager-applet=1.36.0-4ubuntu1 \
  nm-connection-editor=1.36.0-4ubuntu1 network-manager-gnome=1.36.0-4ubuntu1
```

Restart the applet afterward. This leaves the Relay service and Settings installed.

The default bundle now exports only the patched applet and its compatible
transition package. The unchanged editor can remain at the supported Ubuntu
1.36 baseline; it no longer needs a custom matching .deb. Use `[version] --all`
to export it too. See [the dependency review](../../docs/dependency-review.md).
