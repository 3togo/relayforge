#!/bin/bash
# Run only in the disposable workflow container; never against a workstation.
set -euo pipefail
[[ -f /.dockerenv ]] || { echo 'This check requires a disposable container.' >&2; exit 1; }
variant=${1:?standard or desktop-integrated}
[[ $variant == standard || $variant == desktop-integrated ]]
export DEBIAN_FRONTEND=noninteractive
umask 022
root=$(pwd)
release="$root/artifacts/release"
(cd "$release" && sha256sum -c SHA256SUMS)
# Container PID 1 is not systemd. Suppress dependency service startup explicitly.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
apt-get update
apt-get install -y --no-install-recommends python3 python3-gi dbus-daemon
packages=("$release/$variant/"*.deb)
remove=(relayforge)
if [[ $variant == desktop-integrated ]]; then
    remove+=(network-manager-applet network-manager-gnome gnome-control-center)
fi
[[ ! -e /usr/lib/relayforge/relayforge-service ]]
[[ ! -e /etc/wifi-hotspot.conf ]]
apt-get install -y --no-install-recommends "${packages[@]}"
[[ $(dpkg-query -W -f='${Version}' relayforge) == 0.1.1 ]]
[[ ! -e /etc/wifi-hotspot.conf ]]
[[ ! -L /etc/systemd/system/multi-user.target.wants/relayforge-create-ap.service ]]
python3 ci/check-installed-package.py "$variant"
echo 'PASS: clean install and installed service protocol'
apt-get remove -y "${remove[@]}"
[[ ! -e /usr/lib/relayforge/relayforge-service ]]
[[ ! -e /usr/bin/wifi-hotspot-settings ]]
[[ -f /etc/xdg/autostart/relayforge-tray.desktop ]]
apt-get purge -y "${remove[@]}"
[[ ! -e /etc/xdg/autostart/relayforge-tray.desktop ]]
if [[ $variant == desktop-integrated ]]; then
    [[ ! -e /usr/share/gnome-control-center/wifi-relay-entrypoint ]]
fi
echo 'PASS: clean removal and purge'
# Install the genuine previous revision, then verify an upgrade preserves local data.
apt-get install -y --no-install-recommends "$root/artifacts/upgrade-baseline/"*.deb
[[ $(dpkg-query -W -f='${Version}' relayforge) == 0.1.0 ]]
printf 'SSID=release-lifecycle-test\n' > /etc/wifi-hotspot.conf
chmod 600 /etc/wifi-hotspot.conf
printf '\n# Local administrator customization\n' >> /etc/xdg/autostart/relayforge-tray.desktop
before_config=$(sha256sum /etc/wifi-hotspot.conf)
before_autostart=$(sha256sum /etc/xdg/autostart/relayforge-tray.desktop)
apt-get -o Dpkg::Options::=--force-confold install -y --no-install-recommends "${packages[@]}"
[[ $(dpkg-query -W -f='${Version}' relayforge) == 0.1.1 ]]
[[ $(sha256sum /etc/wifi-hotspot.conf) == "$before_config" ]]
[[ $(stat -c %a /etc/wifi-hotspot.conf) == 600 ]]
[[ $(sha256sum /etc/xdg/autostart/relayforge-tray.desktop) == "$before_autostart" ]]
python3 ci/check-installed-package.py "$variant"
echo 'PASS: upgrade from 0.1.0, configuration and conffile preservation'
apt-get remove -y "${remove[@]}"
[[ ! -e /usr/lib/relayforge/relayforge-service ]]
[[ $(sha256sum /etc/wifi-hotspot.conf) == "$before_config" ]]
[[ $(sha256sum /etc/xdg/autostart/relayforge-tray.desktop) == "$before_autostart" ]]
apt-get purge -y "${remove[@]}"
[[ ! -e /etc/xdg/autostart/relayforge-tray.desktop ]]
[[ ! -e /usr/lib/relayforge/settings/main.py ]]
# User-created credentials/configuration are deliberately not owned by dpkg.
[[ $(sha256sum /etc/wifi-hotspot.conf) == "$before_config" ]]
if [[ $variant == desktop-integrated ]]; then
    [[ ! -e /usr/share/gnome-control-center/wifi-relay-entrypoint ]]
fi
[[ -z $(dpkg --audit) ]]
echo 'PASS: upgraded-package removal and purge; user configuration retained'
