# Explicit legacy configuration migration

`--migrate-config` operates on a protected offline configuration copy. It never
activates sharing, changes an interface, contacts NetworkManager, or installs a
package. Writes to the live `/etc/wifi-hotspot.conf` are refused; a dry-run there
is read-only. Applying a migrated copy to a live installation belongs to the
separate controlled deployment procedure.

```text
relayforge-service --migrate-config /absolute/protected-copy.conf --dry-run
relayforge-service --migrate-config /absolute/protected-copy.conf
relayforge-service --migrate-config /absolute/protected-copy.conf --detect-interface --dry-run
relayforge-service --migrate-config /absolute/protected-copy.conf --wifi-interface wlp2s0
```

The file and its parent must pass the native store's ownership checks. The file
must be a single-link regular file with mode 0600; symlinks, insecure modes,
oversized files, invalid UTF-8 and changed source metadata are rejected. Writes
use the native private temporary-file, fsync and atomic-replacement path.

Password rotation applies only when the entire file matches `compat/factory.conf`,
the exact factory template from Wi-Fi Relay revision `12f9f78`. A customized SSID,
comment, credential, option or line ending makes it ineligible. The replacement
uses 128 bits of OS randomness. A customized configuration retaining the old
password is deliberately left alone; change that password explicitly instead.

Interface repair is opt-in. The command checks the real read-only sysfs inventory
and refuses repair if `wlan0` exists. Automatic detection requires exactly one
Wi-Fi adapter; an explicit replacement must also be observed as a Wi-Fi adapter.
It replaces only literal default assignments and the known malformed legacy
`WIFI_IFACE=INTERNET_IFACE=wlan0` assignment. Other interfaces, literal password
bytes, comments and line endings are preserved. The resulting configuration must
validate before any write. Do not auto-detect interfaces on a build host for an
image intended for different hardware.

JSON output contains only `factory_password`, `interfaces` and `applied` flags.
Dry-run reports planned changes with `applied: false` and leaves the file intact.
A second successful migration is a no-op. No stored password is printed.
