#!/bin/bash
# Build the Ubuntu 1.36.0 applet with the optional Relay menu. Never installs it.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
source_version=1.36.0-4ubuntu1
version=${1:-1.36.0-4ubuntu1+relayforge1}
all_packages=${2:-}
[[ -z $all_packages || $all_packages == --all ]] || { echo 'Usage: build-deb.sh [version] [--all]' >&2; exit 2; }
dpkg --validate-version "$version"
if [[ -n ${DISPLAY:-} || -n ${WAYLAND_DISPLAY:-} ]]; then
    "$repo_dir/integration/nm-applet/test.sh"
else
    xvfb-run -a "$repo_dir/integration/nm-applet/test.sh"
fi
output_dir=${RELAYFORGE_INTEGRATION_OUTPUT:-"$repo_dir/artifacts/desktop-integrations"}
mkdir -p "$output_dir"
output_dir=$(realpath "$output_dir")
build_dir=$(mktemp -d "$output_dir/nm-applet-build.XXXXXX")
cd "$build_dir"
apt-get source "network-manager-applet=$source_version"
cd network-manager-applet-1.36.0
python3 "$repo_dir/tools/minimize-integrations.py" applet "$source_version" debian/control
python3 - <<'PY'
from pathlib import Path
path = Path('debian/control')
sections = path.read_text().split('\n\n')
for index, section in enumerate(sections):
    if section.startswith('Package: network-manager-applet\n'):
        sections[index] = section.replace('Depends: ', 'Depends: python3, ', 1)
        break
else:
    raise SystemExit('network-manager-applet stanza missing')
path.write_text('\n\n'.join(sections))
PY
patch -p1 < "$repo_dir/integration/nm-applet/relay-menu.patch"
patch -p1 < "$repo_dir/integration/nm-applet/hotspot-icon.patch"
install -m 0644 "$repo_dir/integration/nm-applet/wifi-relay.c" src/wifi-relay.c
install -m 0644 "$repo_dir/integration/nm-applet/wifi-relay.h" src/wifi-relay.h
install -m 0755 "$repo_dir/integration/nm-applet/restart-applet.py" debian/restart-applet.py
install -m 0755 "$repo_dir/integration/nm-applet/postinst" debian/network-manager-applet.postinst
printf '%s\n' 'debian/restart-applet.py usr/libexec/wifi-relay-nm-applet/' >> debian/network-manager-applet.install
python3 - "$version" <<'PY'
from pathlib import Path
import sys
from email.utils import formatdate
path = Path('debian/changelog')
path.write_text('network-manager-applet (' + sys.argv[1] + ') stonking; urgency=medium\n\n'
    '  * Add optional Wi-Fi Relay service controls to the network menu.\n\n'
    ' -- Wi-Fi Relay <3togo@users.noreply.github.com>  ' + formatdate(localtime=True) + '\n\n'
    + path.read_text())
PY
env -u LD_PRELOAD dpkg-buildpackage -b -uc -us -j8 > "$build_dir/build.log" 2>&1 || {
    tail -80 "$build_dir/build.log"
    exit 1
}
packages=(network-manager-applet network-manager-gnome)
[[ $all_packages != --all ]] || packages+=(nm-connection-editor)
for package in "${packages[@]}"; do
    cp ../"${package}_${version}_"*.deb "$output_dir/"
done
cd "$output_dir"
for package in "${packages[@]}"; do
    for deb in "${package}_${version}_"*.deb; do
        sha256sum "$deb" > "$deb.sha256"
        printf '%s\n' "$output_dir/$deb"
    done
done
printf 'Build log: %s/build.log\n' "$build_dir"
