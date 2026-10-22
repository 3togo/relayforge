#!/bin/bash
# Non-networking integration engine: real Rust recovery, only temporary net files.
set -eu
umask 077
root=${RELAYFORGE_TEST_STATE:?}
runtime() { "$root/runtime-bin" --test-ap-runtime "$root" "$@"; }
cleanup_owned() {
    [[ ! -e "$root/fail-cleanup" ]] || return 1
    runtime stop-children "$CONFDIR" || return 1
    runtime check-interfaces "$CONFDIR" || return 1
    runtime firewall-remove "$CONFDIR" || return 1
    runtime forwarding-release "$CONFDIR" || return 1
    runtime interface-remove "$CONFDIR" || return 1
    runtime device-restore "$CONFDIR" || return 1
    runtime verify-cleanup "$CONFDIR" || return 1
    rm -rf -- "$CONFDIR"
    rm -f -- "$DAEMON_PIDFILE" "$DAEMON_PIDFILE.ready"
}
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
if [[ ${1-} == --recover ]]; then
    state=$(runtime load "$2") || exit 1
    eval "$state"
    cleanup_owned
    exit
fi
config= DAEMON_PIDFILE=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) config=$2; shift 2 ;;
        --pidfile) DAEMON_PIDFILE=$2; shift 2 ;;
        --freq-band|-c) shift 2 ;;
        --daemon|--no-haveged) shift ;;
        *) exit 2 ;;
    esac
done
while IFS='=' read -r key value; do
    case "$key" in WIFI_IFACE) WIFI_IFACE=$value ;; esac
done < "$config"
CONFDIR="$root/engine/create_ap.$WIFI_IFACE.conf.ABCDEFGH"
COMMON_CONFDIR="$root/engine/create_ap.common.conf"
mkdir -m 700 -p "$root/engine" "$COMMON_CONFDIR"
mkdir -m 755 "$CONFDIR"
printf '%s\n' "$$" > "$CONFDIR/pid"
chmod 444 "$CONFDIR/pid"
INTERNET_IFACE=$WIFI_IFACE VWIFI_IFACE= BRIDGE_IFACE= OLD_MACADDR= NEW_MACADDR=
NO_VIRT=0 SHARE_METHOD=nat NO_DNS=0 DNS_PORT=5353 GATEWAY=192.168.12.1 RUNNING_AS_DAEMON=1
ADDED_UNMANAGED= RULE_COMMENT="wifi-relay-${CONFDIR##*/}" BRIDGE_CREATED=0 PHYSICAL_CONFIGURED=0
args=()
for name in CONFDIR COMMON_CONFDIR WIFI_IFACE VWIFI_IFACE INTERNET_IFACE BRIDGE_IFACE OLD_MACADDR NEW_MACADDR NO_VIRT SHARE_METHOD NO_DNS DNS_PORT GATEWAY RUNNING_AS_DAEMON DAEMON_PIDFILE ADDED_UNMANAGED RULE_COMMENT BRIDGE_CREATED PHYSICAL_CONFIGURED; do args+=("$name=${!name}"); done
runtime save "$CONFDIR" "$$" "${args[@]}"
runtime radio-check "$CONFDIR"
state=$(runtime interface-create "$CONFDIR")
eval "$state"
runtime device-release "$CONFDIR"
daemon_state=$(runtime daemons-configure "$CONFDIR" 1 2.4 nl80211)
eval "$daemon_state"
runtime interface-configure "$CONFDIR"
runtime forwarding-enable "$CONFDIR"
for phase in nat dns dhcp; do runtime firewall-add "$CONFDIR" "$phase"; done
stop_owned() { cleanup_owned; exit $?; }
trap stop_owned SIGUSR1 SIGTERM
printf '%s\n' "$$" > "$DAEMON_PIDFILE"
runtime daemons-run "$CONFDIR" &
DAEMONS_PID=$!
wait "$DAEMONS_PID"
