# Feature inventory against Wi-Fi Relay

This is a source inventory, not a release certification. The reference is
Wi-Fi Relay `12f9f78`; the ordered work and acceptance criteria are in
`feature-parity-plan.md`. “Native” means implemented in Rust. “Retained” means
the original Python/JavaScript component still supplies the behavior.

| Reference behavior | RelayForge disposition | Remaining work |
| --- | --- | --- |
| Ten D-Bus methods, three signals and existing desktop identity | Native service, same contract | Keep protocol regressions in all modes |
| Protected settings and privileged requests | Separate noninteractive local control and protected management actions | Real Polkit grants/refusals passed in task 18; interactive Unlock-agent completion remains open |
| SSID, passwords, raw PSK, literal config values and partial edits | Native schema/storage; retained editor | Expanded editor/credential regressions |
| Hidden networks and Wi-Fi QR codes | Retained editor and encoder | Independent decoder is an optional development check |
| Gateway, custom DNS, isolation, MAC filtering, 802.11 options | Schema retained; native create_ap daemon rendering; NM has explicit restrictions | Native configuration/radio blockers are now exposed before activation |
| Interface discovery and radio capabilities | Native sysfs and iw inspection | Managed editor now preserves empty inventories and explains unavailable adapters |
| Client names, addresses, count and polling/signals | Native observations; retained desktop presentation | Real DHCP/hostapd/client validation |
| Start, Stop, cancellation and responsive status | Native intent and worker/controller scheduling | Hardware cancellation/restart checks |
| Error/retry feedback | Retained editor; maintained tray now preserves failure reason and advice | Reasons/guidance and Shell 51/Settings/About startup verified in task 18 |
| Tray Show/Hide and login preferences | Retained private preference files and lifecycle; maintained tray in independent recommended companion | Installed-session lifecycle checks; bundled-to-split and split file-lifecycle fixtures passed |
| Tray Quit stops sharing before exit; closing Settings leaves sharing running | Retained/maintained desktop contract | Broader desktop and service-loss regression coverage |
| GNOME panel and Quick Settings | Extension source retained; optional companion packaging and opt-in first-login setup added | Load/disable passed on Shell 51 in task 18; other versions and running-session upgrades remain open |
| Integrated network applet menu and hosted-AP icon | Optional source restored with RelayForge tray path | Full distribution package build and installed-menu tests |
| GNOME Settings entry point and stable Wi-Fi tabs | Optional patches restored; retained launcher marker contract | Full distribution package build and real-panel tests |
| Experimental NM-owned child AP | Native service understands downstream capability; optional NM patch restored | Exact-version full build, system integration and hardware validation |
| Stock NM helper-owned AP child | Native owned iw child controller | Stock NM/driver hardware validation |
| Dedicated adapter/Ethernet uplink with create_ap | Native plan/runtime implemented | Multi-adapter traffic and restoration checks |
| AP+STA on the same permitted upstream channel | Both native modes implemented | Hardware DHCP/DNS/NAT/HTTPS and driver matrix |
| Automatic band selection without upstream changes | Native permitted-channel planning | Physical regulatory/radio validation |
| Explicit upstream band switch/reconnect | Experimental native BSSID-only opt-in with durable recovery; compatibility path retained | Pinned-profile fallback, provider concurrency qualification and dedicated-radio validation; default native Start preserves upstream |
| NAT and no-sharing modes | Native create_ap transactions; NM shared IPv4 | Physical routing and no-sharing isolation checks |
| Bridge sharing | Experimental native existing-bridge opt-in implemented; journalled owned-child attachment/cleanup and offline recovery tests passed; compatibility path retained | Fixed-MAC administrator bridge required; dedicated isolated traffic/driver qualification, new bridge creation and upstream migration remain open; see native-bridge-validation.md |
| Driver-specific hostapd behavior | Audited nl80211/dedicated-adapter policy; driver errors before helper launch and checks through final exec; explicit transition-mode and raw-PSK handling | rtl871xdrv contract/captures and representative driver/client qualification remain open; see native-driver-security-audit.md |
| Country/MAC overrides, optional PPTP helper and firewalld workaround | Read-only native create_ap country requirement; active-firewalld refusal before local rule setup and during running; owned cleanup independent of policy changes | Global regulatory/MAC/module changes and native firewalld mutation remain administrator-owned/unimplemented; see native-regulatory-firewall-policy.md |
| Scoped firewall, forwarding and daemon ownership | Native transactions and private recovery records | Real distro firewall/MAC-policy validation |
| Crash cleanup and uncertain ownership | Native identity/journal/pidfd and retirement recovery | Real process/driver/systemd crash matrix |
| Sleep and resume of an authorized request | Native logind intent and cleanup | Controlled suspend/resume with real adapters |
| Fresh random credentials | Native explicit initializer | Distribution initialization/lifecycle integration |
| Exact factory-template password rotation and obsolete wlan0 repair | Explicit native offline migration helper added | CLI/storage tests passed; staged deployment and package adoption remain; no live writes |
| Install, upgrade, remove, purge and optional controls | Basic Debian builder; native offline/live deployment machinery | Disposable package/system lifecycle matrix and user-data policy |
| Source/dependency provenance and checksums | Cargo lock, pinned vendor/import manifests and validation receipts | Unified export and CI evidence generation |
| AppStream/About/manpages/source exports | RelayForge AppStream/manpages, package-version About, reproducible committed-source archive and receipt implemented | Installed desktop/catalog presentation; no original release history or hardware claims inherited |
| RPM packaging recipe | Assessed and deferred; original README also labels RPM builds unvalidated | Select and validate a target distribution before claiming support; see packaging-support.md |
| Original hardware certification | Not inherited | New native-source hardware evidence and release gate closure |

The reference itself labels the downstream networking integrations experimental
and does not validate all drivers, client systems, suspend/resume or long-running
operation. “Complete parity” must not turn those original limits into claims of
support in RelayForge.
