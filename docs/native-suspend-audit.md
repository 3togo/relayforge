# Native suspend/resume race audit

This audit covers the native service orchestrator, logind subscription/inhibitor
lifetime, retained authorization, backend polling and desktop notifications.
Verification uses a private bus and simulated networking; no machine is suspended
and no installed service is activated.

## Fixed findings

### A pending Start could outlive Stop

Start previously subscribed to cancellation only after obtaining the operation
lock and beginning its lifecycle epoch. A request waiting behind another Start
could therefore run after Stop had already completed. Authorization presented
another waiting window. The post-start upstream probe could also delay a Start
reply and return an already-obsolete successful status after Stop.

Start now retains a cancellation receiver from before authorization and checks
it before any backend work. Queue acquisition itself is cancellable. Authorized
Stop invalidates retained intent and advances cancellation immediately, before
waiting for configuration/backend gates. The post-start probe is cancellable,
and a superseded Start returns a failed reply instead of stale success.

Held-activation, delayed-authorization and held-post-start-probe regressions cover
all three windows. A new Start submitted after completed Stop remains available.

### Stale monitoring could revoke a newer request or republish clients

The native monitor cancelled resume intent on an inactive observation before
checking the observation's lifecycle epoch. A delayed result from an earlier
session could therefore cancel the intent of a subsequent successful Start.
Client replacement also occurred after an awaited status signal, permitting
late client state to cross Stop.

Epoch validation, intent changes, status observation and client replacement now
form one synchronous transaction. Stale epochs and sleep-owned status cannot
change any of them. Status/client publication is serialized and reads the current
stored values after acquiring the publication gate; old captured payloads are
not emitted after a newer Stop. Unchanged clients still avoid redundant signals.
A Rust regression verifies that an old inactive result preserves the new intent,
clients and epoch. Existing asynchronous desktop client tests remain the wire
compatibility gate.

### Startup and wake did not gate Start on the inhibitor handshake

The public controls were registered before the initial logind property/inhibitor
setup. Start could run while a delayed initial snapshot would report sleep in
progress. After wake, receipt of the awake signal alone made manual Start
eligible, even while reacquiring the delay inhibitor was still pending.

Initial setup now completes before desktop methods are registered. When trusted
logind is unavailable, the existing explicit manual fallback remains available.
A successful monitor gates both manual and automatic Start on its active awake
inhibitor. Each sleep transition has a separate generation: an old acquisition
cannot unlock a later cycle, while Stop during reacquisition still allows that
same cycle's valid descriptor to unlock future explicit Start.

Private-bus tests hold the initial property reply and wake inhibitor reply,
verify that Start cannot activate, then release them. Failed reacquisition
invalidates intent and reconnecting the monitor never resurrects it.

### Monitor loss incorrectly implied wake

The fallback loop cleared the sleeping flag when logind disappeared, including
when a trusted sleep transition was still in progress. It now retains a known
sleep gate until a new trusted awake snapshot or signal returns. Stop can still
clean owned state and cancel intent. A logind-loss/reconnect regression verifies
both the retained gate and a subsequent fresh explicit Start.

### Automatic startup could miss a late configuration edit

The configuration comparison preceded an upstream probe that can await several
bus requests. An external edit during that probe could leave automatic startup
using obsolete settings. Configuration is now read again after the stable
observation and before committing a Start epoch. A held-probe regression edits
the private configuration during the last observation and verifies no activation.

## Retained boundaries

The D-Bus identifiers, method signatures, reply shapes, authorization and desktop
controls remain unchanged. All mutation still passes through the existing native
ownership journals and backend cleanup. Intent stays in memory and is limited
to the prior explicitly authorized profile/adapter. Second-sleep cancellation,
failed-inhibitor recovery, cleanup failure and bounded retry behavior remain
covered by private-bus tests.

A final read cannot eliminate an administrator changing files or network state
immediately afterward. Existing frozen-plan checks and monitoring still apply.
Real logind delay deadlines, suspend timing, drivers, DHCP/DNS/NAT, distribution
policy and installation/upgrade/rollback remain controlled hardware/deployment
checks. Offline simulation does not establish those properties.
