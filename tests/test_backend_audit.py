"""Native migration audit regressions; private kernel, children and session bus only."""
import json
import signal
import unittest
from test_ap_instance import prepare,running,invoke
from test_ap_runtime import private,bench as runtime_bench
import test_ap_runtime as runtime
from test_create_ap import bench
from test_ap_devices import prepare as devices
from test_ap_network import read,write


def completion(context,directory):return context.state/'engine'/('.cleanup-'+directory.name+'.json')


class BackendAuditTests(unittest.TestCase):
    def test_retirement_resumes_after_every_destructive_phase_without_network_mutation(self):
        for phase in ['proof','markers','file','snapshot','directory']:
            with self.subTest(phase=phase),bench() as c:
                marker,_,args=prepare(c)
                with running(c,args,marker) as process:
                    directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                    private(c.state/'cleanup-control.json',dict(after=phase))
                    process.send_signal(signal.SIGUSR1);process.wait(8)
                    self.assertNotEqual(process.returncode,0)
                    proof=completion(c,directory);self.assertTrue(proof.exists())
                    self.assertNotIn('password',proof.read_text())
                    self.assertEqual(proof.stat().st_mode&0o777,0o600)
                    if phase=='snapshot':self.assertFalse((directory/'recovery.json').exists())
                    if phase=='directory':self.assertFalse(directory.exists())
                    path=c.state/'network-state.json';before=path.read_bytes()
                    (c.state/'cleanup-control.json').unlink()
                    result=invoke(c,['--recover',str(directory)])
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(path.read_bytes(),before)
                    self.assertFalse(directory.exists());self.assertFalse(proof.exists());self.assertFalse(marker.exists())

    def test_completed_proof_preserves_foreign_changed_and_replaced_files(self):
        for change in ['foreign','record','marker','proof','replacement']:
            with self.subTest(change=change),bench() as c:
                marker,_,args=prepare(c)
                with running(c,args,marker) as process:
                    directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                    private(c.state/'cleanup-control.json',dict(after='proof'))
                    process.send_signal(signal.SIGUSR1);process.wait(8)
                    (c.state/'cleanup-control.json').unlink();proof=completion(c,directory)
                    if change=='foreign':path=directory/'foreign';private(path,'keep')
                    elif change=='replacement':
                        path=directory/'wifi_iface';original=path.with_name('saved');path.rename(original)
                        # Keep the original inode outside the instance inventory.
                        original.rename(c.state/'saved');private(path,(c.state/'saved').read_text())
                    else:
                        path={'record':directory/'wifi_iface','marker':marker,'proof':proof}[change]
                        original=path.read_bytes();private(path,'changed')
                    before=(c.state/'network-state.json').read_bytes()
                    result=invoke(c,['--recover',str(directory)]);self.assertNotEqual(result.returncode,0)
                    self.assertTrue(proof.exists());self.assertTrue(path.exists())
                    self.assertEqual((c.state/'network-state.json').read_bytes(),before)
                    if change=='foreign':self.assertEqual(path.read_text(),'keep');path.unlink()
                    elif change=='replacement':path.unlink();(c.state/'saved').rename(path)
                    else:path.write_bytes(original);path.chmod(0o600)
                    result=invoke(c,['--recover',str(directory)]);self.assertEqual(result.returncode,0,result.stderr)

    def test_service_finishes_retirement_when_instance_directory_is_already_gone(self):
        from test_dbus import call,decode_reply
        with bench() as c:
            runtime.NativeRuntimeLifecycleTests.prepare(c)
            private(c.state/'cleanup-control.json',dict(after='directory'))
            with runtime.NativeRuntimeLifecycleTests.service(c) as (bus,_):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                self.assertFalse(list((c.state/'engine').glob('create_ap.*.conf.*')))
                self.assertTrue((c.state/'create-ap.json').exists())
                self.assertTrue(list((c.state/'engine').glob('.cleanup-*.json')))
                (c.state/'cleanup-control.json').unlink()
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                self.assertFalse((c.state/'create-ap.json').exists())
                self.assertFalse(list((c.state/'engine').glob('.cleanup-*.json')))

    def test_subnet_collisions_fail_before_creation_and_preserve_upstream(self):
        for route in ['192.168.0.0/16','192.168.12.128/25','192.168.12.254','invalid/24']:
            with self.subTest(route=route),bench() as c:
                marker,_,args=prepare(c)
                path=c.state/'network-state.json';state=json.loads(path.read_text());state['routes']=[route];private(path,state)
                result=invoke(c,args);self.assertNotEqual(result.returncode,0)
                self.assertEqual(json.loads(path.read_text()),state)
                self.assertFalse(marker.exists());self.assertFalse(list((c.state/'engine').glob('create_ap.*.conf.*')))

    def test_new_collision_before_address_setup_is_rechecked(self):
        with runtime_bench() as b:
            devices(b);b.invoke('interface-create');b.invoke('device-release')
            state=read(b);state['routes']=['192.168.12.128/25'];write(b,state)
            b.invoke('interface-configure',ok=False)
            self.assertEqual(read(b),state);self.assertFalse((b.directory/'interface.json').exists())
            b.invoke('interface-remove');b.invoke('device-restore')
