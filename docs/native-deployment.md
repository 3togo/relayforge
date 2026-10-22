# Native offline installation, upgrade and rollback

The Rust executable now includes a deployment engine for a deliberately selected,
dormant filesystem image. It installs the existing staged native payload, upgrades
it with durable before/after receipts, and restores the preceding files on
rollback. It does not execute installed code, contact a bus, run systemctl, enable
units, reload policy or activate sharing. The live `/` root is refused.

This is the filesystem deployment phase. Live service handover, package-manager
integration, release signatures and physical activation remain separate work.
The existing GTK, tray and extension files are outside its managed paths and
remain unchanged. Public D-Bus identifiers, signatures and policy are retained.

## Prepare and review

Build and stage as an ordinary user using the existing staging tool:

```sh
cargo build --release --workspace --all-targets --locked
python3 tools/stage-native.py --binary target/release/relayforge-service \
  --output artifacts/native-payload --backend create_ap
```

The selected offline root must already exist. Production deployment requires
root ownership of the image, source payload and protected ancestors; the image's
`etc` directory must exist before configuration initialization. Review the staged
files and transfer them to a protected, root-owned location before invoking the
root deployment command. Hashes detect corruption and modification; they are
not a publisher signature or proof that an arbitrary executable is trustworthy.

Fresh configurations use the existing native initializer against the image:

```text
relayforge-service --init-config /srv/relayforge-image/etc/wifi-hotspot.conf \
  --backend create_ap --wifi-interface wlp2s0 --ssid "My hotspot"
```

It creates a random private password and never replaces existing configuration.
An existing image must instead already have a valid, single-link regular
`etc/wifi-hotspot.conf`, mode 0600, with explicit credentials and the requested
BACKEND. Deployment does not change or back up configuration bytes, rotate
credentials or infer the build machine's adapters. User edits survive upgrade,
interrupted recovery and rollback. Missing or invalid configuration must be
resolved explicitly before a mutating transaction can finish.

The following examples operate only on the specified image and protected payload:

```text
relayforge-service --deploy plan --root /srv/relayforge-image --payload /srv/native-payload
relayforge-service --deploy install --root /srv/relayforge-image --payload /srv/native-payload
relayforge-service --deploy status --root /srv/relayforge-image
relayforge-service --deploy rollback --root /srv/relayforge-image
relayforge-service --deploy recover --root /srv/relayforge-image
```

`plan` creates no files and reports the backend and changed payload paths. It is
an advisory snapshot; install revalidates under its deployment lock. `status`
reports the current receipt revision, pending transaction and rollback availability.
Neither prints credentials. Every command checks for pending networking evidence;
none removes it to force an upgrade. A held native runtime lock also blocks work.
Use the previous matching service/helper to finish resource cleanup first.

## Managed files and payload verification

Only eight reserved payload slots can change: the service ELF, optional create_ap
ELF helper, two native unit paths, compatibility bus activation file, bus and
Polkit policies, and license. A package must contain exactly one complete backend
manifest: six files for NetworkManager or seven for create_ap. Native helper and
service copies must be identical. Executable header, size, hash and permissions
are checked; integration templates must match those embedded in this installer.
The helper and service are ELF64; target architecture compatibility still needs
a deployment/release check. Backend switching is refused for an existing receipt;
it requires a separate explicit configuration/service migration.

A first install captures pre-existing files in these reserved slots, including
activation/policy files it will replace or a native unit it will retire. Rollback
can restore that pre-installation baseline. Unrelated desktop files and paths
outside the allowlist are never adopted. Existing file bytes, modes and group
ownership are retained in private backups; all accepted files belong to the
expected deployment UID. Set-ID/sticky file modes and extended metadata such as
ACLs, capabilities or security labels are refused rather than silently lost.

All image traversal uses pinned directory descriptors and Linux openat2 with
BENEATH, NO_SYMLINKS and NO_XDEV. Symlinks, bind mounts and nested filesystem
mounts are refused, including file mounts. Hardlinks and special files are
refused. A flat local Linux image with working file/directory fsync and flock is
required. The explicit ordinary-user `--test-deploy` path runs the same transaction
engine against disposable images and refuses root; environment variables cannot
turn production deployment into fixture mode.

## Transaction and recovery

Private state lives at `var/lib/relayforge-deploy`, mode 0700, with mode-0600
receipts, lock and content-addressed blobs. Receipts bind to the image directory's
device/inode identity. Copied, recreated or remounted images whose identity changes
require operator review; receipts are never silently adopted into a different root.
Keep this directory with its original image while a transaction or rollback is needed.

Install captures and verifies the complete old payload, saves all before/after
blobs, then syncs a pending receipt before publishing files. Each replacement uses
a new temporary, restores its group/mode, fsyncs it, renames it and fsyncs its parent.
The shared D-Bus activation file is published last. The current/previous receipt
changes only after every destination is verified. File rollback is a transaction,
not an atomic multi-file switch: the root must stay offline throughout.

After interruption:

- `recover` finishes the pending target; with no pending transaction it is a no-op.
- `rollback` while pending aborts that transaction and restores its original files,
  retaining the older rollback snapshot.
- `rollback` on a completed deployment restores its preceding snapshot. The
  displaced snapshot becomes the new rollback target, so a later explicit
  rollback can switch back. Inspect status after an error before choosing direction.
- Reinstalling an identical payload is a no-op and preserves the rollback point.

Changed installed files, unsafe metadata, corrupt receipts, missing/changed blobs,
unknown record paths and pending networking evidence block mutation. Recovery
first validates every required blob and every destination against the recorded
before/after values. It does not overwrite an administrator's different content
or modes. Administrative writes must remain quiescent during the transaction;
the deployment lock does not coordinate arbitrary external writers.

Old/unreferenced blobs and crash-left temporary files are retained for review;
this phase does not add garbage collection. It also leaves created empty directories
in place when restoring a baseline that had no payload.

## Validation boundary

Tests use the actual optimized Rust executable and staged payloads in disposable
ordinary-user images. Fault controls exist only in explicit fixture mode. They
exercise interruption after journal publication, each file slot, all files and
receipt commit; recovery, rollback and rollback-history retention; modified
installed files, unsafe paths/metadata, corrupt backups and runtime/deployment
locks; and exact preservation of configuration and desktop sentinels.

The path-resolution mechanism follows the Linux [openat2 UAPI](https://kernel.googlesource.com/pub/scm/docs/man-pages/man-pages/+/master/man2/openat2.2).
No live service or distro authorization integration is established by these tests.
Real root-owned installation, filesystem/mount layouts, extended metadata,
service-manager/D-Bus reloads, desktop activation/authorization, boot readiness,
physical networking and cross-version configuration/state migrations remain
controlled deployment gates. No native service was installed on either active host.
