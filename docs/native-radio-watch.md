# Wi-Fi radio watch

NetworkManager publishes `WirelessEnabled` whenever a hardware or software kill
switch changes. Losing the radio invalidates every owned AP: the interface can no
longer carry traffic, yet a status snapshot would still report it as active.

The pinned Python engine handled this twice — a `PropertiesChanged` subscription
and a 2 s `nmcli radio wifi` poll — and stopped the hotspot when the radio went
away. `crates/relayforge-service/src/radio_watch.rs` restores that behaviour for
the native modes.

## Behaviour

- Subscribes to `org.freedesktop.DBus.Properties.PropertiesChanged` on
  `/org.freedesktop/NetworkManager` for the `org.freedesktop.NetworkManager`
  interface.
- Resolves NetworkManager's bus-assigned unique owner first, so the match rule
  never depends on well-known-name resolution, and keeps resolving it so a later
  NetworkManager start is picked up.
- Acts only on `WirelessEnabled == false`, and only when the signal comes from
  NetworkManager. A signal emitted by any other client is ignored.
- Suppresses the stop while the logind watcher owns a sleep transition, so
  suspend cleanup is never duplicated.
- Runs `Stop` through the normal request path, so cleanup, ownership release and
  status publication are identical to a user-initiated stop. It never starts
  sharing: toggling the radio back on is not a request to share.

Compatibility mode is unaffected; the pinned engine still owns its own handling
there.

## Boundaries

- A radio signal is not authorisation. The resulting `Stop` follows the same
  ownership and cleanup rules as any other stop.
- If NetworkManager is absent the watcher simply retries; nothing fails.
- Simulated coverage lives in `tests/test_radio_watch.py`. No physical radio,
  kill switch or adapter was exercised.
