# Disposable OS integration qualification

Run `run.py` as an ordinary user on a KVM-capable Linux build machine. This is
an explicit integration check, separate from the fast offline suite. Never run
`guest.py` directly on a development or Wi-Fi host: it installs and removes
packages and starts guest services. It refuses execution without both the
cloud-init marker and KVM/QEMU virtualization detection, and requires zero
wireless radios.

Obtain a matching Ubuntu cloud QCOW2 image from
[Ubuntu Cloud Images](https://cloud-images.ubuntu.com/). Verify its SHA-256
against the signed `SHA256SUMS`, using the distribution-provided Ubuntu cloud
image keyring. Preserve the image digest and signature verification result.
The runner records the image and every package digest again.

Build actual release ELF packages as an ordinary user using `tools/build-deb.py`
with `--with-gnome`. Provide these directories under a directory named
`packages`, each containing base, tray, and GNOME `.deb` files:

- `create_ap-1`: older test version.
- `create_ap-2`: newer test version.
- `networkmanager-2`: the newer version selecting the NM backend.

Use the same reviewed source/binary for the lifecycle versions. Record the
binary's actual build source receipt separately; a version suffix alone does
not establish source identity.

```sh
python3 tools/system-validation/run.py \
  --image /absolute/path/verified-base.img \
  --packages /absolute/path/packages \
  --output /absolute/path/new-run-directory
```

The output directory must not exist. The runner uses a private writable QCOW2
overlay and ephemeral SSH key with a dedicated known-hosts file. QEMU's
unprivileged user network supplies guest repository access and one SSH forward
bound to loopback. It creates no host TAP, bridge, or Wi-Fi interface. There
are no host filesystem, system-bus, display, GPU or USB passthroughs. Guest
root is used only inside that VM. The VM is powered off in `finally`, including
on test failures; interrupted outer processes still require checking that the
specific QEMU process has stopped before discarding its files.

The guest exercises apt dependency resolution, idle upgrade, downgrade,
backend replacement, D-Bus/systemd activation, root Polkit authorization,
unprivileged credential denial, settings/tray startup, real GNOME Shell
extension loading, removal/purge, and configuration/preferences preservation. Purge uses real dpkg after apt
removal, since a removed local-only package may no longer be resolvable by apt.
The desktop uses software rendering and virtual displays. No sharing is
started. This does not certify physical AP operation, an interactive Polkit
agent prompt, active-hotspot upgrades, or every declared GNOME version.

Keep raw logs and guest disks outside source control. Record verified guest OS
and tool versions, exact source/package identities, results and limitations in
a sanitized validation report. Read-only host network/activation inventories
before and after the run establish that the test did not change the live
station connection; they are private operational records.

The runner freezes all four executable harness files and records their hashes
before boot. Do not replace guest scripts during a run. A stopped disk created
by this harness may be reused to avoid repeating dependency downloads: all
backing-image hashes are recorded, a new cloud-init instance/key is used, and
only a recognized rftest0 configuration may be reset. Previous guest reports
and private fixture configuration are retained inside the disposable disk.
Never use an existing user VM as that input.

The local-console permission probe creates a bounded guest-only PAM tty
session on seat0. It supplies the seat/VT metadata explicitly, verifies the
registered session, and checks the installed Polkit defaults without adding
Polkit override rules or starting sharing. Protected management returns a
noninteractive challenge; that is an expected refusal, not a failed check.
