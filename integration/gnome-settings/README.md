# Optional GNOME Settings Wi-Fi integration

GNOME Control Center 51.0 uses `NMDevice.Udi` as the Wi-Fi stack page ID.
NetworkManager clears Udi when a virtual Relay interface unrealizes. The
device-removed callback consequently cannot find its old page, leaving another
Intel AX200 tab after every start/stop cycle despite the interface being removed.

The downstream patch uses each NMObject's stable D-Bus path for both stacks,
device membership checks, removal, visible-device bindings, and command-line
device selection. Real hardware UDI changes no longer create duplicate pages.
One physical station and one active Relay child can still appear as two devices.

Build directly on Ubuntu Stonking:

```sh
sudo apt-get build-dep gnome-control-center=1:51.0-1ubuntu1
bash integration/gnome-settings/build-deb.sh
```

The builder preserves Ubuntu packaging, runs its normal tests, and writes the
packages to `dist/`. It does not install them. GNOME Settings must be restarted
after installation. To verify, keep Settings open across repeated Relay Start /
Disconnect cycles: only the current physical adapter and current Relay interface
should have tabs, and removing a visible Relay tab should select a remaining
device without leaving an empty page or a GTK assertion.

The source baseline is Ubuntu `1:51.0-1ubuntu1`; the current builder defaults to
`1:51.0-1ubuntu1+relay3`. GNOME's GPL-2.0-or-later license applies to the patch.
The Ubuntu build passed all four upstream test targets. With the built binary
open against the installed native Relay stack, two client-free Stop/Start cycles
removed the old tab and restored exactly the two current tabs, verified through
the accessibility tree. No GTK assertions occurred. The package awaits sudo
installation; the open window currently runs the tested binary directly.

The default export contains only gnome-control-center. The code-only device-tab
patch retains compatibility with the stock 51.0 Ubuntu data package; rebuilding
data, faces, and development packages is unnecessary for this fix. Pass
`[version] --all` to export every built package. Dependency changes are limited
to this reviewed source version.

## Relay entry point

`relay-entrypoint.patch` adds a **Wi-Fi Relay** button to the Wi-Fi panel's header
when `wifi-hotspot-settings` is available. It invokes `wifi-hotspot-settings
--standalone`, opening the shared editor without redirecting back to this panel.
Launch errors appear in a dialog; the panel does not call privileged Relay methods
or parse credentials itself. Closing either window does not stop sharing.

The builder includes `/usr/share/gnome-control-center/wifi-relay-entrypoint` with
contract version `1` in the patched binary package. The Relay launcher redirects
only in a GNOME session, with this exact marker and an available control-center
executable. Older tab-only packages and stock Settings keep direct editor access.
A failed integrated launch returns its failure instead of opening a second editor.
The marker must be removed with the patched package; do not install it separately.

This optional downstream bridge is an entry point, not an embedded preferences
panel or an upstream merge. It adds no dependency on Relay to GNOME Settings.
For cross-desktop deployments, prefer stock packages and the shared Relay editor;
use the bridge only when maintaining a matching control-center build. The earlier
live results above cover the tab fix, not the new button.
