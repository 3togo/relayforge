"""Rust offline deployment in disposable images. No root, services or real networking."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_dbus import BINARY
from test_staging import staging

SERVICE='usr/lib/relayforge/relayforge-service'
HELPER='usr/lib/relayforge/native/create_ap'
NM='usr/lib/systemd/system/relayforge-native.service'
AP='usr/lib/systemd/system/relayforge-create-ap.service'
ACT='usr/share/dbus-1/system-services/io.github.erhanzeyrek.WifiHotspot.service'
POLICY='usr/share/dbus-1/system.d/io.github.erhanzeyrek.WifiHotspot.conf'
POLKIT='usr/share/polkit-1/actions/io.github.erhanzeyrek.WifiHotspot.policy'
LICENSE='usr/share/doc/relayforge/LICENSE'
PATHS=[SERVICE,HELPER,NM,AP,POLICY,POLKIT,LICENSE,ACT]
STATE='var/lib/relayforge-deploy'
SECRET='offline-literal-secret-$()-do-not-print'

class Image:
    def __init__(self,path,backend):
        self.path=Path(path);self.root=self.path/'image';self.root.mkdir(mode=0o700)
        self.backend=backend
        (self.root/'etc').mkdir()
        self.config=self.root/'etc/wifi-hotspot.conf'
        self.config.write_text(f'# original literal bytes\nBACKEND={backend}\nWIFI_IFACE=demo0\nINTERNET_IFACE=demo0\nSSID=Desktop retained\nPASSPHRASE={SECRET}\n')
        self.config.chmod(0o600);self.original_config=self.config.read_bytes()
        self.payload=self.stage('v1')
        self.desktop=self.root/'usr/share/applications/existing-relay.desktop'
        self.desktop.parent.mkdir(parents=True);self.desktop.write_text('desktop controls unchanged\n')
        for directory in self.root.rglob('*'):
            if directory.is_dir():directory.chmod(0o755)
    def stage(self,name):
        binary=self.path/(name+'-binary');shutil.copyfile(BINARY,binary);binary.chmod(0o755)
        if name!='v1':
            with binary.open('ab') as handle:handle.write(name.encode())
        payload=self.path/name;staging.stage(binary,payload,self.backend);return payload
    def invoke(self,action,payload=None,ok=True,mode='--test-deploy',root=None):
        command=[str(BINARY),mode,action,'--root',str(root or self.root)]
        if payload is not None:command+=['--payload',str(payload)]
        result=subprocess.run(command,text=True,capture_output=True,timeout=15)
        if ok and result.returncode:raise AssertionError(result.stderr)
        if not ok and not result.returncode:raise AssertionError('Unexpected deployment success')
        if SECRET in result.stdout+result.stderr:raise AssertionError('Credentials leaked')
        return result
    def fault(self,phase):
        path=self.root/'deployment-fault.json'
        if phase is None:path.unlink(missing_ok=True)
        else:path.write_text(json.dumps({'after':phase}));path.chmod(0o600)
    def files(self):
        return {name:((self.root/name).read_bytes(),(self.root/name).stat().st_mode&0o7777,(self.root/name).stat().st_gid) if (self.root/name).is_file() else None for name in PATHS}
    def preserved(self,test):
        test.assertEqual(self.config.read_bytes(),self.original_config)
        test.assertEqual(self.config.stat().st_mode&0o777,0o600)
        test.assertEqual(self.desktop.read_text(),'desktop controls unchanged\n')

@contextlib.contextmanager
def image(backend='create_ap'):
    with tempfile.TemporaryDirectory(prefix='relayforge-deploy-') as directory:yield Image(directory,backend)

class DeploymentTests(unittest.TestCase):
    def test_plan_is_read_only_and_native_install_preserves_configuration_and_controls(self):
        for backend in ['create_ap','networkmanager']:
            with self.subTest(backend=backend),image(backend) as c:
                before=c.files();plan=json.loads(c.invoke('plan',c.payload).stdout)
                self.assertEqual(plan['backend'],backend);self.assertEqual(c.files(),before)
                self.assertFalse((c.root/STATE).exists())
                result=json.loads(c.invoke('install',c.payload).stdout);self.assertFalse(result['activated'])
                manifest=json.loads((c.payload/'manifest.json').read_text())
                for path,entry in manifest.items():
                    installed=c.root/path;self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(),entry['sha256'])
                    self.assertEqual(oct(installed.stat().st_mode&0o777),entry['mode'])
                receipt=c.root/STATE/'state.json'
                self.assertEqual(receipt.stat().st_mode&0o777,0o600)
                self.assertEqual(receipt.parent.stat().st_mode&0o777,0o700)
                self.assertNotIn(SECRET,receipt.read_text());c.preserved(self)
                revision=json.loads(c.invoke('status').stdout)['revision']
                c.invoke('install',c.payload);self.assertEqual(json.loads(c.invoke('status').stdout)['revision'],revision)
                c.invoke('recover');c.preserved(self)

    def test_install_directory_modes_ignore_restrictive_caller_umask(self):
        with image() as c:
            before=os.umask(0o077)
            try:c.invoke('install',c.payload)
            finally:os.umask(before)
            self.assertEqual((c.root/'usr/lib').stat().st_mode&0o777,0o755)
            self.assertEqual((c.root/'usr/lib/relayforge').stat().st_mode&0o777,0o755)
            self.assertEqual((c.root/STATE).stat().st_mode&0o777,0o700)
            c.preserved(self)

    def test_upgrade_and_rollback_restore_all_replaced_predecessor_files(self):
        with image() as c:
            for path in [SERVICE,ACT,NM]:
                target=c.root/path;target.parent.mkdir(parents=True,exist_ok=True)
                for directory in c.root.rglob('*'):
                    if directory.is_dir():directory.chmod(0o755)
                target.write_bytes(b'previous installation '+path.encode());target.chmod(0o750 if path==SERVICE else 0o640)
            baseline=c.files();c.invoke('install',c.payload);v1=c.files()
            v2=c.stage('v2');c.invoke('install',v2);self.assertNotEqual(c.files()[SERVICE],v1[SERVICE])
            c.invoke('rollback');self.assertEqual(c.files(),v1)
            c.invoke('rollback');self.assertNotEqual(c.files()[SERVICE],v1[SERVICE])
            # A separate fresh installation retains the pre-installation baseline.
            c.preserved(self)
        with image() as c:
            target=c.root/ACT;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'legacy activation')
            for directory in c.root.rglob('*'):
                if directory.is_dir():directory.chmod(0o755)
            target.chmod(0o640)
            baseline=c.files();c.invoke('install',c.payload);c.invoke('rollback')
            self.assertEqual(c.files(),baseline);c.preserved(self)

    def test_every_published_transaction_phase_can_resume(self):
        for phase in ['journal',*[f'file-{n}' for n in range(8)],'files','commit']:
            with self.subTest(phase=phase),image() as c:
                c.fault(phase);c.invoke('install',c.payload,ok=False)
                c.fault(None);c.invoke('recover')
                self.assertFalse(json.loads(c.invoke('status').stdout)['pending'])
                for path in json.loads((c.payload/'manifest.json').read_text()):self.assertEqual((c.root/path).read_bytes(),(c.payload/path).read_bytes())
                c.preserved(self)

    def test_interrupted_upgrade_can_be_aborted_without_losing_old_rollback(self):
        for phase in ['journal','file-0','file-7','files']:
            with self.subTest(phase=phase),image() as c:
                c.invoke('install',c.payload);before=c.files();v2=c.stage('v2')
                c.fault(phase);c.invoke('install',v2,ok=False);c.fault(None)
                c.invoke('rollback');self.assertEqual(c.files(),before)
                c.invoke('rollback');self.assertTrue(all(value is None for value in c.files().values()));c.preserved(self)

    def test_interrupted_rollback_can_finish_without_replaying_the_wrong_generation(self):
        for phase in ['journal','file-0','file-1','file-7','files','commit']:
            with self.subTest(phase=phase),image() as c:
                c.invoke('install',c.payload);v1=c.files();c.invoke('install',c.stage('v2'))
                c.fault(phase);c.invoke('rollback',ok=False);c.fault(None)
                c.invoke('recover');self.assertEqual(c.files(),v1)
                c.invoke('recover');self.assertEqual(c.files(),v1);c.preserved(self)

    def test_pending_install_blocks_overwrite_and_preserves_new_user_configuration(self):
        with image() as c:
            c.invoke('install',c.payload);v2=c.stage('v2');c.fault('file-0')
            c.invoke('install',v2,ok=False);c.fault(None)
            changed=c.config.read_bytes()+b'# user edit while deployment pending\n';c.config.write_bytes(changed)
            c.invoke('install',v2,ok=False);c.invoke('recover')
            self.assertEqual(c.config.read_bytes(),changed)
            self.assertEqual((c.root/SERVICE).read_bytes(),(v2/SERVICE).read_bytes())

    def test_backend_switch_requires_a_separate_explicit_migration(self):
        with image() as c:
            c.invoke('install',c.payload);before=c.files()
            other=c.path/'nm-payload';staging.stage(BINARY,other,'networkmanager')
            c.config.write_text(c.config.read_text().replace('BACKEND=create_ap','BACKEND=networkmanager'))
            c.invoke('install',other,ok=False);self.assertEqual(c.files(),before)
            c.config.write_bytes(c.original_config);c.preserved(self)

    def test_modified_installed_files_and_bad_saved_blobs_are_never_overwritten(self):
        for change in ['content','mode','symlink','hardlink','blob']:
            with self.subTest(change=change),image() as c:
                c.invoke('install',c.payload);v2=c.stage('v2')
                if change=='blob':
                    c.fault('file-0');c.invoke('install',v2,ok=False);c.fault(None)
                    record=json.loads((c.root/STATE/'state.json').read_text())
                    hash=record['pending']['files'][HELPER]['sha256'];blob=c.root/STATE/'blobs'/hash;blob.write_bytes(b'changed blob')
                    before=c.files();c.invoke('recover',ok=False);self.assertEqual(c.files(),before)
                    continue
                target=c.root/SERVICE;original=target.read_bytes()
                if change=='content':target.write_bytes(b'external edit')
                elif change=='mode':target.chmod(0o700)
                elif change=='symlink':target.unlink();target.symlink_to(c.path/'v1-binary')
                else:os.link(target,c.path/'hardlink')
                c.invoke('install',v2,ok=False);c.invoke('rollback',ok=False)
                if change=='content':self.assertEqual(target.read_bytes(),b'external edit')
                elif change=='mode':self.assertEqual(target.stat().st_mode&0o777,0o700)
                elif change=='symlink':self.assertTrue(target.is_symlink())
                else:self.assertEqual(target.read_bytes(),original)
                c.preserved(self)

    def test_payload_tampering_and_manifest_escape_are_rejected_before_mutation(self):
        for change in ['hash','mode','path','missing','symlink','hardlink','fifo','template','helper']:
            with self.subTest(change=change),image() as c:
                target=c.payload/SERVICE;manifest=json.loads((c.payload/'manifest.json').read_text())
                if change=='hash':target.write_bytes(b'changed')
                elif change=='mode':target.chmod(0o700)
                elif change=='path':manifest['../../escape']=manifest.pop(SERVICE)
                elif change=='missing':target.unlink()
                elif change=='symlink':target.unlink();target.symlink_to(c.path/'v1-binary')
                elif change=='hardlink':os.link(target,c.path/'hardlink')
                elif change=='fifo':target.unlink();os.mkfifo(target)
                else:
                    target=c.payload/(ACT if change=='template' else HELPER);target.write_bytes(target.read_bytes()+b'altered')
                    manifest[str(target.relative_to(c.payload))]['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
                (c.payload/'manifest.json').write_text(json.dumps(manifest))
                before=c.files();c.invoke('install',c.payload,ok=False);self.assertEqual(c.files(),before)
                self.assertFalse((c.root/STATE).exists());c.preserved(self)

    def test_live_root_fixture_privileges_and_parent_symlinks_are_rejected(self):
        with image() as c:
            c.invoke('install',c.payload,root='/',ok=False)
            c.invoke('install',c.payload,mode='--deploy',ok=False)
            # No fallback from an explicit offline fixture to live filesystem operations.
            outside=c.path/'outside';outside.mkdir();sentinel=outside/'sentinel';sentinel.write_text('preserved')
            shutil.rmtree(c.root/'usr');(c.root/'usr').symlink_to(outside,target_is_directory=True)
            c.invoke('install',c.payload,ok=False);self.assertEqual(sentinel.read_text(),'preserved')
            self.assertEqual(list(outside.iterdir()),[sentinel])

    def test_pending_network_state_and_live_backend_lock_block_all_mutations(self):
        for kind in ['native','legacy','lock']:
            with self.subTest(kind=kind),image() as c:
                runtime=c.root/('run/wifi-relay' if kind=='legacy' else 'run/relayforge');runtime.mkdir(parents=True,mode=0o700)
                (c.root/'run').chmod(0o755)
                if kind=='lock':
                    path=runtime/'.lock';path.touch(mode=0o600)
                    with path.open('r+') as handle:
                        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);c.invoke('install',c.payload,ok=False)
                else:
                    (runtime/('create-ap.pid' if kind=='legacy' else 'create-ap.json')).write_text('retained')
                    c.invoke('install',c.payload,ok=False)
                self.assertTrue(all(value is None for value in c.files().values()));self.assertFalse((c.root/STATE).exists())

    def test_corrupt_receipt_foreign_image_and_deployment_lock_fail_closed(self):
        with image() as c:
            c.invoke('install',c.payload);before=c.files();record=c.root/STATE/'state.json';saved=record.read_bytes()
            data=json.loads(saved);data['current']['files']['../../escape']=None;record.write_text(json.dumps(data))
            c.invoke('rollback',ok=False);self.assertEqual(c.files(),before);record.write_bytes(saved)
            copy=c.path/'copy';shutil.copytree(c.root,copy);c.invoke('status',root=copy,ok=False)
            with (c.root/STATE/'.lock').open('r+') as handle:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);c.invoke('rollback',ok=False)
            self.assertEqual(c.files(),before);c.preserved(self)

    def test_configuration_must_be_private_explicit_and_match_backend(self):
        for kind in ['missing','mode','password','backend','symlink']:
            with self.subTest(kind=kind),image() as c:
                if kind=='missing':c.config.unlink()
                elif kind=='mode':c.config.chmod(0o644)
                elif kind=='password':c.config.write_text('BACKEND=create_ap\nWIFI_IFACE=demo0\n')
                elif kind=='backend':c.config.write_text(c.config.read_text().replace('BACKEND=create_ap','BACKEND=networkmanager'))
                else:
                    c.config.unlink();c.config.symlink_to(c.path/'missing')
                c.invoke('install',c.payload,ok=False);self.assertFalse((c.root/STATE).exists())
