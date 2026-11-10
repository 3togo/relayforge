# Live tray authorization update

Applied on 2026-10-23 (UTC) to the Ubuntu 26.10 (Stonking Stingray), x86_64,
XFCE desktop installation. This follows the isolated qualification recorded in
[native-system-integration-validation.md](native-system-integration-validation.md).

The live installation now uses the exact validated NetworkManager packages:

| Package | Version | SHA-256 |
| --- | --- | --- |
| relayforge | 0.1.0~system2 | `5733c453ab5d671d78fa82ed0b9960f502a2850c7fc57a85a0186000144c7c51` |
| relayforge-tray | 0.1.0~system2 | `4eb855a10577dc8aa3fab555ff4a8b8b2e661bd5dc4f5ecf3133f4c0d4b12cc7` |

These are qualification fixture versions, not published releases. The installed
service SHA-256 is
`fc56f938544add5860d6215eb213c2146c69dab065d86494b5ef65bca8444fee`,
matching build source ID
`85e106df770fb3a46dcc001f9600db6cc9dbe5bbc81de31585cfdd3d097d5f3b`
and the source receipt in the qualification report.

The installer required one administrator authorization to replace system files.
It retained a private root-owned rollback backup of the previous package,
actual installed files and configuration. No unrelated packages were installed
or removed. Only the idle RelayForge service was stopped and restarted; its
activation was temporarily masked during replacement.

Passing live checks:

- The hotspot was inactive and not requested before installation and remained
  so afterward. No ownership/recovery journal was present before replacement.
- NetworkManager's process ID and activation timestamp, device states, active
  connection identities/timestamps, and Wi-Fi IPv4 address/gateway observations
  matched exactly across installation.
- Private hotspot configuration and user preference fingerprints were unchanged.
- The tray was replaced as the ordinary desktop user without invoking hotspot
  Stop. Its session-bus owner resolved to the newly launched installed tray.
- Real Polkit `pkcheck` for `.control`, targeting that tray process without
  interactive authorization, returned success. The installed policy grants
  active local users `.control`; `.manage` retains administrator authorization.
- A fresh GetStatus after tray replacement remained inactive/unrequested, and
  the active Wi-Fi device remained connected.
- The unrelated deployment edits retained their exact combined diff SHA-256:
  `9b0f2a33dc5632a7d67666d820fe538d4ec33976ed3cb0530354a6278466a395`.

No Start, Stop, Wi-Fi reconfiguration, firewall change or physical sharing test
was requested as a validation action. The tray restart and authorization checks
establish the live process and password-free control grant; actual hotspot
activation remains outside this non-disruptive check. Settings opens locked;
explicitly unlocking credential-bearing settings still requests authorization.
Private operational receipts and rollback locations are retained outside source
history. No commit or push was performed for this application step.
