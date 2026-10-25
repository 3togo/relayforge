# Native configuration and interface inventory

Rust now owns GetConfig, SetConfig and GetInterfaces. Their D-Bus signatures,
JSON shapes are unchanged. Polkit still authorizes every protected call, but the
shipped action now grants the active local session without prompting (see
[packaging/io.github.erhanzeyrek.WifiHotspot.policy](../packaging/io.github.erhanzeyrek.WifiHotspot.policy));
the pinned upstream file asks for an administrator password on every call. All 48 reused desktop/backend
source files remain byte-for-byte identical to the original pinned snapshot.

## Native responsibilities

- Complete defaults and field/cross-field validation, including USE_PSK with
  a 64-digit PSK, UTF-8 SSID byte limits, IPv4 gateway/DNS restrictions and legacy
  key names. Partial updates merge into current settings and retain credentials
  and options absent from the desktop editor.
- Literal KEY=value files, split on the first equals sign, preserving password
  whitespace, quotes, hashes and shell punctuation without shell evaluation.
  Duplicate legacy assignments retain their last value. D-Bus JSON patches
  reject duplicate keys, including escaped spellings.
- Atomic credential storage using a retained directory descriptor, exclusive
  random temporary files, file fsync, renameat2 and directory fsync. Reads reject
  symlinks, hardlinks, FIFOs, wrong ownership, modes other than 0600, oversized
  data and invalid UTF-8. Read/save metadata checks detect external changes during
  I/O and between preparing and committing a replacement. Errors contain no
  credential values. A post-rename directory-sync failure is reported explicitly;
  the caller must reload because replacement may already have occurred.
- Secure initialization of an absent file with 128 bits of OS random data,
  represented by 32 hexadecimal characters. RENAME_NOREPLACE preserves any
  existing path, including symlinks. Initialization never prints the password
  and never rotates an existing password. It keeps the historical `wlan0`
  default unless `--detect-interface` is passed, which adopts the pinned
  installer's rule: use an adapter only when exactly one Wi-Fi device exists and
  none was named explicitly. Image assembly therefore cannot capture the build
  host's adapters by accident.
- Linux interface discovery using sysfs, without iw/ip subprocesses. Lists are
  sorted, loopback is omitted and wireless/phy80211 markers identify Wi-Fi.
  Empty inventories produce empty lists instead of fictitious adapter names.

The pure schema retains historical defaults for compatibility comparisons.
Production disk configuration must explicitly contain PASSPHRASE: an empty or
partially edited file cannot silently select the historical factory password.
Every Start revalidates native configuration before invoking the retained backend
and checks that a concurrent Stop has not superseded that request.

## Ordering and remaining Python boundary

Configuration changes and Start/band changes are serialized in Rust. Stop has
reserved request capacity, bypasses the startup queue and shares its read lock
with Start. Configuration commits use an exclusive lock, preventing a backend
selection change during Stop. Backend selection additionally requires a fresh
legacy ownership/status check; an initial or stale inactive snapshot is
insufficient. Ordinary edits remain allowed while sharing is active and apply
through the existing backend's next-read behavior.

The Python pipe adapter rejects configuration and interface-enumeration requests.
Its internal GetStatus request now inspects live backend state in the original
serialized operation queue. The service still uses Python for capabilities,
activation/deactivation, client observation, ownership recovery, suspend and
resume behavior. Those networking paths read the same atomically replaced file
and retain their own validation. No Python configuration-write method is exposed
through RelayForge's worker protocol.

The old installer helper that rotates an exact factory template is intentionally
not ported: creating a file can never observe a previously customized one, and
system mode always requires an explicit PASSPHRASE. The package-upgrade repair is
ported as an explicit command rather than an implicit hook:

```text
relayforge-service --repair-config-interface PATH [--wifi-interface NAME]
```

It refuses to act when `wlan0` still exists, when the file fails the same
ownership/single-link/0600 checks as every other read, or when several Wi-Fi
adapters exist and none was named. It replaces only the first occurrence of
`WIFI_IFACE=INTERNET_IFACE=wlan0` (the historical corrupted assignment),
`WIFI_IFACE=wlan0` and `INTERNET_IFACE=wlan0`, and rewrites the file atomically
with the original owner and group. Comments, ordering, quoting, CRLF endings and
every other assignment stay byte-for-byte identical, so a failed or repeated run
cannot lose credentials. `--test-repair-config-interface NET_ROOT` and
`--test-init-config NET_ROOT` point the inventory at a fixture directory for
tests; production mode always reads `/sys/class/net`.

System mode still needs a prepared, root-owned installation and policy files; no
installer or live network changes are part of this migration.

## Verification

Automated checks cover native storage failures, credential-preserving saves,
stale external edits, merged PSK validation, persistence across service restarts,
the original desktop decoder/client and exact ten-method/three-signal API,
concurrent configuration editing and startup cancellation, denied authorization,
and removal of the old worker requests. `tests/test_config_init.py` covers fresh
initialization with and without interface detection, and the repair matrix:
obsolete defaults, the historical corrupted assignment, ambiguous or present
hardware, CRLF/comment preservation, symlinks, group-writable files and invalid
interface names. Differential tests compare full-schema
acceptance and normalized values against the unchanged Python reference, and
compare printability for all 1,114,112 Unicode code points. The Rust example
config_oracle exists solely for these tests and is not a production service
command. To build it, use cargo build --workspace --all-targets --locked.

Detailed source revision, environment, executed checks and limits are recorded
in [the validation update](native-configuration-validation.md). Existing physical Wi-Fi Relay evidence
does not certify this Rust implementation.

## References

- [Rust Unix file-open flags](https://doc.rust-lang.org/std/os/unix/fs/trait.OpenOptionsExt.html)
- [Unicode category library and data version](https://docs.rs/crate/unicode-general-category/1.1.0)
- [Python printable-character semantics](https://docs.python.org/3.14/library/stdtypes.html#str.isprintable)
