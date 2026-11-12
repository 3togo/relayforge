# Installed-system qualification of 8d0a5bb

On 2026-11-12 (UTC), packages assembled from `8d0a5bb` passed all **39
installed-system checks** in a disposable Ubuntu 26.10 x86-64 KVM guest.
[Hosted CI](https://github.com/3togo/relayforge/actions/runs/43140005427) for that
source passed **80 Rust tests, 459 Python tests and 49 check groups**, with no
reported skips. The [receipt](qualification-8d0a5bb-20261112.json) identifies
source, packages, executable, harness and verified image chain.

## Changes and controls

A rebooted guest reproduced a packaged service failing with EACCES while its
AppArmor profile was in complain mode. Adding `attach_disconnected` to the
parent profile allowed real service startup and a successful GetStatus call.
The package now loads its profile before restarting services, including when
apparmor_parser exists without the optional aa-complain utility. Fresh-install
activation with the loaded profile passed in the subsequent VM run. This is a
complain-mode development policy, not validated enforcement; disconnected-name
attachment requires review before treating the profile as an enforcement policy.

The initial desktop check also exposed a harness defect: Shell's bus name can
exist before its extension API is exported. The test-only observer stayed
INITIALIZED and never produced a window inventory. Harness `4ecc12a` waits for
the actual extension API, enables both extensions through that API, verifies
both ACTIVE states, and requires the observer's first sample. The rerun passed
Settings/About window detection and tray startup without authorizing sharing.

## Exact scope

Nine fixture packages used versions `0.1.3~rc1` and `0.1.3~rc2`. They are not
published releases. Their shared Rust executable was reused from the `8f39165`
build: all Rust source files, Cargo manifests and Cargo.lock are unchanged in
`8d0a5bb`. The packaging source and harness revisions are recorded separately;
the later harness and report commits do not imply another executable build.
The exported source receipt records the packaging commit and tree. All three
base packages contain the same stripped executable digest.

The completed run covers fresh installation, idle upgrade and downgrade,
backend replacement, preserved administrator enablement, D-Bus activation,
systemd startup, authorization/credential denial, GNOME controls, companion
removal/reinstallation, removal of either running idle backend, purge and
configuration preservation. Guest package hashes match the host input manifest.
The base image checksum and Ubuntu signature were verified. The stopped disk
from the fresh-image run was reused for the corrected desktop harness; all
backing-image hashes and the new harness hashes were recorded.

Local full-suite runs were rejected, rather than reported as passes. They
exposed inherited scratch permissions and packaging umask issues, followed by
startup/cleanup failures and transaction timeouts during block-I/O stalls.
Kernel I/O pressure and an owned helper waiting in rq_qos_wait were observed;
individual fsync probes took 25–73 ms. The hosted results above are separate
successful runs, not replacements for those local failure records.

The VM shut down after qualification. Host routes and active connections match
the saved baseline. No candidate package was installed on the host. Raw guest
logs, keys, disks and operational inventories remain outside source control.

## Remaining release gates

This run does not qualify active-sharing upgrades, physical NetworkManager or
create_ap traffic, AP+STA, automatic resume, two-client operation, other bands,
additional drivers/client OSes, soak behavior or interactive Polkit prompts.
The earlier physical result for `9526f6d` is not transferred to this candidate.
See [release readiness](release-readiness.md). Stable release qualification
remains incomplete pending an explicitly selected, privileged hardware bench.
