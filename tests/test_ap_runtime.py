"""Real Rust snapshot/pidfd protocol, simulated net/firewall; never changes networking."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from test_dbus import BINARY,ROOT

spec=importlib.util.spec_from_file_location('pinned_recovery',ROOT/'vendor/wifi-relay/daemon/legacy_runtime.py')
reference=importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
spec=importlib.util.spec_from_file_location('runtime_staging',ROOT/'tools/stage-native.py')
staging=importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)
CHILD=Path(os.environ.get('RELAYFORGE_AP_CHILD',ROOT/'target/debug/examples/ap_child_fixture'))


def private(path,data):
    path.write_text(json.dumps(data) if not isinstance(data,str) else data)
    path.chmod(0o600)


class Bench:
    def __init__(self,path):
        self.root=Path(path)
        self.root.chmod(0o700)
        self.engine=self.root/'engine'
        self.engine.mkdir(mode=0o700)
        self.directory=self.engine/'create_ap.demo0.conf.ABCDEFGH'
        self.directory.mkdir(mode=0o755)
        self.common=self.engine/'create_ap.common.conf'
        self.common.mkdir(mode=0o700)
        (self.root/'net').mkdir(mode=0o700)
        (self.root/'supervisor').mkdir(mode=0o700)
        self.snapshot=self.directory/'recovery.json'
        self.fields=dict(CONFDIR=str(self.directory),COMMON_CONFDIR=str(self.common),WIFI_IFACE='demo0',VWIFI_IFACE='',INTERNET_IFACE='demo0',BRIDGE_IFACE='',OLD_MACADDR='',NEW_MACADDR='',NO_VIRT='0',SHARE_METHOD='nat',NO_DNS='0',DNS_PORT='',GATEWAY='192.168.12.1',RUNNING_AS_DAEMON='1',DAEMON_PIDFILE=str(self.root/('create-ap-'+'a'*32+'.pid')),ADDED_UNMANAGED='',RULE_COMMENT='wifi-relay-'+self.directory.name,BRIDGE_CREATED='0',PHYSICAL_CONFIGURED='0')
        private(self.root/'firewall.json',dict(filter='',nat=''))
        self.interface('demo0',7,'02:00:00:00:00:07')

    def interface(self,name,index,mac):
        path=self.root/'net'/name
        path.mkdir(exist_ok=True)
        (path/'ifindex').write_text(str(index)+'\n')
        (path/'address').write_text(mac+'\n')

    def invoke(self,action,path=None,*values,ok=True):
        result=subprocess.run([str(BINARY),'--test-ap-runtime',str(self.root),action,str(path or self.directory),*map(str,values)],text=True,capture_output=True,timeout=10)
        if ok and result.returncode:
            raise AssertionError(result.stderr)
        if not ok and not result.returncode:
            raise AssertionError('Unexpected runtime success: '+result.stdout)
        return result

    def save(self,pid=None,arrays=None,ok=True):
        values=[f'{key}={value}' for key,value in self.fields.items()]
        for key,items in (arrays or {}).items():
            values.extend(f'{key}[]={v}' for v in items)
        return self.invoke('save',None,pid or os.getpid(),*values,ok=ok)

    @contextlib.contextmanager
    def child(self,name='hostapd',ignore=False,own=True,common=False):
        directory=self.common if common else self.directory
        bindir=self.root/'bin'
        bindir.mkdir(exist_ok=True)
        binary=bindir/name
        if not binary.exists():
            shutil.copyfile(CHILD,binary)
            binary.chmod(0o700)
        config=(directory if own else self.root)/f'{name}.conf'
        private(config,'test-only')
        process=subprocess.Popen([str(binary),str(config),*(['--ignore-term'] if ignore else [])],start_new_session=True)
        try:
            deadline=time.monotonic()+3
            while not Path(str(config)+'.ready').exists():
                if process.poll() is not None or time.monotonic()>deadline:
                    raise AssertionError('Child did not become ready')
                time.sleep(.01)
            private(directory/(name+'.pid'),str(process.pid)+'\n')
            yield process
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL)
            process.wait(3)


@contextlib.contextmanager
def bench():
    with tempfile.TemporaryDirectory(prefix='relayforge-runtime-') as path:
        yield Bench(path)


class RuntimeTests(unittest.TestCase):
    def test_snapshot_matches_reference_and_preserves_atomic_private_schema(self):
        with bench() as b:
            arrays={'IP_ADDRS':['inet 192.0.2.4/24',"quote'and space"],'ROUTE_ADDRS':['default via 192.0.2.1']}
            b.save(arrays=arrays)
            data=json.loads(b.snapshot.read_text())
            self.assertEqual(data['owner'],reference.process_identity(os.getpid()))
            self.assertEqual(data['fields'],b.fields)
            self.assertEqual(data['arrays'],arrays)
            self.assertEqual(b.snapshot.stat().st_mode&0o777,0o600)
            oldruntime,oldnet=reference.RUNTIME,reference.SYS_NET
            try:
                reference.RUNTIME=b.engine
                reference.SYS_NET=b.root/'net'
                values=[f'{k}={v}' for k,v in b.fields.items()]+[f'{k}[]={v}' for k,vs in arrays.items() for v in vs]
                reference.save(b.directory,os.getpid(),values)
                self.assertEqual(json.loads(b.snapshot.read_text()),data)
                b.save(arrays=arrays)
                self.assertEqual(json.loads(b.snapshot.read_text()),data)
            finally:
                reference.RUNTIME,reference.SYS_NET=oldruntime,oldnet
            self.assertFalse(list(b.directory.glob('.runtime-*')))

    def test_load_quotes_shell_values_without_execution_and_refuses_live_owner(self):
        with bench() as b:
            b.save()
            self.assertIn('owner is alive',b.invoke('load',ok=False).stderr)
            owner=subprocess.Popen(['/bin/sleep','30'])
            try:
                payload=f"'$(touch {b.root}/injected)\nhello"
                b.save(pid=owner.pid,arrays={'IP_ADDRS':[payload],'ROUTE_ADDRS':['two words']},ok=False)
                # A different owner cannot overwrite the existing snapshot.
                b.snapshot.unlink()
                b.save(pid=owner.pid,arrays={'IP_ADDRS':[payload],'ROUTE_ADDRS':['two words']})
            finally:
                owner.terminate(); owner.wait(3)
            text=b.invoke('load').stdout
            result=subprocess.run(['/bin/bash','-c','eval "$1"; printf "%s" "${IP_ADDRS[0]}"','runtime-test',text],capture_output=True,text=True,check=True,timeout=3)
            self.assertEqual(result.stdout,payload)
            self.assertFalse((b.root/'injected').exists())

    def test_changed_interface_and_planned_virtual_identity_are_not_adopted(self):
        with bench() as b:
            b.fields.update(VWIFI_IFACE='aptest0',NEW_MACADDR='02:00:00:00:00:08')
            b.save()
            b.interface('aptest0',8,'02:00:00:00:00:08')
            b.invoke('check-interfaces')
            b.save()
            original=b.snapshot.read_bytes()
            for index,mac in [(9,'02:00:00:00:00:08'),(8,'02:00:00:00:00:09')]:
                b.interface('aptest0',index,mac)
                b.invoke('check-interfaces',ok=False)
                b.save(ok=False)
                self.assertEqual(b.snapshot.read_bytes(),original)
            b.interface('aptest0',8,'02:00:00:00:00:08')
            b.interface('demo0',7,'02:00:00:00:00:09')
            b.invoke('check-interfaces')
            b.interface('demo0',77,'02:00:00:00:00:09')
            b.invoke('check-interfaces',ok=False)

    def test_unrecorded_virtual_or_bridge_identity_fails_closed(self):
        for key in ('VWIFI_IFACE','BRIDGE_IFACE'):
            with self.subTest(key=key),bench() as b:
                b.fields[key]='aptest0'
                b.fields['NEW_MACADDR']='02:00:00:00:00:08'
                b.save()
                b.interface('aptest0',8,'02:00:00:00:00:09')
                b.invoke('check-interfaces',ok=False)

    def test_snapshot_rejects_untrusted_fields_missing_schema_and_bad_types(self):
        for mutate in (lambda d:d['fields'].update(PASSPHRASE='secret-must-not-echo'),lambda d:d['fields'].update(CONFDIR='/tmp/elsewhere'),lambda d:d['fields'].update(DAEMON_PIDFILE='/tmp/foreign.pid'),lambda d:d['fields'].update(RULE_COMMENT=''),lambda d:d['fields'].update(NO_VIRT='$(touch unsafe)'),lambda d:d['fields'].pop('WIFI_IFACE'),lambda d:d['arrays'].update(IP_ADDRS=['a',1]),lambda d:d.update(unexpected=True)):
            with self.subTest(mutate=mutate),bench() as b:
                b.save()
                data=json.loads(b.snapshot.read_text())
                mutate(data)
                private(b.snapshot,data)
                before=b.snapshot.read_bytes()
                result=b.invoke('load',ok=False)
                self.assertNotIn('secret-must-not-echo',result.stderr)
                self.assertEqual(b.snapshot.read_bytes(),before)

    def test_snapshot_links_fifo_permissions_and_bounds_are_rejected(self):
        for kind in ('symlink','hardlink','mode','fifo','oversize'):
            with self.subTest(kind=kind),bench() as b:
                b.save()
                if kind=='symlink':
                    target=b.root/'other'; b.snapshot.rename(target); b.snapshot.symlink_to(target)
                elif kind=='hardlink':
                    os.link(b.snapshot,b.root/'other')
                elif kind=='mode':
                    b.snapshot.chmod(0o644)
                elif kind=='fifo':
                    b.snapshot.unlink(); os.mkfifo(b.snapshot,0o600)
                else:
                    b.snapshot.write_bytes(b'x'*65537)
                b.invoke('check-interfaces',ok=False)
                b.save(ok=False)

    def test_runtime_and_instance_paths_cannot_escape_or_follow_symlinks(self):
        with bench() as b:
            b.invoke('save',b.root,os.getpid(),ok=False)
            other=b.engine/'create_ap.other0.conf.ABCDEFGH'
            other.symlink_to(b.directory,target_is_directory=True)
            b.invoke('save',other,os.getpid(),ok=False)
            b.directory.chmod(0o777)
            b.save(ok=False)
            b.directory.chmod(0o755)
            b.engine.chmod(0o755)
            b.save(ok=False)

    def test_record_stop_and_forced_kill_match_exact_reference_identity(self):
        for ignore in (False,True):
            with self.subTest(ignore=ignore),bench() as b,b.child(ignore=ignore) as child:
                b.invoke('record-child',b.directory/'hostapd.pid','hostapd')
                identity=b.directory/'hostapd.identity'
                self.assertEqual(json.loads(identity.read_text()),reference.process_identity(child.pid))
                b.invoke('stop-children')
                child.wait(3)
                self.assertEqual(child.returncode,-signal.SIGKILL if ignore else -signal.SIGTERM)
                b.invoke('stop-children')

    def test_changed_birth_and_argv_are_not_signalled(self):
        for change in ('starttime','cmdline_sha256','exe'):
            with self.subTest(change=change),bench() as b,b.child() as child:
                b.invoke('record-child',b.directory/'hostapd.pid','hostapd')
                path=b.directory/'hostapd.identity'
                data=json.loads(path.read_text())
                data[change]=('0'*64 if change=='cmdline_sha256' else data[change]+'0')
                private(path,data)
                b.invoke('stop-children')
                self.assertIsNone(child.poll())

    def test_wrong_executable_or_other_instance_config_is_never_recorded(self):
        for name,own in (('foreign',True),('hostapd',False)):
            with self.subTest(name=name,own=own),bench() as b,b.child(name=name,own=own) as child:
                if name=='foreign':
                    private(b.directory/'hostapd.pid',str(child.pid))
                b.invoke('record-child',b.directory/'hostapd.pid','hostapd',ok=False)
                self.assertFalse((b.directory/'hostapd.identity').exists())
                self.assertIsNone(child.poll())

    def test_daemonization_window_recovers_only_verified_private_children(self):
        with bench() as b,b.child() as child:
            b.invoke('stop-children')
            child.wait(3)
            self.assertTrue((b.directory/'hostapd.identity').exists())
        with bench() as b,b.child(own=False) as child:
            b.invoke('stop-children',ok=False)
            self.assertIsNone(child.poll())
        with bench() as b,b.child(name='haveged',common=True) as child:
            b.invoke('record-child',b.common/'haveged.pid','haveged')
            b.invoke('stop-children',b.common)
            child.wait(3)

    def test_watchdog_requires_direct_owner_and_unrecorded_pid_is_not_adopted(self):
        with bench() as b:
            b.save()
            child=subprocess.Popen(['/bin/bash','-c','read -r -t 30 data'],stdin=subprocess.PIPE,start_new_session=True)
            try:
                private(b.directory/'watchdog.pid',str(child.pid))
                b.invoke('stop-children',ok=False)
                self.assertIsNone(child.poll())
                b.invoke('record-child',b.directory/'watchdog.pid','bash')
                b.invoke('stop-children')
                child.wait(3)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid,signal.SIGKILL)
                child.communicate(timeout=3)

    def test_load_stops_broadcaster_even_when_interface_reuse_blocks_cleanup(self):
        with bench() as b,b.child() as child:
            owner=subprocess.Popen(['/bin/sleep','30'])
            try:
                b.save(pid=owner.pid)
                b.invoke('record-child',b.directory/'hostapd.pid','hostapd')
            finally:
                owner.terminate(); owner.wait(3)
            b.interface('demo0',99,'02:00:00:00:00:07')
            before=b.snapshot.read_bytes()
            b.invoke('load',ok=False)
            child.wait(3)
            self.assertEqual(b.snapshot.read_bytes(),before)

    def test_cleanup_requires_absent_children_interfaces_and_owned_firewall_rules(self):
        with bench() as b,b.child() as child:
            b.save()
            b.invoke('verify-cleanup',ok=False)
            b.invoke('record-child',b.directory/'hostapd.pid','hostapd')
            b.invoke('verify-cleanup',ok=False)
            b.invoke('stop-children'); child.wait(3)
            b.invoke('verify-cleanup')
            private(b.root/'firewall.json',dict(filter='-A INPUT -m comment --comment '+b.fields['RULE_COMMENT'],nat=''))
            b.invoke('verify-cleanup',ok=False)
            private(b.root/'firewall.json',dict(filter='',nat=''))
            b.fields.update(VWIFI_IFACE='aptest0',NEW_MACADDR='02:00:00:00:00:08')
            b.save()
            b.interface('aptest0',8,'02:00:00:00:00:08')
            b.invoke('verify-cleanup',ok=False)
            shutil.rmtree(b.root/'net/aptest0')
            b.invoke('verify-cleanup')

    def test_generated_bash_functions_call_rust_without_python_runtime(self):
        with bench() as b:
            generated=staging.native_engine((ROOT/'vendor/wifi-relay/daemon/create_ap').read_bytes()).decode()
            functions=generated.split('save_recovery_state() {',1)[1].split('HAVEGED_WATCHDOG_PID=',1)[0]
            shim=b.root/'runtime'
            import shlex
            shim.write_text('#!/bin/bash\nshift\nexec '+shlex.quote(str(BINARY))+' --test-ap-runtime '+shlex.quote(str(b.root))+' "$@"\n')
            shim.chmod(0o700)
            assignments='\n'.join(k+'='+shlex.quote(v) for k,v in b.fields.items())
            script='RUNTIME_HELPER='+shlex.quote(str(shim))+'\n'+assignments+'\nIP_ADDRS=("literal words")\nROUTE_ADDRS=()\nsave_recovery_state() {'+functions+'\nsave_recovery_state\n'
            result=subprocess.run(['/bin/bash','-c',script],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads(b.snapshot.read_text())
            self.assertEqual(data['fields'],b.fields)
            self.assertEqual(data['arrays']['IP_ADDRS'],['literal words'])
            self.assertNotIn('python3',generated)
            self.assertNotIn('legacy_runtime.py',generated)
            stage=b.root/'stage'
            manifest=staging.stage(BINARY,stage,'create_ap')
            self.assertEqual(len(manifest),7)
            self.assertTrue(all(not name.endswith('.py') for name in manifest))
            subprocess.run(['/bin/bash','-n',str(stage/'usr/lib/relayforge/native/create_ap')],check=True,timeout=3)


class NativeRuntimeLifecycleTests(unittest.TestCase):
    @staticmethod
    def prepare(context):
        state=context.state
        (state/'runtime-bin').symlink_to(BINARY)
        (state/'bin').mkdir(mode=0o700)
        shutil.copyfile(CHILD,state/'bin/hostapd')
        (state/'bin/hostapd').chmod(0o700)
        (state/'net').mkdir(mode=0o700)
        (state/'net/demo0').mkdir()
        (state/'net/demo0/ifindex').write_text('7\n')
        (state/'net/demo0/address').write_text('02:00:00:00:00:07\n')
        private(state/'network-state.json',dict(interfaces={
            'demo0':dict(identity=dict(ifindex=7,mac='02:00:00:00:00:07'),kind='managed',up=True,connected=True,addresses=['192.0.2.10/24']),
            'apfake0':dict(identity=dict(ifindex=101,mac='02:77:88:99:aa:bb'),kind='AP',up=False,connected=False,addresses=[])},
            **{'global':0},forwarding=dict(all=0,default=0,demo0=0,apfake0=0)))
        private(state/'firewall.json',dict(filter='',nat=''))
        private(state/'firewall-state.json',dict(rules=[dict(table='filter',chain='INPUT',args=['--comment','foreign-preserved','-j','ACCEPT'])]))

    @staticmethod
    def service(context):
        from test_dbus import running
        return running('--test-create-ap',context.radio,context.config,context.state,ROOT/'tests/native_create_ap.sh')

    def test_rust_supervisor_and_runtime_keep_desktop_client_and_retry_cleanup(self):
        from test_create_ap import bench as supervised_bench
        from test_dbus import ServiceClient,wait_for,call,decode_reply
        with supervised_bench() as context:
            self.prepare(context)
            with self.service(context) as (bus,_):
                replies,ready,signals=[],[],[]
                client=ServiceClient(lambda error:ready.append(error),lambda name,data:signals.append((name,data)))
                try:
                    wait_for(lambda:bool(ready))
                    client.call('Start',lambda value,error:replies.append((value,error)))
                    wait_for(lambda:bool(replies))
                    self.assertIsNone(replies[0][1])
                    self.assertTrue(replies[0][0]['success'])
                    wait_for(lambda:any(n=='ClientsChanged' and len(v)==1 for n,v in signals))
                    record=next((context.state/'engine').glob('*/hostapd.identity'))
                    identity=json.loads(record.read_text())
                    self.assertEqual(Path(identity['exe']).name,'hostapd')
                    self.assertNotIn('supervisor-secret',json.dumps(json.loads(record.with_name('recovery.json').read_text())))
                    (context.state/'fail-cleanup').touch()
                    self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                    self.assertTrue((context.state/'create-ap.json').exists())
                    self.assertFalse(reference.same_process(identity))
                    (context.state/'fail-cleanup').unlink()
                    self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                    self.assertFalse((context.state/'create-ap.json').exists())
                finally:
                    (context.state/'fail-cleanup').unlink(missing_ok=True)
                    client.close()

    def test_native_runtime_recovers_live_children_after_supervisor_death(self):
        from test_create_ap import bench as supervised_bench
        from test_dbus import call,decode_reply
        with supervised_bench() as context:
            self.prepare(context)
            with self.service(context) as (bus,process):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                record=next((context.state/'engine').glob('*/hostapd.identity'))
                identity=json.loads(record.read_text())
                process.kill(); process.wait(5)
            self.assertTrue(reference.same_process(identity))
            with self.service(context) as (bus,_):
                self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
                self.assertFalse((context.state/'create-ap.json').exists())
                self.assertFalse(reference.same_process(identity))
                self.assertFalse(list((context.state/'engine').glob('create_ap.demo0.conf.*')))
                self.assertEqual(len(json.loads((context.state/'firewall-state.json').read_text())['rules']),1)

    def test_firewall_failure_retains_journal_and_desktop_stop_can_retry(self):
        from test_create_ap import bench as supervised_bench
        from test_dbus import call,decode_reply
        with supervised_bench() as context:
            self.prepare(context)
            with self.service(context) as (bus,_):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                path=context.state/'firewall-state.json'
                data=json.loads(path.read_text())
                self.assertEqual(len(data['rules']),9)
                data['fail_delete']=True
                private(path,data)
                self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                self.assertTrue((context.state/'create-ap.json').exists())
                data=json.loads(path.read_text());data['fail_delete']=False;private(path,data)
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                self.assertFalse((context.state/'create-ap.json').exists())
                self.assertEqual(len(json.loads(path.read_text())['rules']),1)

    def test_forwarding_failure_retains_journal_and_desktop_stop_can_retry(self):
        from test_create_ap import bench as supervised_bench
        from test_dbus import call,decode_reply
        with supervised_bench() as context:
            self.prepare(context)
            with self.service(context) as (bus,_):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                path=context.state/'network-state.json'
                data=json.loads(path.read_text());data['fail']=True;private(path,data)
                self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                self.assertTrue((context.state/'create-ap.json').exists())
                self.assertTrue((context.state/'engine/forwarding.json').exists())
                data=json.loads(path.read_text());data['fail']=False;private(path,data)
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                self.assertFalse((context.state/'create-ap.json').exists())
                final=json.loads(path.read_text())
                self.assertEqual(final['global'],0)
                self.assertEqual(final['forwarding'],dict(all=0,default=0,demo0=0))
                self.assertEqual(final['interfaces']['demo0']['addresses'],['192.0.2.10/24'])
                self.assertFalse((context.state/'engine/forwarding.json').exists())
