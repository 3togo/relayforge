# Two-client qualification through the Intel station — 2026-11-12

This completes the previously blocked two-client rerun in the
[boot-policy and radio follow-up](qualification-boot-radio-20261112.md).
It uses the same selected API 34 tablet, API 29 TV/projector, Intel iwlwifi
AP+STA bench and exact rc2 package hashes from the
[installed-system receipt](qualification-8d0a5bb-20261112.json).
No daemon code or release asset changed for this test.

A temporary route for the LAN fixture selected the Intel station. The HTTPS
fixture observed the Intel station's NAT source; the independent USB station
and its settings were retained. Intel power saving stayed on throughout.
Both clients obtained DHCP leases, matched the fresh AP BSSID/subnet, received
complete certificate/hostname-verified 32 KiB HTTPS responses, and rejected the
wrong-hostname TLS control. Device trust stores were unchanged.

The [sanitized receipt](qualification-two-client-intel-20261112.json) records:

| Check | Result |
| --- | --- |
| Concurrent complete transfers | 606.026 seconds; tablet 81/81 and TV 81/81. Each received exactly 2,654,208 bytes in complete 32 KiB HTTPS responses. All 164 fixture requests, including the two initial positive controls, used the Intel NAT source. |
| Fresh client identities | All 42 periodic client SSID/BSSID checks passed. Both devices were kept awake/unlocked, including the waits. |
| Station continuity | All 200 radio samples remained connected with power saving on. Beacon-loss counter stayed 15; failed-transmission counter stayed zero. Retries increased from 93 to 851. No Intel disconnect/beacon-loss supplicant event or kernel connection-loss event appeared during the measured interval. |
| Service continuity | PID, active-enter timestamp and restart count remained unchanged; restart count was zero. |

The offline workflow passed at the unchanged software head
[`a81cec7`](https://github.com/3togo/relayforge/actions/runs/43192946457), including
the new rollback-command test group. This follow-up adds physical evidence and
documentation only.

This is a bounded functional reliability run, not a throughput benchmark or
proof of the cause of the earlier upstream disconnect. The historical
simultaneous Intel/USB beacon-loss incident remains unexplained; a passing
rerun does not erase it. Broader adapters/bands/kernels and external-path
reliability limits remain in the [release ledger](release-readiness.md).

Independent verification confirmed the original package file bytes/modes,
configuration, service, station connection, forwarding and all protected
dependency marks. Owned firewall/AppArmor resources and the fixture route were
absent; the independent USB station and original Intel power saving were
preserved. Both devices returned to their original home networks and original
power settings. The test network entries and helper/certificate/UI files were
removed; the original saved-network sets, including all 61 TV identities, were
verified. The fixture server, SSH tunnel, radio observer, restoration guard and
temporary root screen were closed. Unrelated sessions and the unselected
Android device were untouched.

Raw network/device identities, packet/event logs, credentials and rollback
archives remain private. No release asset was published.
