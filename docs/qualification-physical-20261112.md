# Physical qualification attempt — 2026-11-12

The [corrected follow-up](qualification-recovery-20261112.md) records subsequent
suspend, recovery, two-client checks and complete client cleanup. This report
describes the earlier attempt. Stable release qualification remains incomplete.
Raw logs, credentials, device
identifiers, network addresses, packet captures and rollback archives are private.

## Tested artifacts and scope

The candidate used the NetworkManager `0.1.3~rc2` base and tray packages identified
in [the installed-system receipt](qualification-8d0a5bb-20261112.json).
Their package source was `8d0a5bb`; the unchanged Rust executable came from
`8f39165`, as disclosed in that receipt. This is not a freshly rebuilt package
from the later preparation-tool or report commits.

The upgrade source was the published `0.1.2` amd64 asset, SHA-256
`cc09dd9c55d3f5fb8cdeb2f6ed212e49f94f0149f9a8af0c2e4ff7d1de73d96d`.
The bench ran Ubuntu 26.10 on x86-64, with one Intel iwlwifi radio supporting
AP+STA on the same channel. The upstream and AP used 2.4 GHz channel 12.
Clients were an Android 14/API 34 tablet and an Android 9/API 29 TV device.
Only the tablet completed the candidate traffic checks.

## Observed results

| Check | Result and limit |
| --- | --- |
| Published-release two-client traffic | Both clients obtained AP leases and passed Wi-Fi-bound, certificate/hostname-verified HTTPS to two external sites. Invalid-certificate HTTPS was rejected. This establishes the old-release baseline, not two-client candidate coverage. |
| DNS forwarding baseline | Fresh unique queries were observed on client, gateway and upstream packet paths. The upstream returned NOERROR with no answers and one authority record. The strict NXDOMAIN probe remained unsuccessful; upstream NODATA was not relabeled as NXDOMAIN success. |
| Active upgrade | With tablet HTTPS traffic running, installation of the candidate restarted the service and removed the old owned AP interface and NetworkManager profile. The configuration and upstream UUID were unchanged; the installed ELF matched the candidate receipt. Sharing was interrupted and the new process remained idle with no retained active request. No seamless-upgrade claim is made. |
| Candidate AP+STA | An explicit Start created a separate AP interface while retaining the upstream connection. The tablet rejoined with a fresh BSSID/subnet match, passed two Wi-Fi-bound HTTPS checks with certificate and hostname verification, and rejected an invalid certificate. The strict DNS probe again received NODATA. |
| TV reconnect | After the published AP was recreated with a new BSSID, the TV reported that it could not find the SSID. Its first baseline connection had worked. The cause was not established, and the candidate two-client and reconnection gates remain open. |
| 5 GHz no-IR refusal | The published release refused AP+STA on the original no-IR channel without changing the observed station, interface or profile sets. This is refusal coverage, not successful 5 GHz sharing. |
| Automatic resume | Inconclusive because the test harness treated queued suspend as a completed sleep cycle and cleared its RTC alarm too early. System logs confirm sleep from 05:35:31 to 05:51:49 UTC. The test assertion ran before actual sleep; neither automatic sharing recovery nor Stop-prevents-resume was established. This is not evidence of a product wake defect. |

## Restoration and remaining work

The inactivity watchdog restored the original host installation after resume.
An independent root verification then checked the original package versions,
file bytes and modes, configuration, original upstream profile and enabled/running
service. Candidate ownership journals, AP interfaces, AppArmor profile and unit
links were absent; forwarding matched the saved baseline and the RTC alarm was
clear. The temporary root screen was closed without touching other sessions.

Both clients returned to their original home networks. Original stay-awake and
screen-timeout settings were verified after restoration. The tablet test network
was forgotten, and the temporary probe JAR and UI XML files were removed from
both devices. The TV's saved test-network entry was not confirmed removed:
its shell denies Wi-Fi management commands and the unavailable SSID was absent
from the inspected settings list. No original saved networks were deleted.

A corrected observer must be active before another suspend request, and the wake
alarm must remain armed until actual resume is confirmed. Remaining release gates
include automatic resume and Stop suppression, active downgrade, candidate
create_ap traffic, radio/upstream/crash/cancellation recovery, a two-client
candidate soak, and the additional adapter/band matrix. See the
[release ledger](release-readiness.md); these missing results must not be inferred
from hosted CI, installed-system checks or the old-release baseline.
