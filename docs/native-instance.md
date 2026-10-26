# Rust native instance runner

The native create_ap helper is now Rust. Staging keeps the existing
`usr/lib/relayforge/native/create_ap` path but copies the service ELF there
instead of deriving a Bash adapter. The desktop D-Bus name, methods, signals,
authorization, status and client formats remain unchanged. No new desktop
controls, installer, activation or package release are introduced.

## Options and execution

The outer supervisor still records the gated child's boot/start identity before
acknowledging execution. It detects ELF helpers and executes the native
`--create-ap-instance` entrypoint directly, without launching Bash. Credentials
remain in the private frozen configuration and hostapd file, never in argv.

The internal helper accepts exactly the options supplied by that supervisor:
`--config PATH --pidfile PATH --freq-band BAND -c CHANNEL --daemon --no-haveged`,
or `--recover DIR`. Duplicate, missing, unknown and noncanonical options are
declined without echoing supplied values. This is an internal lifecycle protocol,
not an implementation of create_ap's historical public command-line interface.
Desktop controls continue using D-Bus for configuration, clients and lifecycle.
`--daemon` retains the marker protocol; the helper stays in the foreground.

Configuration must be a private, owner-matched, single-link regular file at the
exact path derived from the random pidfile marker. Rust parses the existing
record format, validates the complete native schema and requires explicit
credentials. Paths, band and channel must match the frozen plan. There is no
shell sourcing, eval, positional credential input, factory-password fallback or
automatic option rewriting. Bridge and regulatory overrides remain declined.

## Publication, signals and transactions

Rust creates private runtime directories and a mode-0700 instance directory.
It binds the directory device/inode into the recovery snapshot and writes and
syncs the owner identity, snapshot and PID before atomically
publishing the instance with rename-without-replacement. A collision never
overlays another instance. No network operation starts before publication.
An interrupted unpublished `.pending-TOKEN` directory is not adopted or scanned
as an active hotspot; it can require administrator review, but has no networking
resources. Shared allocation and forwarding locks remain in the engine root.

The runner dispatches existing Rust radio, device, daemon, interface, forwarding
and firewall transactions directly, consuming their typed snapshots without
shell output evaluation. It writes the existing private PID marker before
launching the separately supervised foreground daemon runner. Physical-interface
configuration intent is already durable before its configured flag is recorded.

SIGUSR1, SIGTERM and SIGINT request cleanup; SIGUSR2 requests failure with cleanup.
Handlers are registered before publication. Startup can be cancelled while an
asynchronous transaction runs; its existing durable intent remains available to
cleanup. Unexpected daemon exit follows the same cleanup path. The outer service
retains its pidfd and exact-argv checks, bounded escalation and direct-child group
cleanup. Fixture process identities are accepted only in explicit test mode.

## Cleanup and recovery

Cleanup holds an instance lock, quiesces recorded children, checks interfaces,
then attempts owned firewall removal, forwarding release, interface removal,
NetworkManager restoration and final resource verification. Failures retain
recovery evidence. `--recover` refuses a live owner and never adopts bare PIDs.

Housekeeping first checks every instance entry against known names and regular
file/owner/link/permission/size rules. It verifies the retained directory's
device/inode and each file's device/inode/content before deletion. Symlinks,
hardlinks, unknown files and replaced directories block removal. After confirming
all recorded daemons have exited, cleanup may retire the exact AP's leftover Unix
datagram control socket: it must be owner-matched, single-linked, unchanged during
inspection and refuse a connection. Live sockets, unexpected names, symlinks and
regular files are retained. Only the resulting empty, owner-matched control
directory can be removed. Any existing service PID/readiness markers must still contain the exact
recorded owner's PID. No recursive directory deletion or orphan sweep runs.

After resource verification, Rust persists a mode-0600 completion record in the
engine root before deleting markers or instance files. It binds the snapshot,
directory identity, known file fingerprints and any existing PID/readiness
markers. The completion record is deleted only after all owned files, markers
and the directory are gone. Recovery can therefore resume disk-only retirement
after the snapshot or even the instance directory has been removed, without
replaying network mutations. The outer supervisor finishes matching completion
records before clearing its own journal or frozen credentials.

Resumption rejects unknown or changed remaining files, replaced directories,
changed markers and malformed completion records. Missing files are accepted
only under a valid completion proof. A pending completion also prevents reuse of
the instance name. Historical snapshot-less directories without such proof
remain unconfirmed; unpublished `.pending-TOKEN` directories are not adopted.

Historical Bash helper identities and snapshots remain readable during a
controlled migration. The outer supervisor can still run an explicitly installed
old script; new ELF helpers can clean compatible private snapshots after their
owners exit. Legacy shared forwarding backups are not adopted. The historical
adapter generator remains for offline protocol/parity tests and is never staged.
The original Python/Bash compatibility backend and pinned vendor files remain
unchanged.

## Test boundary

`--test-ap-instance ROOT ...` is an explicit ordinary-user fixture entrypoint.
Production never selects fixtures from environment variables. Tests execute
the same parser, publication, signals, transaction dispatch and housekeeping
against private temporary network state and disposable children. Desktop tests
exercise the runner through the unmodified Start/Stop/client protocol.

These checks do not establish real driver behavior, hostapd control sockets,
dnsmasq privilege dropping, mandatory access-control policy, DHCP/DNS/NAT,
installation or upgrade/rollback. Physical activation remains deferred.
