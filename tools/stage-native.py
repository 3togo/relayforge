#!/usr/bin/env python3
"""Stage a native-core payload in a fresh directory; never install or enable it."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import stat

ROOT=Path(__file__).resolve().parents[1]
PAYLOAD={
    'usr/share/doc/relayforge/LICENSE':ROOT/'LICENSE',
    'usr/share/dbus-1/system.d/io.github.erhanzeyrek.WifiHotspot.conf':ROOT/'vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.conf',
    'usr/share/polkit-1/actions/io.github.erhanzeyrek.WifiHotspot.policy':ROOT/'packaging/io.github.erhanzeyrek.WifiHotspot.policy',
}


def native_engine(source):
    """Historical Bash protocol/parity reference; never included in new payloads."""
    expected=json.loads((ROOT/'vendor/manifest.json').read_text())['files']['daemon/create_ap']
    if hashlib.sha256(source).hexdigest()!=expected:
        raise ValueError('Native adapter source differs from the pinned vendor manifest')
    text=source.decode('utf-8')
    old='LEGACY_HELPER="$(dirname "$(readlink -f "$0")")/legacy_runtime.py"'
    call='python3 -I "$LEGACY_HELPER"'
    if text.count(old)!=1 or text.count(call)!=6:
        raise ValueError('Pinned create_ap helper protocol changed; review the native adapter')
    text=text.replace(old,'RUNTIME_HELPER="$(dirname "$(readlink -f "$0")")/../relayforge-service"')
    text=text.replace(call,'"$RUNTIME_HELPER" --create-ap-runtime')
    if 'python3' in text or 'LEGACY_HELPER' in text:
        raise ValueError('Native create_ap adapter still references Python recovery')
    lines=text.splitlines(keepends=True)
    result=[]
    counts={'nat':0,'dns':0,'dhcp':0,'delete':0}
    index=0
    while index<len(lines):
        line=lines[index]
        match=re.match(r'^(\s*)iptables ',line)
        if not match:
            result.append(line)
            index+=1
            continue
        command=line
        while command.rstrip().endswith(chr(92)):
            index+=1
            command+=lines[index]
        indent=match.group(1)
        if ' -D ' in command:
            counts['delete']+=1
            result.append(indent+': # Exact owned rules are removed by Rust above.\n')
        else:
            if 'POSTROUTING' in command or 'FORWARD' in command:
                phase='nat'
            elif '--dport 67' in command:
                phase='dhcp'
            else:
                phase='dns'
            counts[phase]+=1
            if counts[phase]==1:
                if phase=='dhcp':
                    result.append(indent+'save_recovery_state || die "Could not record DHCP resources."\n')
                result.append(indent+'"$RUNTIME_HELPER" --create-ap-runtime firewall-add "$CONFDIR" '+phase+' || die\n')
            else:
                result.append(indent+': # Rust applies this complete firewall phase.\n')
        index+=1
    if counts!={'nat':3,'dns':4,'dhcp':1,'delete':8}:
        raise ValueError('Pinned firewall command layout changed; review native extraction')
    text=''.join(result)
    anchor='    local found=0\n    for x in $(list_running_conf); do\n'
    if text.count(anchor)!=1:
        raise ValueError('Pinned firewall cleanup boundary changed')
    text=text.replace(anchor,'    "$RUNTIME_HELPER" --create-ap-runtime firewall-remove "$CONFDIR" || failed=1\n\n'+anchor)
    if re.search(r'^\s*iptables ',text,re.MULTILINE):
        raise ValueError('Native adapter still executes Bash firewall commands')
    def section(start,end,replacement):
        nonlocal text
        if text.count(start)!=1 or end not in text[text.index(start):]:
            raise ValueError('Pinned network transaction boundary changed')
        first=text.index(start)
        last=text.index(end,first)
        text=text[:first]+replacement+text[last:]

    section('    local found=0\n    for x in $(list_running_conf); do\n',
            '    if [[ "$SHARE_METHOD" != "none" ]]; then\n',
            '    "$RUNTIME_HELPER" --create-ap-runtime forwarding-release "$CONFDIR" || failed=1\n'
            '    if ! has_running_instance; then\n'
            '        stop_recorded_children "$COMMON_CONFDIR" || failed=1\n'
            '        [[ $failed -eq 0 ]] && rm -rf "$COMMON_CONFDIR"\n'
            '    fi\n\n')
    section('    if [[ $NO_VIRT -eq 0 ]]; then\n        if [[ -n "$VWIFI_IFACE" ]]; then\n',
            '    for x in $ADDED_UNMANAGED; do\n',
            '    if [[ $NO_VIRT -eq 0 && -n "$VWIFI_IFACE" ]]; then\n'
            '        networkmanager_rm_unmanaged_if_needed "$VWIFI_IFACE" || failed=1\n'
            '    fi\n'
            '    if "$RUNTIME_HELPER" --create-ap-runtime interface-remove "$CONFDIR"; then\n'
            '        [[ $NO_VIRT -eq 0 && -n "$VWIFI_IFACE" ]] && dealloc_iface "$VWIFI_IFACE"\n'
            '    else\n        failed=1\n    fi\n'
            '    if [[ $NO_VIRT -eq 1 && $PHYSICAL_CONFIGURED -eq 1 ]]; then\n'
            '        networkmanager_rm_unmanaged_if_needed "$WIFI_IFACE" || failed=1\n'
            '    fi\n')
    section('if [[ "$SHARE_METHOD" == "nat" ]]; then\n    echo $INTERNET_IFACE > $CONFDIR/nat_internet_iface\n',
            'mutex_unlock\n\nsave_recovery_state',
            'if [[ "$SHARE_METHOD" == "nat" ]]; then\n'
            '    echo "$INTERNET_IFACE" > "$CONFDIR/nat_internet_iface"\nfi\n')
    section('# initialize WiFi interface\n','# enable Internet sharing\n',
            '# Rust journals the baseline before configuring the recorded AP.\n'
            'if [[ $NO_VIRT -eq 1 ]]; then PHYSICAL_CONFIGURED=1; fi\n'
            'save_recovery_state || die "Could not record interface configuration."\n'
            '"$RUNTIME_HELPER" --create-ap-runtime interface-configure "$CONFDIR" || die\n\n')
    writes=('        echo 1 > /proc/sys/net/ipv4/conf/$INTERNET_IFACE/forwarding || die\n'
            '        echo 1 > /proc/sys/net/ipv4/ip_forward || die\n')
    if text.count(writes)!=1:
        raise ValueError('Pinned forwarding writes changed')
    text=text.replace(writes,'        "$RUNTIME_HELPER" --create-ap-runtime forwarding-enable "$CONFDIR" || die\n')
    # Native mode declines bridge sharing before its historical code is reachable.
    section('        # disable iptables rules for bridged interfaces\n',
            '        # to initialize the bridge interface correctly',
            '        die "Bridge sharing is unavailable in native create_ap mode."\n\n')
    power='if [[ $USE_IWCONFIG -eq 0 ]]; then\n    iw dev ${WIFI_IFACE} set power_save off\nfi\n'
    if text.count(power)!=1:
        raise ValueError('Pinned parent power-save boundary changed')
    text=text.replace(power,'# Native setup preserves the parent station power-save setting.\n')
    if '/proc/sys/net/' in text:
        raise ValueError('Native adapter still accesses Bash sysctls')
    # All allocation and unmanaged-device decisions belong to Rust in native mode.
    section('alloc_new_iface() {\n','get_all_macaddrs() {\n','')
    section('get_new_macaddr() {\n','# start haveged when needed\n','')
    section('NETWORKMANAGER_CONF=/etc/NetworkManager/NetworkManager.conf\n','wait_for_ap() {\n','')
    section('if [[ $FIX_UNMANAGED -eq 1 ]]; then\n','if [[ $DAEMONIZE -eq 1 && $RUNNING_AS_DAEMON -eq 0 ]]; then\n',
            'if [[ $FIX_UNMANAGED -eq 1 ]]; then\n'
            '    die "Global NetworkManager repair is unavailable in native mode."\nfi\n\n')
    section('if [[ "$SHARE_METHOD" == "bridge" ]]; then\n    if is_bridge_interface $INTERNET_IFACE; then\n',
            '# Native setup preserves the parent station power-save setting.\n',
            'if [[ "$SHARE_METHOD" == "bridge" ]]; then die "Native bridge mode is unavailable."; fi\n\n')
    # Preserve the read-only upstream channel synchronization before creating a child.
    creation=text.index('if [[ $NO_VIRT -eq 0 ]]; then\n    VWIFI_IFACE=$(alloc_new_iface ap)\n')
    begin=text.index('    if is_wifi_connected ${WIFI_IFACE}; then\n',creation)
    end=text.index('\n\n    VIRTDIEMSG=',begin)
    channel=text[begin:end]
    section('if [[ $NO_VIRT -eq 0 ]]; then\n    VWIFI_IFACE=$(alloc_new_iface ap)\n',
            'save_recovery_state || die "Could not record hotspot interface."\n',
            'if [[ $NO_VIRT -eq 0 ]]; then\n'+channel+'\nfi\n'
            'device_state=$("$RUNTIME_HELPER" --create-ap-runtime interface-create "$CONFDIR") || die\n'
            'eval "$device_state"\n\n')
    section('if networkmanager_exists && ! networkmanager_iface_is_unmanaged ${WIFI_IFACE}; then\n',
            '[[ $HIDDEN -eq 1 ]]',
            '"$RUNTIME_HELPER" --create-ap-runtime device-release "$CONFDIR" || die\n\n')
    section('    if [[ $NO_VIRT -eq 0 && -n "$VWIFI_IFACE" ]]; then\n',
            '    mutex_unlock\n    cleanup_lock\n',
            '    "$RUNTIME_HELPER" --create-ap-runtime interface-remove "$CONFDIR" || failed=1\n'
            '    "$RUNTIME_HELPER" --create-ap-runtime device-restore "$CONFDIR" || failed=1\n\n')
    stop='    stop_recorded_children "$CONFDIR" || failed=1\n'
    if text.count(stop)!=1:
        raise ValueError('Pinned cleanup owner boundary changed')
    text=text.replace(stop,stop+
        '    local device_state\n'
        '    device_state=$("$RUNTIME_HELPER" --create-ap-runtime device-fields "$CONFDIR") || { mutex_unlock; cleanup_lock; return 1; }\n'
        '    eval "$device_state"\n')
    section('        elif [[ "$SHARE_METHOD" == "bridge" ]]; then\n',
            '    if [[ "$SHARE_METHOD" != "bridge" ]]; then\n',
            '        elif [[ "$SHARE_METHOD" == "bridge" ]]; then\n'
            '            failed=1 # Native mode never owns bridge resources.\n'
            '        fi\n    fi\n\n')
    section('\n    elif [[ "$SHARE_METHOD" == "bridge" ]]; then\n',
            '    fi\nelse\n    echo "No Internet sharing"\n',
            '\n    elif [[ "$SHARE_METHOD" == "bridge" ]]; then\n'
            '        die "Native bridge mode is unavailable."\n')
    section('    if networkmanager_is_running; then\n',
            '    die\nfi\n\nclean_exit\n','')
    if any(value in text for value in ('networkmanager_', 'alloc_new_iface', 'dealloc_iface', 'interface add', 'NetworkManager.conf')):
        raise ValueError('Native adapter retains Bash device ownership operations')
    section('# hostapd config\n','# Rust journals the baseline before configuring the recorded AP.\n',
            'daemon_state=$("$RUNTIME_HELPER" --create-ap-runtime daemons-configure "$CONFDIR" "$CHANNEL" "$FREQ_BAND" "$DRIVER") || die\n'
            'eval "$daemon_state"\n\n')
    launch=('      umask 0033\n'
            '      dnsmasq -C $CONFDIR/dnsmasq.conf -x $CONFDIR/dnsmasq.pid -l $CONFDIR/dnsmasq.leases -p $DNS_PORT || die\n'
            '      umask $SCRIPT_UMASK\n'
            '      record_child "$CONFDIR/dnsmasq.pid" dnsmasq || die "Could not record dnsmasq identity."\n')
    if text.count(launch)!=1:
        raise ValueError('Pinned DNS launch boundary changed')
    text=text.replace(launch,'      : # Rust launches and supervises the foreground DNS daemon below.\n')
    section('# start access point\n','\nclean_exit\n',
            '# Rust owns both daemon children and the readiness acknowledgement.\n'
            '[[ $NO_HAVEGED -eq 1 ]] || die "Native mode requires --no-haveged."\n'
            '"$RUNTIME_HELPER" --create-ap-runtime daemons-run "$CONFDIR" &\n'
            'DAEMONS_PID=$!\n'
            'wait "$DAEMONS_PID" || die "Native daemon supervision failed."\n')
    section('# start haveged when needed\n','CHANNEL=default\n','')
    if any(value in text for value in ('cat << EOF > $CONFDIR/hostapd.conf', 'cat << EOF > $CONFDIR/dnsmasq.conf', 'dnsmasq -C ', '$HOSTAPD $HOSTAPD_DEBUG_ARGS', 'wait_for_ap()')):
        raise ValueError('Native adapter retains Bash daemon configuration or launch')
    # The frozen plan already comes from Rust preflight. Recheck it before any
    # interface mutation; never let legacy radio heuristics rewrite that plan.
    section('if [[ $FREQ_BAND_SET != 0 ]]; then\n',
            'if [[ "$SHARE_METHOD" != "nat" && "$SHARE_METHOD" != "bridge" && "$SHARE_METHOD" != "none" ]]; then\n',
            'WIFI_IFACE=$1\n'
            '[[ "$DRIVER" == nl80211 && -z "$COUNTRY" && -z "$NEW_MACADDR" ]] || die "Unsupported native radio override."\n\n')
    section('if [[ -n "$NEW_MACADDR" ]]; then\n',
            'if [[ "$SHARE_METHOD" != "none" ]]; then\n    MIN_REQUIRED_ARGS=2\n','')
    section('if [[ $(get_adapter_kernel_module ${WIFI_IFACE}) =~ ^rtl[0-9].*$ ]]; then\n',
            'if [[ $NO_VIRT -eq 1 && "$WIFI_IFACE" == "$INTERNET_IFACE" ]]; then\n','')
    section('if [[ $NO_VIRT -eq 0 ]]; then\n    if is_wifi_connected ${WIFI_IFACE}; then\n',
            'device_state=$("$RUNTIME_HELPER" --create-ap-runtime interface-create "$CONFDIR") || die\n',
            '"$RUNTIME_HELPER" --create-ap-runtime radio-check "$CONFDIR" || die\n')
    section('if [[ -n "$COUNTRY" && $USE_IWCONFIG -eq 0 ]]; then\n',
            '"$RUNTIME_HELPER" --create-ap-runtime device-release "$CONFDIR" || die\n','')
    section('version_cmp() {\n','USE_IWCONFIG=0\n','')
    section('is_wifi_interface() {\n','is_macaddr() {\n','')
    text=text.replace("#    iwconfig (you only need this if 'iw' can not recognize your adapter)\n",'')
    section('is_macaddr() {\n','CHANNEL=default\n','')
    # Optional PPTP helper loading changes global kernel state and is not part
    # of the supported desktop NAT contract. Kernel dependencies remain managed
    # by the administrator and normal kernel autoloading.
    module='        modprobe nf_nat_pptp > /dev/null 2>&1\n'
    if text.count(module)!=1:
        raise ValueError('Pinned optional module boundary changed')
    text=text.replace('        # to enable clients to establish PPTP connections we must\n'
                      '        # load nf_nat_pptp module\n'+module,'')
    if any(value in text for value in ('can_transmit_to_channel', 'is_wifi_connected', 'get_adapter_', 'iw reg ', 'iw phy ', 'iwconfig ', 'modprobe ')):
        raise ValueError('Native adapter retains Bash radio policy or module loading')
    return text.encode('utf-8')


def stage(binary, output, backend='networkmanager'):
    if backend not in ('networkmanager','create_ap'):
        raise ValueError('Unknown native backend')
    payload=dict(PAYLOAD)
    unit='relayforge-native.service' if backend=='networkmanager' else 'relayforge-create-ap.service'
    activation='io.github.erhanzeyrek.WifiHotspot.service' if backend=='networkmanager' else 'create-ap-dbus.service'
    payload['usr/lib/systemd/system/'+unit]=ROOT/'packaging'/unit
    payload['usr/share/dbus-1/system-services/io.github.erhanzeyrek.WifiHotspot.service']=ROOT/'packaging'/activation
    if backend=='create_ap':
        payload['usr/lib/relayforge/native/create_ap']=Path(binary)
    binary=Path(binary)
    output=Path(output)
    info=binary.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Supply a regular ELF release executable')
    with binary.open('rb') as handle:
        magic=handle.read(4)
    if magic!=b'\x7fELF':
        raise ValueError('Supply a regular ELF release executable')
    # mkdir without exist_ok prevents overlaying an existing tree, even a symlink.
    output.mkdir(mode=0o700)
    manifest={}
    try:
        for target,source in {'usr/lib/relayforge/relayforge-service':binary,**payload}.items():
            destination=output/target
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,destination)
            destination.chmod(0o755 if target.endswith(('/relayforge-service','/create_ap')) else 0o644)
            manifest[target]={'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                              'mode':oct(stat.S_IMODE(destination.stat().st_mode))}
        for directory in output.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o755)
        (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        (output/'manifest.json').chmod(0o644)
        return manifest
    except BaseException:
        # Only this newly created staging directory belongs to this operation.
        shutil.rmtree(output)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--backend',choices=('networkmanager','create_ap'),default='networkmanager')
    args=parser.parse_args()
    stage(args.binary,args.output,args.backend)
    print('Staged native core; no configuration, services or desktop controls were changed.')


if __name__=='__main__':
    main()
