#!/bin/bash
# Non-networking integration engine: real Rust recovery, only temporary net files.
set -eu
umask 077
root=${RELAYFORGE_TEST_STATE:?}
runtime() { "$root/runtime-bin" --test-ap-runtime "$root" "$@"; }
if [[ ${1-} == --rf-observe ]]; then
    for record in "$root/engine"/create_ap.*.conf.*/recovery.json; do
        [[ -f "$record" ]] || continue
        state=$(runtime device-fields "${record%/*}") || exit 1
        eval "$state"
        printf '{"interface":"%s","frequency":2412,"clients":[{"mac":"02:aa:bb:cc:dd:ee","ip":"192.0.2.2","hostname":"Rust client"}]}\n' "$WIFI_IFACE"
        break
    done
    exit
fi
# Fixture selection is explicit and unavailable in the production instance mode.
exec "$root/runtime-bin" --test-ap-instance "$root" "$@"
