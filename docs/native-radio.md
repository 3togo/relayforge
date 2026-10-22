# Native radio inspection and startup preflight

Rust now implements public `GetCapabilities` and checks radio permissions before
sending a fresh Start request to the retained networking backend. The existing
D-Bus signature and all ten capability fields are preserved. Capability
requests are forbidden at both worker protocol boundaries.

The pure core decodes the configured interface identity, PHY, supported AP mode,
permitted 2.4/5 GHz frequencies, interface combinations and station link state.
The service executes only `iw dev IFACE info`, `iw phy phyN info` and
`iw dev IFACE link`, with a fixed utility path and C locale. Each command has a
two-second deadline and a one-MiB limit on each output pipe; the entire inspection
has a five-second deadline. Invalid UTF-8, command failure, timeout and malformed
identity/link state return sanitized errors. Raw stderr is never returned.
Cancellation drops the inspection and kills its owned process group.

Channel parsing accepts known frequency/channel pairs and unrestricted transmit
power records. Disabled, passive, no-IR, DFS and unknown channel annotations are
excluded. Duplicate identical channel entries intersect their permissions.
Concurrency requires capacity for both managed and AP interfaces, a total of at
least two interfaces and at least one channel. Unknown extra combination
constraints are rejected. These rules intentionally reject some records the
pinned Python regex parser accepted; future driver formats need explicit support.
Six-GHz operation is outside the existing desktop contract.

Start validates the native configuration first. Dedicated-adapter sharing needs
a permitted channel and a distinct upstream interface. Connected sharing needs
AP/STA capacity and virtual-interface support. NetworkManager additionally needs
a managed, connected adapter on an allowed current channel, a matching requested
band/channel and supported configuration options. A rejection returns the existing
`success: false` JSON shape and a status error without requesting activation.
An already-observed active hotspot retains its idempotent Start path.

A Stop revision cancels a pending radio inspection. Lifecycle epochs prevent
stale preflight completion from overwriting Stop. Status and clients remain
available through Rust snapshots while inspections run. Configuration commits
remain serialized with startup.

The resulting Start plan is a validation result; Rust does not yet execute it.
Python still rechecks live radio state before activation and owns upstream band
switching, NetworkManager profile/sibling-interface checks, actual AP creation,
firewall changes, journals, recovery and cleanup. An inspection is not an atomic
kernel snapshot and cannot authorize later activation by itself. Inspection
failure can require Stop/cleanup before another startup attempt. `iw` remains an
external nl80211 utility; this step does not introduce native netlink transport.

`--demo-radio CAPTURE.json` replays an explicit capture on the private session bus
with in-memory configuration and simulated activation. The capture has exactly
four string fields: `wifi_iface`, `info`, `phy` and `link`; its adapter must match
the configured adapter (`demo0` initially). It cannot start a real networking
worker or run `iw`. Normal demo modes use a synthetic one-channel capture through
the same parser.

Tests compare the complete capability JSON with the immutable Python backend
across 32 ordinary captures, exercise deliberate stricter rejection cases,
malformed identities and link data, timeout/output/encoding bounds, cancellation
of a process and its descendant, and the unchanged desktop client over a private
bus. No physical Wi-Fi or firewall changes are part of these tests.

The command interface is documented by the
[Linux wireless iw documentation](https://wireless.docs.kernel.org/en/latest/en/users/documentation/iw.html).

For non-disruptive physical diagnostics, `--inspect-radio IFACE` prints live
capabilities using read-only iw commands. `--preflight-radio /absolute/config`
loads an operator-owned private configuration and prints either a Start plan or
`success: false` with its rejection reason. Both modes exit before opening D-Bus
or constructing the networking worker. A successful plan with
`switch_upstream: true` requires changing the upstream connection if executed;
the diagnostic never applies it. These modes do not install configuration or
replace the running service. Treat their raw capability output as private because
it includes the upstream SSID and BSSID.
