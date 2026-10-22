# Native suspend and resume

Both native service modes coordinate sleep through logind without Python. The
D-Bus name, methods, replies, authorization, StatusChanged and ClientsChanged
signals used by the existing desktop controls are unchanged.

## Authorization and retained intent

A successful explicit Start may authorize recovery of that same sharing request.
Resume intent exists only in memory; service restart performs owned cleanup and
requires a fresh Start. Credentials are not added to public status, argv or a
new persistent resume file.

Arming requires a working logind subscription and a connected NetworkManager
Wi-Fi upstream whose profile UUID, SSID, adapter ifindex/MAC/PHY, BSSID and
frequency agree before and after Start. Failure to establish that evidence does
not prevent ordinary manual sharing, but does not grant automatic recovery.
Dedicated adapters without such an upstream require manual Start after sleep.

Stop invalidates intent immediately, including while a Start is awaiting
authorization, queued behind another operation, or running automatically.
Superseded Start replies cannot report stale success. Successfully changed settings also invalidate intent; external edits
are detected before recovery. Rejected edits do not change authorization.
Unexpected backend loss outside a sleep cycle still requires explicit Start.
This feature does not reconnect Wi-Fi, switch profiles, change regulatory policy,
or adopt another process's hotspot.

## Sleep handling

The service resolves logind to its unique bus owner and subscribes to
PrepareForSleep on that owner, path and interface. Signals from unrelated bus
clients cannot request sleep handling. It also reads PreparingForSleep during
subscription setup. A delay-mode sleep inhibitor is held while awake. Initial setup finishes before
desktop methods are registered. During wake, both explicit and automatic Start
wait for the inhibitor for that sleep generation to be acquired.

Before sleep, Rust blocks new Start requests, cancels pending startup, and runs
the existing backend's owned cleanup. Cleanup must be confirmed before the delay
file descriptor is released. logind's configured maximum delay still bounds how
long the OS waits; this service does not block sleep indefinitely. Unconfirmed
cleanup retains ownership evidence, reports failure and prevents recovery.
Cleanup is retried with a delay, and desktop Stop remains available.

Status keeps its normal JSON shape. Completed cleanup reports inactive sharing,
zero clients and state `suspended`, retaining desired_active only when an
already-authorized recovery intent exists. After wake it reports `waiting` while
that intent is pending, or `idle` when there is no request.

## Recovery after wake

The inhibitor is reacquired after wake. Recovery first waits four seconds, then
requires four seconds of stable observations of the original profile and adapter.
Roaming to another BSSID/channel on that same profile can recover after the new
association stabilizes. A different profile, absent association or replaced
adapter never qualifies. Fresh radio/regulatory preflight is required.

The exact stable observation is passed into backend startup and checked again
before resource creation. Existing frozen-plan, ownership, subnet, interface,
forwarding, firewall and daemon checks continue to apply. As with ordinary Start,
external reconfiguration can race observations; existing monitoring then stops
owned sharing instead of modifying the upstream.

A failed automatic Start is followed by confirmed owned cleanup before retry.
Attempts back off exponentially, capped at 64 seconds, and must again satisfy
association stability. Status reads do not initiate recovery. Stop and another
sleep event cancel an automatic Start through the existing cancellation channel.

Logind disappearance, replacement, malformed signals or failed inhibitor setup
invalidate recovery intent. An established monitor's loss also cancels startup
and schedules owned cleanup. Subscription is retried after five seconds;
reconnection alone never restores the previous authorization. Losing logind
during a known sleep transition keeps Start blocked until a trusted awake
snapshot or signal returns. Stop remains available.

## Verification and limits

Private-bus tests use a fake logind with real Unix descriptor passing, a simulated
NetworkManager/kernel and disposable child processes. They verify inhibitor
release/reacquisition, native create_ap cleanup/restart, original-profile guards,
roaming, startup cancellation, Stop races, cleanup failure and retry backoff.
Pure policy tests advance a supplied monotonic observation time.

The implementation follows logind's [D-Bus interface](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html)
and [delay-inhibitor contract](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md).
No live suspend, installed-service activation or physical hotspot is required by
these tests. Real sleep timing, drivers, DHCP/DNS/NAT and distribution authorization
remain hardware/deployment gates; simulated results do not certify them.

The [race audit](native-suspend-audit.md) records cancellation, stale-observation,
inhibitor-handshake and late-configuration fixes and their regression coverage.
