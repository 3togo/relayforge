# Rust hostapd and dnsmasq runtime

Native create_ap now delegates hostapd/dnsmasq configuration, launch, readiness
and foreground process supervision to Rust. The desktop D-Bus methods, signals,
authorization and JSON replies are unchanged. The compatibility backend and all
48 pinned vendor files remain intact. Staging never activates a service.

## Configuration

`daemons-configure DIR CHANNEL BAND DRIVER` reads the supervisor's private frozen
configuration using the recorded random pidfile marker. It requires explicit
credentials and validates the complete native schema, snapshot ownership and
radio plan. Native mode supports nl80211 and the existing desktop fields;
legacy-only driver overrides and arbitrary extra daemon arguments are declined.

Rust atomically writes single-link mode-0600 hostapd.conf and dnsmasq.conf files.
SSID bytes use hostapd's hexadecimal `ssid2` form, avoiding parser ambiguity while
preserving the advertised SSID. Passphrases/PSKs remain literal validated values
inside the private hostapd file. WPA1/2, the existing WPA3 transition rendering,
hidden/isolation flags, MAC filtering and the supported n/ac/ax flags retain
the pinned engine's configuration semantics. This is not live WPA/driver
certification. The native schema does not expose arbitrary HT/VHT directives.

DNS configuration binds explicitly to the owned AP and its gateway, retains the
/24 DHCP range, router/custom DNS options, upstream MTU when available and the
hosts-file option. DNS-disabled mode uses port zero while retaining DHCP;
NO_DNSMASQ omits the DNS process entirely. Bind-dynamic requires dnsmasq 2.63 or
newer. Supported renderer semantics are compared to the pinned Bash template
without starting a daemon; the installed dnsmasq parser is exercised with
`--test`, which checks syntax and exits. See the [dnsmasq manual](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html).

A private daemons.json manifest binds owner, marker, radio plan, configuration
hashes, executable paths and lease ownership. It never contains plaintext
credentials. Changed files, frozen input, executable plans or ownership prevent
launch. Stop must clear an earlier daemon session before another starts.

## Foreground launch and recovery

`daemons-run DIR` records its own exact identity and holds the per-instance daemon
lock. It checks the AP setup proof and NetworkManager release before launching
anything. Both daemons stay in the foreground. Dnsmasq uses keep-in-foreground,
an empty daemon pidfile option and an explicit unprivileged account; Rust writes
private pidfiles from the actual child PIDs.

Each child initially runs an internal Rust `daemon-exec` gate. The parent records
the kernel boot/start identity before releasing an anonymous one-byte execution
acknowledgement. EOF, missing intent or the wrong child identity cannot execute
a daemon. Arguments contain only private paths and fixed options, never a
password. Environment, output and startup deadlines are bounded. A transient
/proc observation during exec is retried until the exact command is confirmed.
Closing stdout/stderr is allowed; excessive output is a failure.

Readiness requires a live owner and children, matching AP identities and an exact
hostapd_cli `state=ENABLED` response. Only Rust writes the existing readiness
acknowledgement, with the original instance owner PID. The outer native supervisor
still verifies channel and upstream identity before reporting Start success.
A daemon exit, missing owner or interface drift ends the runner and triggers
owned cleanup. There is no automatic daemon restart or upstream reconnection.

Cleanup stops the recorded runner before inspecting its final child journal.
Pidfds, boot/start identities and exact gate/daemon argv cover a crash between
gate release and normal child-identity recording. Recovery never signals a bare
pidfile PID. Successful cleanup confirms child exit, removes only owned lease
data and marks the daemon manifest complete. Errors retain evidence for Stop
retry. Existing outer process-group cleanup remains limited to a retained direct
helper child; recovery does not signal an unanchored group.

## Lease permissions

An unprivileged dnsmasq cannot traverse the mode-0700 credential runtime. Its
lease directory therefore lives at `/run/relayforge-leases/TOKEN`: the root-owned
base is mode 0711, and each instance directory and pre-created lease file are
owned by the dnsmasq account with modes 0700 and 0600. The account must exist,
with a non-root UID and primary GID. No accounts are installed automatically.
The credential runtime retains its original permissions.

Rust records the lease directory's device/inode and verifies ownership, mode,
regular-file type, link count and size when reading leases for desktop clients.
Replacement directories, symlinks and unknown files are preserved and block
cleanup. A crash before directory ownership is confirmed may require operator
review; recovery does not adopt an existing directory just because its name
matches. The fixture uses the same checks with ordinary-user temporary files.

Mandatory access control stays enforced. Distribution policy may need an
administrator-reviewed grant for the new lease location and private daemon
configuration. No AppArmor/SELinux policy was changed by this extraction.

## Instance and hardware boundary

The [Rust instance runner](native-instance.md) now parses options, manages its
instance markers and dispatches Rust transactions. [Startup radio and driver
checks](native-startup-radio.md) also run in Rust. Optional PPTP module loading
has been removed from native mode. Native supervision always supplies
--no-haveged; manual native use without it is declined. New native payloads
contain no Bash backend.

Fixtures execute disposable Rust child binaries, private kernel/NetworkManager
state and the unchanged desktop client. They cover rendered settings, disabled
services, child failure/output limits, readiness deadlines, supervisor death,
interrupted identity recording, lease isolation and desktop failure/cleanup.
They do not establish actual privilege dropping, mandatory access-control policy,
real hostapd control-socket behavior, driver behavior, DHCP/DNS/NAT connectivity
or deployed upgrade/rollback. Physical activation remains deferred to preserve
the existing Wi-Fi connection.
