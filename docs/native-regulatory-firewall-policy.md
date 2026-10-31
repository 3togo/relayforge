# Native regulatory and firewall policy

Task 17 follows an administrator-owned policy: RelayForge observes existing
settings and refuses incompatible startup. It does not provision global radio,
kernel-module or firewall-manager settings during hotspot activation.

## Country is a requirement, not an override

For native create_ap, optional `COUNTRY=XX` requires the selected radio's existing
domain to match. The service checks it before helper launch; the helper checks
again before allocating the AP. It uses bounded, read-only
`iw phy phyN reg get`, requiring exactly that radio, one recognized country/DFS
header and a nonempty rule listing. Global-only, incomplete, ambiguous, changed
or unsupported observations fail closed. There is no fallback to a different
radio or global country. The existing iw channel/AP/concurrency checks still
decide whether the selected channel is permitted.

A private radio baseline binds a normalized hash of the selected radio's domain
and rule observation, in addition to existing configuration, parent/PHY/driver
and upstream identities. Changed country or rule data prevents daemon launch;
the active supervisor also rechecks it about once per second, after each bounded
query completes. The final daemon exec gate rechecks the baseline. Observation
is not atomic exclusion against another administrator's changes.

RelayForge does not issue `iw reg set`, render hostapd `country_code` or country
advertisement directives, or change another radio's policy. A match is only a
configuration constraint, not a regulatory approval or a guarantee of usable
channels. Remove COUNTRY or provision the intended domain through the system's
normal administrative policy before retrying. No country is selected on behalf
of the user. With COUNTRY absent, ordinary permitted-channel behavior remains.
The native NetworkManager backend keeps its existing refusal of COUNTRY overrides.

No regulatory restoration is needed because no regulatory change is acquired.
Cleanup/recovery does not require the current country to match, and old baselines
without regulatory hashes remain readable. Existing owned interfaces/daemons can
be cleaned after the observed domain changes.

This country contract requires the native ELF helper. Production preflight and
the final execution gate refuse passing COUNTRY to a retained compatibility
script. Ordinary-user test wrappers are explicitly limited to private fixtures.

## Scoped iptables and active firewalld

The native create_ap NAT/no-sharing path still owns only its existing marked
iptables rules and forwarding leases. Before startup, each rule insertion and
daemon launch, it queries the system bus daemon for ownership of
`org.fedoraproject.FirewallD1`. This query does not activate or call firewalld.
An active owner or an unavailable/failed observation prevents new setup. The
supervisor repeats the check while running. Installing firewalld without running
its service is not itself a refusal.

Active firewalld receives actionable guidance: use the native NetworkManager
backend with its supported defaults, or a separately validated administrator
integration. Leave firewalld running. RelayForge does not disable/reload it, move
interfaces to a trusted zone, add direct/passthrough rules, or change runtime or
permanent firewalld policy. A name-owner check does not prove all other firewall
managers absent or prove packet delivery; coexistence with other policy still
needs isolated traffic qualification.

Cleanup does not depend on firewalld being absent. It removes only recognized
owned iptables rules through the existing marker/identity checks and retains
uncertain ownership evidence. Existing-bridge mode acquires no local firewall
rules and therefore skips this local iptables-manager refusal; it does not claim
that the administrator bridge's firewall permits traffic.

The [firewalld direct-interface contract](https://firewalld.org/documentation/man-pages/firewalld.direct.html)
describes nftables acceptance caveats; direct iptables ACCEPT rules do not prove
end-to-end acceptance. Its [D-Bus contract](https://firewalld.org/documentation/man-pages/firewalld.dbus.html)
identifies the manager name/interface. Those constraints support explicit refusal
until an owned native firewalld integration is implemented and distro-tested.
The [Linux regulatory processing documentation](https://wireless.docs.kernel.org/en/latest/en/developers/regulatory/processing_rules.html)
explains that observed country information can change through normal association;
therefore a configured country string alone cannot establish lasting permission.

## Optional kernel and MAC behavior

The reference create_ap path unconditionally attempts `modprobe nf_nat_pptp` for
NAT. Native mode does not load it or edit helper-related sysctls. PPTP behavior
requires separate administrator provisioning and traffic validation; no support
claim is inherited from a best-effort module load.

Public configuration does not accept parent-MAC overrides. Generated addresses
remain limited to the owned virtual AP path; dedicated-adapter restoration keeps
its existing identity guards. Global bridge-netfilter, firewall defaults, STP and
regulatory policy are not rewritten. Administrator-mutating variants remain open
until an explicit ownership/restoration contract and isolated system tests exist.

Private fixtures cover policy changes, insertion boundaries and recovery without
touching real radios, modules or firewall services. They do not qualify real
firewalld distributions, drivers or traffic. Exact evidence is recorded in
`native-regulatory-firewall-validation.md`.
