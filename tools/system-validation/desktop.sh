#!/bin/bash
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/$(id -u)
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
export LIBGL_ALWAYS_SOFTWARE=1
export MUTTER_DEBUG_DUMMY_MODE_SPECS=1280x800
export WAYLAND_DISPLAY=wayland-0
export XDG_CURRENT_DESKTOP=GNOME
export XDG_SESSION_TYPE=wayland
probe_dir="$HOME/.local/share/gnome-shell/extensions/relayforge-validation@example.invalid"
mkdir -p "$probe_dir"
cp /home/ubuntu/probe.js "$probe_dir/extension.js"
cat > "$probe_dir/metadata.json" <<'JSON'
{"uuid":"relayforge-validation@example.invalid","name":"Disposable validation observer","description":"Test-only window observer","shell-version":["45","46","47","48","49","50","51"]}
JSON
gsettings set org.gnome.shell enabled-extensions '[]'
gsettings set org.gnome.shell disable-user-extensions false
gnome-shell --headless --wayland --virtual-monitor 1280x800 > /home/ubuntu/results/shell.log 2>&1 &
shell_pid=$!
trap 'kill "$shell_pid" 2>/dev/null || true' EXIT
for attempt in $(seq 1 60); do
    if gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.NameHasOwner org.gnome.Shell | grep -q true; then break; fi
    sleep 1
done
gnome-extensions enable relayforge-validation@example.invalid
gnome-extensions enable wifi-relay@3togo.github.io
sleep 5
gnome-extensions info wifi-relay@3togo.github.io | tee /home/ubuntu/results/extension.txt
grep -q 'State: ACTIVE' /home/ubuntu/results/extension.txt
wait_window() {
    local pid="$1" name="$2"
    for attempt in $(seq 1 60); do
        kill -0 "$pid"
        if python3 - "$pid" <<'PYTEST'
import json, sys
from pathlib import Path
p = Path('/home/ubuntu/results/windows.json')
raise SystemExit(not (p.exists() and any(w['pid'] == int(sys.argv[1]) and w['visible']
                                      for w in json.loads(p.read_text()))))
PYTEST
        then break; fi
        sleep 1
    done
    python3 - "$pid" "$name" <<'PYTEST'
import json, sys
from pathlib import Path
windows = json.loads(Path('/home/ubuntu/results/windows.json').read_text())
assert any(w['pid'] == int(sys.argv[1]) and w['visible'] for w in windows), windows
assert sys.argv[2] in ('settings', 'about')
Path('/home/ubuntu/results/'+sys.argv[2]+'-window.json').write_text(json.dumps(windows, indent=2))
PYTEST
}
wifi-hotspot-settings > /home/ubuntu/results/settings.log 2>&1 &
settings_pid=$!
wait_window "$settings_pid" settings
sleep 4
kill "$settings_pid"
wifi-hotspot-settings --about > /home/ubuntu/results/about.log 2>&1 &
about_pid=$!
wait_window "$about_pid" about
kill "$about_pid"
# GTK3 tray needs X11. A dedicated Xvfb server is wholly inside the guest.
GDK_BACKEND=x11 xvfb-run -a bash -c 'wifi-hotspot-tray > /home/ubuntu/results/tray.log 2>&1 & p=$!; sleep 8; kill -0 "$p"; kill "$p"'
gnome-extensions disable relayforge-validation@example.invalid
gnome-extensions disable wifi-relay@3togo.github.io
