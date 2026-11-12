# Package rollback and boot policy

Use the checkout's installer when downgrading an existing native installation to
published 0.1.2. That release's maintainer hook can enable a previously disabled
daemon. The current package hook does not enable it, but cannot change a hook
inside an older published archive.

```sh
sudo /usr/bin/python3 -I tools/install-packages.py \
  --receipt /root/relayforge-rollback.json \
  /absolute/path/to/relayforge_0.1.2_amd64.deb
```

Verify the downloaded package against its release digest before running it.
The command accepts local RelayForge base and optional companion packages, saves
the original service enablement in a new private receipt, runs APT with downgrade
and existing-configuration preservation, then restores and verifies boot policy.
Restoration also runs when APT fails. A failed installation or restoration returns
failure and leaves its receipt for recovery. APT may remove companions that are
incompatible with the older base package.

Use it for the same native backend with ordinary persistent `enabled` or
`disabled` units. Masks, runtime enablement and other unsupported states are
rejected before APT. Backend changes require a separate reviewed migration.
Keep the checkout and receipt outside paths replaced by the package. Do not run
another package or service-policy operation concurrently. This command does not
stop or restart sharing itself; package replacement interrupts active sharing,
and an explicit Start may be needed afterward. Daemon boot enablement and a
request to share are separate settings.

If the machine loses power during APT, read the receipt's `before` values,
complete or repair the package transaction, restore those values with
`systemctl enable` or `systemctl disable`, and verify `systemctl is-enabled`.
The command cannot guarantee restoration across power loss or termination of
the installer process. Plain APT downgrade to the unmodified 0.1.2 archive still
has its historical enablement behavior.
