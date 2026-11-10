# Installed-system and physical Wi-Fi qualification of 9526f6d

On 2026-11-07 (UTC), source `9526f6d662073f5d649f4a14e6dbbc9eb43473db`
passed all **39 installed-system checks** and physical Wi-Fi traffic checks
before and after **42.958 seconds of real suspend**. Manual AP restart after
resume succeeded, and the original host/client state was restored. The
[machine-readable receipt](qualification-9526f6d-20261107.json) records source,
executable, package and harness identities, results and limits.

## Source and packages

All 285 committed source files and their modes matched tree
`24b8088bc77426428b7ccab9c368978b8990d56a` before and after qualification.
The release executable was reused from a prior build whose source contents and
modes exactly match this commit; it was not rebuilt for these reruns. Nine
packages were freshly assembled with fixture versions `0.1.2~r9526f6d1` and
`0.1.2~r9526f6d2`. Extracted hooks matched committed files, and all guest package
hashes matched the input manifest. Three package-hook regression tests and
shell syntax checks also passed. These fixture packages are not releases.

These are new runs for the worker lifecycle refactor in `9526f6d`, separate
from the earlier [669175f qualification](qualification-669175f-20261106.md).
Publishing this documentation does not imply another qualification run against
its later documentation-only commit.

## Installed-system checks

A fresh disposable Ubuntu 26.10 (Stonking Stingray), x86-64 KVM overlay passed
installation, upgrade, downgrade, backend replacement, enablement preservation,
D-Bus/systemd activation, authorization and unprivileged credential denial,
GNOME controls, companion removal/reinstallation, removal of either running idle
backend, purge and explicit configuration preservation: **39 checks passed**.

The guest used systemd 261.3, D-Bus 1.16.2, Polkit 127, GNOME Shell 51.0,
GTK 4.24.1 and 3.24.52, and Python 3.14.7. Signed cloud-image checksums and all six
retained backing-image hashes were verified. The owned VM and container stopped
before physical testing began. Host networking and source were unchanged.
Candidate packages were installed only in the guest; no physical radio or host
system bus was exposed to it.

## Physical Wi-Fi and suspend

The physical Ubuntu 26.10 x86-64 run exercised native `create_ap` with one
dedicated 2.4 GHz AP, Ethernet upstream and an Android client. The verified
candidate package ran in an isolated container using the real radio, system bus
and RTC. No candidate package was installed on the physical host.

Before suspend, the client associated, obtained DHCP, pinged the gateway,
resolved DNS explicitly through the gateway and received HTTP 200 over TLS 1.3
with certificate and hostname verification. The service client list confirmed
the DHCP lease. RTC wake resumed the host after **42.958 seconds** of measured
suspend. The service returned to idle, and manual AP restart succeeded.
Ethernet address/default-route recovery took **42.105 seconds** before the
second probe. All client traffic and service lease checks then passed again.
AP stop and cleanup completed without errors.

Routes, active connections, radio/rfkill state, forwarding and original service
activation/enablement were restored. The client's original network and saved
profiles were preserved. Both original ADB devices were available again; test
device files and temporary policy were removed. No test AP interface or
container remained, and the root postcheck confirmed the RTC alarm was off.

## Evidence and limits

The retrieved private archive contains **75 files**, all verified against its
manifest. Only its SHA-256 and selected result fields are published in the
receipt. Raw logs, machine/account identifiers, connection details, network
inventories, addresses, device identifiers, credentials, UI dumps, guest disks
and keys remain outside Git.

Installed-system coverage is limited to idle-service package lifecycle.
Active-sharing upgrades remain unqualified. Physical coverage is limited to
this adapter/client, native `create_ap`, dedicated 2.4 GHz AP mode, Ethernet
upstream and manual restart after resume. Physical operation through the
compatibility worker, other bands, AP+STA, automatic restart, other
hardware/distributions and interactive Polkit-agent prompts remain unqualified.
