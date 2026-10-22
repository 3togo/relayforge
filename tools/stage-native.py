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
    'usr/share/polkit-1/actions/io.github.erhanzeyrek.WifiHotspot.policy':ROOT/'vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.policy',
}


def native_engine(source):
    """Derive the native adapter while preserving the audited vendor snapshot."""
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
        payload['usr/lib/relayforge/native/create_ap']=ROOT/'vendor/wifi-relay/daemon/create_ap'
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
            if target=='usr/lib/relayforge/native/create_ap':
                destination.write_bytes(native_engine(source.read_bytes()))
            else:
                shutil.copyfile(source,destination)
            destination.chmod(0o755 if target.endswith(('/relayforge-service','/create_ap')) else 0o644)
            manifest[target]={'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                              'mode':oct(stat.S_IMODE(destination.stat().st_mode))}
        (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
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
