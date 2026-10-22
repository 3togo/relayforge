#!/bin/bash
# Only a private temporary-directory simulator. No networking commands.
set -u
utility="${BASH_SOURCE[0]%.sh}.py"
if [[ ${1-} == --recover ]]; then
    exec /usr/bin/python3 -I "$utility" recover "$2"
fi
if [[ ${1-} == --rf-observe ]]; then
    exec /usr/bin/python3 -I "$utility" observe "$2"
fi
config= pidfile=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) config=$2; shift 2 ;;
        --pidfile) pidfile=$2; shift 2 ;;
        --freq-band|-c) shift 2 ;;
        --daemon|--no-haveged) shift ;;
        *) exit 2 ;;
    esac
done
stop_owned() {
    if /usr/bin/python3 -I "$utility" should-stop "$pidfile"; then
        /usr/bin/python3 -I "$utility" stop "$pidfile"
        exit $?
    fi
}
trap stop_owned SIGUSR1 SIGTERM
/usr/bin/python3 -I "$utility" init "$pidfile" "$$" "$config" || exit $?
while :; do
    /usr/bin/python3 -I "$utility" tick "$pidfile" "$$" || exit $?
    sleep 0.03
done
