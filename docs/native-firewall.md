# Rust create_ap firewall transactions

The native create_ap path now delegates all NAT, DNS and DHCP rule decisions,
iptables execution, rule removal and absence checks to Rust. The public desktop
methods, signals, status and authorization remain unchanged. Neither this change
nor staging installs a service, activates sharing or changes an existing network.

This is one complete extraction from the Bash engine. [Daemon configuration and
launch](native-daemons.md) now use Rust; optional module loading and adapter
housekeeping remain Bash.
[Device ownership](native-device-ownership.md) now handles interface creation and
NetworkManager managed-state restoration. [Rust network transactions](native-network-state.md)
now own AP setup/teardown and forwarding sysctl leases. Native mode continues to decline
band switching, bridge mode and COUNTRY overrides. Physical validation remains
pending; the current Wi-Fi connection was not used for activation.

## Rules and ownership

Rust builds structured argument vectors from the private validated recovery
snapshot. NAT retains the reference /24 masquerade and two forwarding rules,
including established/related return traffic independently of the selected
uplink. DNS retains TCP/UDP acceptance on the configured local port and redirects
port 53 traffic from the AP subnet. DHCP acceptance is added only when the Bash
configuration enables dnsmasq. The native backend preserves the existing rule
semantics, including routing through an Ethernet/VPN-preferred uplink.

The adapter saves intent before each phase and invokes `firewall-add DIR PHASE`
for `nat`, `dns` or `dhcp`. All rule arguments are generated in Rust; no shell
command, user-defined chain or arbitrary command is accepted. The gateway is a
numeric IPv4 address, interface names are validated, and the instance comment
must match its private directory. Credentials never enter firewall argv, state
or diagnostics. Disabled DNS legitimately records port zero; it produces no DNS
rules and no snapshot-validation failure.

Application requires the same live owner and a realized, recorded AP identity.
Each insertion rechecks owner/process and interface identity. Existing exact
rules are not duplicated. Successful listings and exact presence checks precede
changes; unexpected tagged rules or duplicates block application until Stop
resolves cleanup. Rule changes use `-I`, `-C` and `-D` with the full specification;
there is no flushing, chain deletion or policy reset. See the underlying
[iptables manual](https://man7.org/linux/man-pages/man8/iptables.8.html).

Cleanup runs `firewall-remove DIR` before the remaining Bash resource restoration.
It removes only exact known rules, including bounded duplicates, then checks
both tables for the exact instance comment. Similar comments and unrelated
rules are preserved. Unknown rules with the exact marker remain in place and
prevent successful cleanup. Rule listings are parsed as literal quoted words,
never executed, and comment comparison is exact rather than a substring search.
A failed query, malformed output or remaining owned rule cannot establish success.

A lost insertion reply retains the saved intent for cleanup. A failed removal
reply is reconciled against observed presence; cleanup succeeds only after
absence is confirmed. Other errors retain the existing supervisor journal and
snapshot for Stop retry. The Bash adapter records failure instead of removing
its recovery directory when Rust cleanup is unconfirmed.

## Process and concurrency boundaries

Each instance has a private no-follow, single-link mode-0600 firewall lock. Add,
remove and absence verification serialize through it. An orphaned Rust helper
retains that lease until its operation ends; recovery cannot claim absence while
that helper is still changing rules. Lock acquisition is bounded, and a timeout
retains evidence. Directory-relative file operations and explicit unlock follow
the existing snapshot/journal rules.

Each iptables process has a bounded xtables wait, execution deadline and output
budget. A whole add/remove transaction also has a deadline. Processes run with a
minimal environment, drained/discarded stderr and a retained process-group guard;
cancellation kills the owned command group. No raw command output is returned as
a desktop error. These rules protect lifecycle consistency; they do not make a
multi-rule firewall update atomic in the kernel.

Staging still emits seven create_ap payload files with no Python backend/helper.
The pinned source hash is verified before transformation. The generator replaces
all sixteen direct Bash iptables commands with three phase calls and one cleanup
call, preserving the reference networking code outside that boundary. It checks
the expected command counts and rejects any remaining direct iptables command.
All 48 vendor files remain unchanged. Generated Bash passes syntax verification.

## Isolated validation and remaining work

The internal ordinary-user fixture uses a private JSON firewall simulator. It
cannot fall back to a system firewall executable, even if fixture state is
missing or invalid. Tests compare the rule plan to the pinned Bash invocations
by substituting a printing function for iptables. Other cases cover partial
insertion, lost replies, idempotency, duplicate cleanup, foreign markers,
unrecognized owned rules, malformed/failed queries, lock contention, unsafe
files, dead owners, changed AP identities and cleanup retry through the unchanged
desktop client. Tests never activate the real Bash networking path.

Live iptables variants, kernel behavior, distribution firewall interaction,
DHCP/DNS/NAT connectivity, upgrade/rollback and driver behavior still require a
controlled hardware window. Use the previous matching payload to Stop and confirm
cleanup before changing a live installation; do not bypass a pending journal.
Daemon configuration/launch now use the [Rust daemon runtime](native-daemons.md). See [native recovery](native-recovery.md) and
[create_ap supervision](native-create-ap.md) for the retained operational limits.
