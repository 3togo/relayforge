# Boot-policy rollback and upstream-radio follow-up — 2026-11-12

This follows the [create_ap and active-downgrade report](qualification-create-ap-downgrade-20261112.md).
The old archive remains unchanged. The new [rollback command](package-rollback.md)
restores and verifies boot policy around its installation, including when APT
fails. The current candidate's maintainer hook already avoids enabling services.

## Disconnect investigation

The original incident's supplicant journal recorded beacon loss on both the
Intel AP+STA station and an independent USB station on the same upstream BSSID.
Between 08:01:30 and 08:05:00 UTC, Intel reported 10 beacon-loss notifications and USB
reported 45. At 08:04:35 UTC, Intel disconnected with reason 4 and
`locally_generated=1`; the supplicant ignored the BSSID for 10 seconds before
reconnecting at approximately 08:04:46. During this interruption create_ap
reported the changed upstream and stopped its AP as designed. No firmware crash was recorded
in that interval.

The Linux [mac80211 connection-loss path](https://linux.googlesource.com/linux/kernel/git/torvalds/linux/+/e3b76494d675e49fa032ec47f58487875a1de70a/net/mac80211/mlme.c)
contains the observed connection-loss diagnostic and locally reports a
connection-loss disconnect. This supports interpreting the log as a local
station-link failure; it does not identify why beacons were lost. Simultaneous
loss on the separate USB PHY makes shared RF/upstream conditions or coexistence
worth investigating, but does not prove router failure or exclude AP-related
interference. No speculative driver, firmware or power-management change was
made.

## Egress correction

The earlier LAN fixture logs identify the NAT source as the independent USB
station. Those counts establish forwarding through the bench, rather than
loaded egress through the Intel station. The earlier reports now disclose this
limit. Native create_ap deliberately follows host route preference; see
[firewall semantics](native-firewall.md).

For this follow-up, a temporary fixture-only host route selected the Intel
station. The fixture observed that station's source address. Both station
profiles and their settings were retained, and the USB station was left active.
The route is included in the restoration guard. Intel station power saving
remained on; the USB station's existing power saving remained off.

## Rerun results

The [sanitized results](qualification-boot-radio-20261112.json) record:

| Check | Result |
| --- | --- |
| Idle baseline | 301.280 seconds; 100 station samples; no sampled disconnect; Intel beacon-loss counter remained 15. |
| Loaded create_ap, tablet only | 602.725 seconds; 91/91 complete, verified 32 KiB HTTPS responses, totaling 2,981,888 bytes. Fresh AP identity checks passed. All 199 station samples remained connected with power saving on; beacon-loss counter stayed 15 and failed-transmission counter stayed zero. Retries increased from 1 to 91. |
| Active rollback to published 0.1.2 | The new command installed the actual archive during verified tablet traffic. APT returned zero, the daemon restarted idle without desired sharing, and original disabled boot policy was restored and verified automatically. Old AP interface/profile/journal cleanup, configuration, upstream, process replacement and published executable checks passed. No manual Stop preceded installation. |
| Traffic after rollback | Explicit Start and manual tablet network reselection; 5/5 complete verified 32 KiB HTTPS responses over 33.466 seconds. Wrong-hostname TLS control rejected. |
| Two-client rerun | Blocked: the previously selected API 29 TV/projector was unreachable by ADB and ping. The API 34 tablet was available. No other client was substituted, and the one-client result is not a two-client pass. |
| Portable regressions | Seven new rollback-command tests and four existing package-hook tests passed. They cover disabled/enabled policy, APT failure, restoration failure and preflight refusal of masks/backend changes. |

The failed plain-APT historical result remains intact. This is a verified fix
for the documented rollback command, not a retroactive change to 0.1.2 or a claim
that plain APT now preserves its boot policy. Sharing interruption during package
replacement is expected; uninterrupted traffic is not claimed.

The original two-client disconnect was not reproduced by this single-client
rerun. Its exact cause remains unresolved, and the unavailable second client
prevents closing the original reliability check.

The packages remain the exact rc2 candidates in the
[installed-system receipt](qualification-8d0a5bb-20261112.json). No daemon
executable or historical release asset was rebuilt for this follow-up.

## Restoration

Independent verification confirmed the original package files/modes,
configuration, service state, station connection, forwarding and dependency
marks, with no owned firewall or AppArmor resources remaining. The fixture route
was removed, the independent USB station preserved, and Intel power saving
remained at its original setting. The tablet returned to its original home
network and launcher; original saved-network identities and power settings were
verified, and test network/probe/certificate/UI files removed. The unavailable TV
was not configured or joined during this follow-up. The fixture server, radio
observer, restoration guard and temporary root screen were closed. Unrelated
sessions and the unselected Android device were left alone.

No release asset was published. Stable sign-off still requires the two-client
rerun and the broader limits in the [release ledger](release-readiness.md).
