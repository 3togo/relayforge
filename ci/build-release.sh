#!/bin/bash
set -euo pipefail
umask 022
root=$(pwd)
logs="$root/artifacts/release-logs"
mkdir -p "$logs" artifacts/release/{standard,desktop-integrated} artifacts/upgrade-baseline artifacts/baseline-source
# Reproduce the actual previously pushed version, not a relabelled new binary.
baseline=3f83d5b9db86d4a2658a120b0bad87169dd64e1c
git archive "$baseline" | tar -x -C artifacts/baseline-source
export CARGO_TARGET_DIR="$root/target"
(
  cd artifacts/baseline-source
  cargo build --release --locked -p relayforge-service
  cp "$CARGO_TARGET_DIR/release/relayforge-service" "$root/artifacts/baseline-service"
  python3 tools/build-deb.py --binary "$root/artifacts/baseline-service" --backend create_ap --output-dir "$root/artifacts/upgrade-baseline"
) > "$logs/baseline-build.log" 2>&1
cargo build --release --locked -p relayforge-service > "$logs/release-build.log" 2>&1
cargo test --workspace --locked > "$logs/rust-tests.log" 2>&1
python3 tools/check-vendor.py > "$logs/vendor-check.log" 2>&1
# Crash tests deliberately orphan helpers. The Actions container's PID 1 does
# not reap them; use a subreaper so dead helpers cannot remain as zombies.
RELAYFORGE_BINARY="$CARGO_TARGET_DIR/release/relayforge-service" tini -s -- xvfb-run -a dbus-run-session -- python3 -m unittest discover -s tests -v > "$logs/python-tests.log" 2>&1
xvfb-run -a bash integration/nm-applet/test.sh > "$logs/menu-tests.log" 2>&1
SANITIZE=1 xvfb-run -a bash integration/nm-applet/test.sh > "$logs/menu-sanitizer-tests.log" 2>&1
python3 tools/build-deb.py --binary "$CARGO_TARGET_DIR/release/relayforge-service" --backend create_ap --output-dir artifacts/release/standard > "$logs/standard-package.log" 2>&1
python3 tools/build-deb.py --binary "$CARGO_TARGET_DIR/release/relayforge-service" --backend create_ap --desktop-integrations both --output-dir artifacts/desktop-build > "$logs/integrated-packages.log" 2>&1
cp artifacts/desktop-build/*.deb artifacts/release/desktop-integrated/
cp docs/releases/0.1.1.md artifacts/release/RELEASE_NOTES.md
python3 - <<'PY'
from pathlib import Path
import hashlib, subprocess
root = Path('artifacts/release')
(root/'SOURCE_REVISION').write_text(subprocess.check_output(['git','rev-parse','HEAD'],text=True))
files = sorted(p for p in root.rglob('*') if p.is_file() and p.name != 'SHA256SUMS')
(root/'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root)}\n' for p in files))
PY
(cd artifacts/release && sha256sum -c SHA256SUMS)
