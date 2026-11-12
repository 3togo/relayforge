# Suspend, recovery and two-client qualification — 2026-11-12

The NetworkManager candidate passed corrected physical suspend, lifecycle recovery
and a controlled two-client LAN traffic soak. The public Internet soak had two
connection timeouts and is not a clean pass. Stable release qualification remains
incomplete; see the [release ledger](release-readiness.md).

## Artifacts and bench

This follow-up used the same NetworkManager `0.1.3~rc2` base and tray package
hashes in the [installed-system receipt](qualification-8d0a5bb-20261112.json).
Package source was `8d0a5bb`, with the unchanged Rust executable built from
`8f39165`, as disclosed there. Later report and preparation-tool commits do not
change that executable's provenance. Staged package hashes were checked again.

The bench ran Ubuntu 26.10 with kernel `7.2.6-070206-generic`, an Intel
`8086:a370` radio using iwlwifi, and AP+STA on 2.4 GHz channel 12. This kernel
and radio combination does not establish coverage of Ubuntu's stock kernel or
other adapters. Clients were the Android 14/API 34 tablet and Android 9/API 29
TV device used in the [initial attempt](qualification-physical-20261112.md).

The [sanitized results](qualification-recovery-20261112.json) include fixture
hashes and measured counts. Raw operational evidence and fixture credentials
remain private.

## Suspend and lifecycle recovery

The corrected observer ran as a separate system service. It subscribed to the
current logind owner's sleep signals before requesting suspend, retained the RTC
alarm until confirmed resume, and independently measured sleep with the
BOOTTIME/MONOTONIC difference. Earlier failed harness runs were retained.

| Check | Observed result |
| --- | --- |
| Authorized automatic resume | 57.345 seconds asleep; same upstream profile; sharing returned through waiting/connecting to active in 26.034 seconds after resume, without an explicit Start. The tablet was reselected onto the AP and passed fresh Wi-Fi-bound public HTTPS and invalid-certificate rejection. Automatic Android network reselection is not claimed. |
| Stop prevents resume | After Stop, a second 57.414-second sleep left sharing idle with no desired request throughout 38.047 seconds of post-resume observations. |
| Startup cancellation | Stop interrupted a real connecting state. Start returned `Startup cancelled`; the final state was idle and the saved profile set was preserved. |
| Service crash | SIGKILL caused systemd recovery and a new idle process. The old AP interface, ownership journal and AP profile were removed; no active request survived the process restart. |
| Upstream loss | Disconnecting the station produced a failed/disconnected-session status and cleared active intent. Restoring the same upstream, cleaning up and explicitly starting sharing succeeded. No automatic reconnect-sharing claim is made. |
| Radio off/on | Off cleared sharing. Radio-on required a bounded device-settle retry before station activation; cleanup and explicit sharing restart then passed. Saved profiles and the original upstream were preserved. |

These are NetworkManager backend results. Native create_ap still needs its own
candidate physical rerun.

## Two clients, DNS and measured traffic

Both clients manually rejoined the recovered candidate AP with fresh BSSID and
subnet checks. Each passed Wi-Fi-bound, certificate/hostname-verified HTTPS to
two public sites and rejected an invalid certificate.

Fresh DNS probes on both clients returned NOERROR/NODATA. Direct upstream
controls matched their response codes and record counts. Packet evidence showed
client-to-gateway, upstream and return traffic for each unique query. The strict
NXDOMAIN probes remained unsuccessful; their result was not relabeled as success.

| Traffic check | Result |
| --- | --- |
| Public Internet soak | 610.544 seconds; tablet 56/57 and TV 56/57 successful HTTPS requests. Each had one connect timeout. Repeated checks retained the expected AP identity. |
| Direct external controls | Bench upstream 112/116 successful requests; primary machine 61/62. Controls also timed out without using the AP forwarding path. They establish external-path unreliability, but do not individually attribute the two client failures. |
| Controlled upstream-LAN HTTPS soak | 604.397 seconds; 82/82 complete responses per client, each exactly 32,768 bytes: 2,686,976 bytes per client. No transfer or AP identity failures occurred. Server-side source addresses confirmed the bench's NAT path to a separate machine on the upstream LAN. |
| Controlled TLS identity | The LAN fixture used a per-test certificate and HTTPS hostname verification; both wrong-hostname controls failed at TLS. Device trust stores were unchanged. |
| Service continuity | The service PID remained unchanged across the observed soak telemetry. This is an availability check, not a throughput or memory-performance benchmark. |

The controlled LAN soak isolates the local forwarding path; it does not replace
the public Internet result. The two Internet client timeouts remain unattributed,
and a clean WAN reliability result is still open.

## Restoration

The original hotspot package versions, file bytes/modes, configuration, enabled
and running service, station profile and forwarding baseline were independently
verified after restoration. A concurrent package autoremove had removed hostapd
while its original dependent was absent; its original version and automatic mark
were restored, and configured package state plus dependency consistency were
checked. Unrelated concurrent system updates were retained.

Both clients returned to their original networks and power settings. Both test
network entries were removed; all 61 original TV network identities, including
disabled entries, were preserved. Temporary JARs, certificate and UI files were
removed from the devices. The test server, SSH tunnel and temporary root screen
were closed without touching unrelated sessions.

Remaining release work includes active downgrade, final native create_ap traffic
and recovery, additional adapters/bands, and clean external-path reliability
coverage. No release asset was published by this qualification run.
