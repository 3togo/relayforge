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
    rm -rf -- "$root/net/$VWIFI_IFACE"
    runtime verify-cleanup "$CONFDIR" || return 1
    rm -rf -- "$CONFDIR"
    rm -f -- "$DAEMON_PIDFILE" "$DAEMON_PIDFILE.ready"
}
if [[ ${1-} == --rf-observe ]]; then
    printf '%s\n' '{"interface":"apfake0","frequency":2412,"clients":[{"mac":"02:aa:bb:cc:dd:ee","ip":"192.0.2.2","hostname":"Rust client"}]}'
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
INTERNET_IFACE=$WIFI_IFACE VWIFI_IFACE=apfake0 BRIDGE_IFACE= OLD_MACADDR= NEW_MACADDR=02:77:88:99:aa:bb
NO_VIRT=0 SHARE_METHOD=nat NO_DNS=0 DNS_PORT=5353 GATEWAY=192.168.12.1 RUNNING_AS_DAEMON=1
ADDED_UNMANAGED= RULE_COMMENT="wifi-relay-${CONFDIR##*/}" BRIDGE_CREATED=0 PHYSICAL_CONFIGURED=0
mkdir -p "$root/net/apfake0"
printf '101\n' > "$root/net/apfake0/ifindex"
printf '%s\n' "$NEW_MACADDR" > "$root/net/apfake0/address"
WIFI_IFACE=$VWIFI_IFACE
args=()
for name in CONFDIR COMMON_CONFDIR WIFI_IFACE VWIFI_IFACE INTERNET_IFACE BRIDGE_IFACE OLD_MACADDR NEW_MACADDR NO_VIRT SHARE_METHOD NO_DNS DNS_PORT GATEWAY RUNNING_AS_DAEMON DAEMON_PIDFILE ADDED_UNMANAGED RULE_COMMENT BRIDGE_CREATED PHYSICAL_CONFIGURED; do args+=("$name=${!name}"); done
runtime save "$CONFDIR" "$$" "${args[@]}"
for phase in nat dns dhcp; do runtime firewall-add "$CONFDIR" "$phase"; done
printf 'fixture\n' > "$CONFDIR/hostapd.conf"
"$root/bin/hostapd" "$CONFDIR/hostapd.conf" &
printf '%s\n' "$!" > "$CONFDIR/hostapd.pid"
for ((i=0; i<100; i++)); do [[ -e "$CONFDIR/hostapd.conf.ready" ]] && break; sleep .01; done
runtime record-child "$CONFDIR/hostapd.pid" hostapd
stop_owned() { cleanup_owned; exit $?; }
trap stop_owned SIGUSR1 SIGTERM
printf '%s\n' "$$" > "$DAEMON_PIDFILE"
printf '%s\n' "$$" > "$DAEMON_PIDFILE.ready"
while :; do sleep .05; done
