#!/usr/bin/env python3
"""Stage a native-core payload in a fresh directory; never install or enable it."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat

ROOT=Path(__file__).resolve().parents[1]
PAYLOAD={
    'usr/share/doc/relayforge/LICENSE':ROOT/'LICENSE',
    'usr/share/dbus-1/system.d/io.github.erhanzeyrek.WifiHotspot.conf':ROOT/'vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.conf',
    'usr/share/polkit-1/actions/io.github.erhanzeyrek.WifiHotspot.policy':ROOT/'vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.policy',
}


def stage(binary, output, backend='networkmanager'):
    if backend not in ('networkmanager','create_ap'):
        raise ValueError('Unknown native backend')
    payload=dict(PAYLOAD)
    unit='relayforge-native.service' if backend=='networkmanager' else 'relayforge-create-ap.service'
    activation='io.github.erhanzeyrek.WifiHotspot.service' if backend=='networkmanager' else 'create-ap-dbus.service'
    payload['usr/lib/systemd/system/'+unit]=ROOT/'packaging'/unit
    payload['usr/share/dbus-1/system-services/io.github.erhanzeyrek.WifiHotspot.service']=ROOT/'packaging'/activation
    if backend=='create_ap':
        for name in ('create_ap','legacy_runtime.py'):
            payload['usr/lib/relayforge/vendor/wifi-relay/daemon/'+name]=ROOT/'vendor/wifi-relay/daemon'/name
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
