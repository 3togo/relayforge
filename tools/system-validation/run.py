#!/usr/bin/env python3
"""Run real OS qualification in a disposable KVM overlay, never on the host.

Input: verified Ubuntu cloud image and packages/{create_ap-1,create_ap-2,
networkmanager-2} directories, each containing base/tray/GNOME packages.
The only forwarded port binds loopback and authenticates with a temporary key.
No host devices, buses, filesystems or graphical session are passed to the VM.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import time

HERE = Path(__file__).resolve().parent


def run(args, **kw):
    return subprocess.run(args, check=True, **kw)


def main():
    if os.geteuid() == 0:
        raise RuntimeError('Run the VM as an ordinary user')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image', type=Path, required=True)
    p.add_argument('--packages', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    image = a.image.resolve(strict=True)
    packages = a.packages.resolve(strict=True)
    out = a.output.resolve()
    out.mkdir()  # Never overwrite an existing run or VM.
    for series in ('create_ap-1', 'create_ap-2', 'networkmanager-2'):
        assert len(list((packages/series).glob('*.deb'))) == 3
    harness = out/'harness'; harness.mkdir()
    for name in ('run.py', 'guest.py', 'desktop.sh', 'probe.js'):
        shutil.copyfile(HERE/name, harness/name)
    receipt = {'harness': {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in harness.iterdir()},
               'image_sha256': hashlib.file_digest(image.open('rb'), 'sha256').hexdigest(),
               'packages': {str(f.relative_to(packages)): hashlib.sha256(f.read_bytes()).hexdigest()
                            for f in packages.rglob('*.deb')}}
    chain = json.loads(subprocess.check_output(['qemu-img', 'info', '--backing-chain', '--output=json', str(image)]))
    receipt['image_chain'] = [{'filename': n['filename'], 'sha256': hashlib.file_digest(Path(n['filename']).open('rb'), 'sha256').hexdigest()} for n in chain]
    (out/'inputs.json').write_text(json.dumps(receipt, indent=2)+'\n')
    run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(out/'key')])
    (out/'user-data').write_text('''#cloud-config
ssh_pwauth: false
write_files:
  - path: /run/relayforge-validation-guest
    content: disposable-task18
users:
  - name: ubuntu
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - '''+(out/'key.pub').read_text().strip()+'\n')
    (out/'meta-data').write_text('instance-id: relayforge-task18-'+hashlib.sha256((out/'key.pub').read_bytes()).hexdigest()[:16]+'\n')
    run(['genisoimage', '-quiet', '-output', str(out/'seed.iso'), '-volid', 'cidata',
         '-joliet', '-rock', str(out/'user-data'), str(out/'meta-data')])
    run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(image), str(out/'guest.qcow2')])
    run(['qemu-img', 'resize', str(out/'guest.qcow2'), '24G'])
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    ssh = ['ssh', '-i', str(out/'key'), '-p', str(port), '-o', 'BatchMode=yes',
           '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile='+str(out/'known_hosts'),
           '-o', 'ConnectTimeout=3', 'ubuntu@127.0.0.1']
    scp = ['scp', '-q', '-i', str(out/'key'), '-P', str(port), '-o', 'BatchMode=yes',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile='+str(out/'known_hosts')]
    qemu = ['qemu-system-x86_64', '-enable-kvm', '-cpu', 'host', '-smp', '4', '-m', '6144',
            '-display', 'none', '-monitor', 'none', '-serial', 'file:'+str(out/'serial.log'),
            '-drive', 'file='+str(out/'guest.qcow2')+',if=virtio,format=qcow2',
            '-drive', 'file='+str(out/'seed.iso')+',media=cdrom,readonly=on',
            '-nic', f'user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{port}-:22',
            '-device', 'virtio-vga']
    (out/'qemu-command.json').write_text(json.dumps(qemu, indent=2)+'\n')
    with (out/'qemu.log').open('w') as log:
        vm = subprocess.Popen(qemu, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic()+180
            while time.monotonic() < deadline:
                assert vm.poll() is None, 'VM exited; inspect qemu.log'
                ready = subprocess.run(ssh+['test -f /run/relayforge-validation-guest'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if ready.returncode == 0: break
                time.sleep(2)
            else: raise RuntimeError('Guest SSH readiness timeout')
            run(scp+['-r', str(packages), 'ubuntu@127.0.0.1:packages'])
            run(scp+[str(harness/'guest.py'), str(harness/'desktop.sh'), str(harness/'probe.js'), 'ubuntu@127.0.0.1:'])
            with (out/'guest.log').open('w') as guestlog:
                result = subprocess.run(ssh+['sudo python3 /home/ubuntu/guest.py'],
                                        stdout=guestlog, stderr=subprocess.STDOUT, timeout=1800)
            run(scp+['-r', 'ubuntu@127.0.0.1:results', str(out)])
            if result.returncode: raise RuntimeError('Guest qualification failed; inspect guest.log')
            assert json.loads((out/'results/report.json').read_text())['complete']
        finally:
            subprocess.run(ssh+['sudo poweroff'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try: vm.wait(timeout=30)
            except subprocess.TimeoutExpired:
                vm.terminate(); vm.wait(timeout=10)
    print(out/'results/report.json')


if __name__ == '__main__':
    main()
