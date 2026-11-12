# create_ap and active downgrade qualification — 2026-11-12

Native create_ap passed functional traffic, suspend and recovery checks on the
selected bench. Its short soak was interrupted by an upstream Wi-Fi disconnect.
The active downgrade to published 0.1.2 passed cleanup and traffic recovery but
**failed service boot-enablement preservation**. These are qualified results,
not a clean stable-release sign-off.

## Scope and artifacts

This used the same Ubuntu 26.10 / kernel `7.2.6-070206-generic` Intel iwlwifi
`8086:a370` bench and Android API 34 tablet / API 29 TV described in the
[NetworkManager follow-up](qualification-recovery-20261112.md). AP+STA used
2.4 GHz channel 12. Additional adapters, bands and stock kernels remain outside
this evidence.

Candidate package hashes match the
[8d0a5bb receipt](qualification-8d0a5bb-20261112.json), including its disclosed
unchanged executable built from `8f39165`. The create_ap rc1/rc2 packages contain
the same executable; their version transition exercises installation, process
ownership and cleanup, not compatibility with different historical create_ap
code. The NetworkManager downgrade used the actual published 0.1.2 artifact,
whose executable differs from the candidate. Digests and counts are in the
[sanitized results](qualification-create-ap-downgrade-20261112.json).

## create_ap observations

| Check | Result |
| --- | --- |
| AP+STA and two clients | A separate AP interface retained the upstream station. Both clients obtained DHCP leases, matched the live AP identity and passed verified public HTTPS to at least one site. The tablet passed both public sites; one TV request to example.com timed out at connect. |
| Controlled HTTPS and TLS | Both clients received complete 32 KiB HTTPS responses through the bench's NAT path to the upstream LAN fixture. Both wrong-hostname controls failed at TLS; trust stores were unchanged. |
| DNS | Unique client queries traversed the gateway, upstream and return paths in packet evidence. Direct upstream controls matched NOERROR/NODATA. Strict NXDOMAIN probes remained unsuccessful. |
| Short concurrent soak | 183.793 seconds: tablet 22/22 transfers, TV 21/22. The last TV probe timed out in ADB while the kernel recorded loss of the upstream station connection at 08:04:35 UTC. Sharing stopped and reported that the upstream changed. The station returned after about 11 seconds; explicit Start restored sharing. The initial link-loss cause remains unassigned; this is not a clean reliability soak. |
| Active rc2 → rc1 fixture downgrade | With USB-tablet traffic running, the daemon restarted idle, the old service-cgroup processes disappeared, and the owned interface, journal and all tagged firewall rules were removed. Configuration, upstream, boot enablement and unrelated firewall rules were preserved. Explicit Start and complete HTTPS passed afterward. |
| Upgrade back to rc2 | Active cleanup, process replacement and preservation checks passed again before further rc2 qualification. |
| Automatic sharing resume | 57.043 seconds physically asleep; the same upstream returned and sharing became active after 10.012 seconds without an explicit Start. After manual tablet network reselection, complete HTTPS passed. Automatic client network reselection is not claimed. |
| Stop suppression | A second 57.270-second sleep after Stop left sharing idle with no desired request for the 38.052-second post-resume observation window. |
| Recovery | Cancellation interrupted a real connecting state. Crash recovery restarted idle and removed owned resources. Upstream loss and radio off/on preserved the profile set and allowed explicit sharing restart. |

Both devices were used for traffic qualification. The in-flight package
transitions used the USB tablet, avoiding dependence on wireless ADB while its
AP interface was intentionally removed. Interrupted traffic was expected;
uninterrupted sharing is not claimed.

## Downgrade to the published 0.1.2 release

With verified tablet traffic running, the NetworkManager candidate was downgraded
to the published 0.1.2 package. The new process was idle with no retained sharing
request. The old AP interface/profile and ownership journal were gone, the
configuration and upstream were unchanged, and the installed executable matched
the published asset. APT removed the incompatible split tray companion while
installing the older bundled package.

The raw test **failed** because service enablement changed from `disabled` to
`enabled`. Inspection of the published package's maintainer hook confirmed its
conditional enable operation. This transaction began with a default-disabled
candidate service; it does not establish preservation of every possible
administrator/helper-state history. Enabling the daemon did not resume sharing.

The disabled policy was reapplied and verified as a separate administrative
step, leaving the failed raw result intact. Explicit Start and a complete
verified HTTPS transfer then passed on the published executable. Downgrades to
this artifact require reapplying and verifying the intended boot policy;
straight APT downgrade is not certified to preserve it. The published asset was
not modified or replaced.

## Restoration and remaining limits

The original hotspot installation, file bytes/modes, configuration, service
state, station connection, forwarding and absence of owned firewall/AppArmor
resources were independently verified. Fourteen dependency packages were
protected from autoremove while the original application was absent; their
original automatic/manual marks and configured dependency state were restored.
Unrelated system changes were retained.

Both test networks were forgotten, the original saved-network sets preserved,
and power/timeouts restored. Device probe/certificate/UI files, the temporary
server/tunnel and the qualification root screen were removed or closed. Raw
logs, client identifiers, network details, credentials and rollback archives
remain private.

Remaining qualification limits are the observed upstream/Internet instability,
additional hardware/bands/kernels and historical downgrade boot-policy handling.
See the [release ledger](release-readiness.md). No release asset was published.
