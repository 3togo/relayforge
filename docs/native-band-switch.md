# Rust upstream band switching

`SwitchBandAndReconnect` exists because a hotspot may only use channels the PHY
advertises without DFS/CAC. When the upstream station is associated on a channel
that is not permitted, the station itself must move to a BSSID of the same network
that sits on a permitted channel of the wanted band.

The pinned Python engine did this with `nmcli` (scan, BSSID selection, band
pinning and restore). RelayForge now performs the same operation over the
NetworkManager D-Bus API in `crates/relayforge-service/src/band_switch.rs`, with
no shell, no `nmcli` and no credential in an argument list.

## What it does

1. Validates the target band (`2.4` or `5`) and re-reads the live radio snapshot.
2. Refuses the request while sharing is active, starting or stopping: reconnecting
   the upstream would tear down an owned AP's own uplink.
3. Requests a scan and repeatedly reads `Device.Wireless.AccessPoints` until an AP
   serving the current SSID on a permitted channel appears, then picks the
   strongest one.
4. Activates the *saved* upstream profile against that BSSID with
   `ActivateConnection`, and re-inspects the radio to verify the band, SSID and
   channel permission actually changed.
5. If NetworkManager accepted the BSSID but immediately roamed back — a real
   driver/NM behaviour the pinned engine also worked around — retries once with a
   **volatile copy** of the profile whose `802-11-wireless.band` is constrained.
   The administrator's saved profile is never modified.
6. On final failure, reactivates the saved profile as a best-effort restore and
   returns a JSON error, matching the pinned reply contract.

A successful switch cancels any armed sleep-resume intent, because the upstream
connection it was armed against no longer exists.

## Boundaries

- Only APs whose SSID equals the current network and whose frequency is a channel
  this adapter may use for an AP are candidates.
- The upstream profile is never edited; the fallback uses `persist=volatile`.
- The switch changes the *upstream* connection. It is therefore refused whenever
  an owned AP could be affected, and it is not offered in demo mode.
- Scan waiting is bounded (4 s) and activation is bounded (25 s) so the desktop
  controls stay responsive.
- Simulated coverage lives in `tests/test_band_switch.py`. No physical adapter,
  scan or activation was exercised.
