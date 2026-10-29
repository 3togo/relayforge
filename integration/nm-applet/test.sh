#!/bin/bash
# Requires GTK3/Gio/Jansson development libraries and a display; changes no host network.
set -euo pipefail
ulimit -c 0
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
build_dir=$(mktemp -d)
trap 'rm -rf -- "$build_dir"' EXIT
read -r -a dependency_flags < <(pkg-config --cflags --libs gtk+-3.0 gio-2.0 jansson)
compiler_flags=(-Wall -Wextra -Werror -Wno-unused-parameter -g)
opt_level=${OPT_LEVEL:-0}
[[ $opt_level == 0 || $opt_level == 1 || $opt_level == 2 ]] || {
    echo 'OPT_LEVEL must be 0, 1, or 2' >&2
    exit 2
}
compiler_flags+=("-O$opt_level")
if [[ ${SANITIZE:-0} == 1 ]]; then
    compiler_flags+=(-fsanitize=address,undefined -fno-omit-frame-pointer)
fi
"${CC:-cc}" "${compiler_flags[@]}" "$repo_dir/integration/nm-applet/test-menu.c" \
    -o "$build_dir/test-menu" "${dependency_flags[@]}"
# Leak checks are disabled for GTK's process-wide caches; address/UB checks stay enabled.
GIO_USE_VFS=local NO_AT_BRIDGE=1 ASAN_OPTIONS=detect_leaks=0 "$build_dir/test-menu"
