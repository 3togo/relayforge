# Optional downstream desktop integrations

These sources restore optional Wi-Fi Relay integration work for RelayForge.
They are development components, not dependencies of the normal RelayForge
package. Import provenance, original hashes and adaptations are recorded in
`manifest.json`. The only initial code adaptation changes the applet's tray path
to `/usr/lib/relayforge/settings/tray.py`.

| Component | Supported Ubuntu source | Behavior | License |
| --- | --- | --- | --- |
| nm-applet | network-manager-applet 1.36.0-4ubuntu1 | Shared-service hotspot menu, client count, error display, tray visibility, settings launcher, hosted-AP icon | GPL-2.0-or-later |
| gnome-settings | gnome-control-center 1:51.0-1ubuntu1 | Stable Wi-Fi device tabs and a Relay settings entry point | GPL-2.0-or-later |
| nm-core | network-manager 1.58.1-1ubuntu3 | Experimental downstream capability 0x7001 and NM-owned child AP interfaces | GPL-2.0-or-later and LGPL-2.1-or-later |

The upstream notices remain in the sources and patch contexts. Wi-Fi Relay
integration copyright belongs to its contributors; NetworkManager patches also
derive from NetworkManager contributors. Full GPL-2 and LGPL-2.1 texts are in
`licenses/`. These components do not change RelayForge's MIT licensing.

The source-version boundaries are intentional. Do not apply these patches to a
different distribution source without reviewing, rebasing and testing them.
No original hardware report or release certification is transferred by importing
the patches.

Run `python3 tools/check-integrations.py` to check source provenance. The applet's
`test.sh` compiles a GTK3/Gio/Jansson harness and uses a private bus and fake
service. It does not change host connections. A display and development headers
are required. Use `SANITIZE=1` for address/undefined-behavior checks and
`OPT_LEVEL=2` for an optimized run. Run these checks in an isolated build
environment rather than installing development packages on a live networking
host just to satisfy them.

The applet retains the existing public D-Bus name and uses the same desktop
preferences as RelayForge. GNOME Settings invokes `wifi-hotspot-settings
--standalone`; the entry-point marker is installed only with the patched panel.
The NM capability remains a downstream development contract, not a public
NetworkManager API. Stock NetworkManager continues to use RelayForge's existing
owned-child helper path.

## Build-only workflow

Obtain and unpack the exact Ubuntu source from the table in a disposable build
environment, with its build dependencies already available. Review a build plan:

```sh
python3 tools/build-integration.py nm-applet --source-dir /path/to/unpatched-source --plan
python3 tools/build-integration.py nm-applet --source-dir /path/to/unpatched-source
```

Use `gnome-settings` or `nm-core` for the other components. The builder rejects
other source versions and copies the supplied tree before applying patches with
zero fuzz. It retains the build directory and log on failure, bounds the build
time, and records exported package hashes and the integration manifest identity.
`--all-packages` retains all companion packages; the NM build never relaxes
libnm exact-version dependencies. `--jobs` controls build parallelism.
Applet and Settings dependency minimization applies only to their reviewed
source versions. The applet builder retains the distribution's lifecycle scripts
instead of installing a custom automatic applet-restart hook.

Building packages must never install them, enable extensions, restart the
applet or networking services, or activate a hotspot. Installing these downstream
packages and testing them against real hardware requires a separate controlled
environment. Keep the stock distribution packages available for rollback.

## Incoming desktop package workflow

The existing `tools/build-deb.sh --desktop-integrations both` workflow remains
available for the applet and GNOME Settings packages. It uses the component
`build-deb.sh` scripts and requires their exact patched versions in the service
package. The normal package build omits these dependencies. The build-only
workflow above also supports the experimental NetworkManager core patch.
