#!/usr/bin/env bash
# Build the Debian package; forward all options to the Python builder.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${script_dir}/build-deb.py" "$@"
