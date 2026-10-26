#!/bin/bash
# Build the Ubuntu GNOME Settings Wi-Fi tab correction without installing it.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
source_version=1:51.0-1ubuntu1
version=${1:-1:51.0-1ubuntu1+relayforge1}
all_packages=${2:-}
[[ -z $all_packages || $all_packages == --all ]] || { echo 'Usage: build-deb.sh [version] [--all]' >&2; exit 2; }
dpkg --validate-version "$version"
output_dir=${RELAYFORGE_INTEGRATION_OUTPUT:-"$repo_dir/artifacts/desktop-integrations"}
mkdir -p "$output_dir"
output_dir=$(realpath "$output_dir")
build_dir=$(mktemp -d "$output_dir/gnome-settings-build.XXXXXX")
cd "$build_dir"
apt-get source "gnome-control-center=$source_version"
cd gnome-control-center-51.0
python3 "$repo_dir/tools/minimize-integrations.py" settings "$source_version" debian/control
patch -p1 < "$repo_dir/integration/gnome-settings/stable-wifi-device-tabs.patch"
patch -p1 < "$repo_dir/integration/gnome-settings/relay-entrypoint.patch"
# Advertise the bridge only in the package containing the patched panel.
# dh_install copies this single file without changing the stock data package.
printf '1\n' > debian/wifi-relay-entrypoint
printf 'debian/wifi-relay-entrypoint usr/share/gnome-control-center\n' >> debian/gnome-control-center.install
python3 - "$version" <<'PY'
from pathlib import Path
from email.utils import formatdate
import sys
path = Path('debian/changelog')
path.write_text('gnome-control-center (' + sys.argv[1] + ') stonking; urgency=medium\n\n'
    '  * Use stable Wi-Fi device paths and add an optional Relay settings entry.\n\n'
    ' -- Wi-Fi Relay <3togo@users.noreply.github.com>  ' + formatdate(localtime=True)
    + '\n\n' + path.read_text())
PY
env -u LD_PRELOAD dpkg-buildpackage -b -uc -us -j8 > "$build_dir/build.log" 2>&1 || {
    tail -80 "$build_dir/build.log"
    exit 1
}
artifacts=(../gnome-control-center_"${version#*:}"_*.deb)
[[ $all_packages != --all ]] || artifacts=(../*.deb)
for package in "${artifacts[@]}"; do
    cp "$package" "$output_dir/"
done
printf 'Packages: %s\nBuild log: %s/build.log\n' "$output_dir" "$build_dir"
