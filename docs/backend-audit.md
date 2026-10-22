# Native backend migration audit

This audit covers the Rust create_ap supervisor, instance protocol, child runtime,
device/interface ownership, forwarding/firewall recovery, native NetworkManager
controller, configuration guards and desktop contract. Source inspection was
paired with offline fixtures; no physical hotspot or deployment was performed.

## Fixed findings

### Connected-subnet overlap was not checked in create_ap mode

The native NetworkManager controller rejected a /24 hotspot overlapping an
existing connected network. The native create_ap path could assign its gateway
without that check, potentially competing with an upstream route.

Both modes now use the same pure IPv4 overlap parser. The create_ap path inspects
connected routes before interface creation and again before address setup.
Identity-recorded AP routes are excluded on retry; the parent and unrelated
devices are included. Broader, narrower and host routes count as overlaps.
Malformed route data blocks setup. Fixture checks verify rejection before any
interface or NetworkManager mutation, and a collision arriving between creation
and configuration is also refused.

### Final cleanup could lose its remaining recovery evidence

Resource cleanup was verified before deleting instance files, but a process death
during deletion could remove interface journals or the final recovery snapshot
before the instance directory disappeared. The supervisor then correctly refused
to guess ownership, requiring operator review despite clean networking.

A durable completion record now precedes all destructive file retirement. It
binds the validated snapshot, directory device/inode, known file fingerprints,
empty control-directory identity and PID/readiness marker fingerprints. A shared
retirement lock serializes completion and resumption. The record stays until
the directory and markers have been removed and synced. Pending retirement
prevents reuse of the instance name.

Direct recovery and outer service recovery can finish partially removed records,
including when the recovery snapshot or directory is already gone. Resumption
performs no network mutations. Changed files/markers, copied replacement
directories, hardlinks, symlinks, unknown files and malformed completion records
remain explicit failures. Credential text is absent from completion records.
Existing unprovable historical state is still retained instead of adopted.

Offline fault controls exercise interruption after proof creation, marker
deletion, ordinary file deletion, snapshot deletion and directory removal.
Private-bus Start/Stop tests verify the outer journal remains until retirement
finishes and a later Stop succeeds without changing simulated upstream state.
These controls exist only on the explicit ordinary-user fixture platform.

## Retained boundaries and remaining gaps

| Area | Audit disposition |
| --- | --- |
| Desktop controls | D-Bus identifiers, methods, signals, authorization and reply formats retained; existing client fixtures remain the compatibility gate |
| Python/Bash source | Compatibility backend and pinned vendor snapshot intentionally retained; new native payloads contain ELF service/helper and policy files |
| Historical helper recovery | Exact argv/birth checks and explicit old-helper support retained; no raw-PID or global orphan adoption |
| Upstream/regulatory changes | Bridge mode, band switching, regulatory overrides and active-parent reuse remain conservative refusals |
| Kernel helpers/firewall policy | No automatic PPTP module loading or global firewalld-policy rewrite; distribution policy integration remains pending |
| Automatic resume | [Native suspend/resume](native-suspend.md) now retains an authorized request through logind sleep; live hardware validation remains pending |
| Configuration migration | Fresh credentials are random; explicit existing credentials are required. Legacy factory-password rotation and installer repair hooks remain separate work |
| Publication residue | Unpublished private pending directories have no networking resources; they remain unadopted and may require administrative review |
| Hardware and deployment | Live driver/control-socket behavior, dnsmasq privilege dropping, mandatory access control, DHCP/DNS/NAT, desktop authorization, install/upgrade/rollback and multiple-driver checks remain gates |

The remaining entries do not establish production parity merely because the
native implementation is Rust. Hardware and deployment validation need a
controlled activation window that is outside this non-disruptive audit.
