"""Rust-supervised real processes, fake Bash engine, private bus: no actual Wi-Fi calls."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from test_dbus import BINARY,ROOT,call,connect,decode_reply,running,wait_for,GLib,ServiceClient
from test_radio import capture


class Bench:
    def __init__(self,root,options):
        self.root=Path(root)
        self.state=self.root/'state'
        self.state.mkdir(mode=0o700)
        self.control=dict(options)
        self.update()
        radio=capture()
        radio['link']='Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Upstream\n\tfreq: 2412\n'
        self.radio=self.root/'capture.json'
        self.radio.write_text(json.dumps(radio))
        (self.state/'live-radio.json').write_text(json.dumps(radio))
        (self.state/'live-radio.json').chmod(0o600)
        self.config=self.root/'config'
        self.config.write_text('BACKEND=create_ap\nWIFI_IFACE=demo0\nINTERNET_IFACE=demo0\nSSID=Native AP\nPASSPHRASE=supervisor-secret-not-in-argv\n')
        self.config.chmod(0o600)

    def update(self,**fields):
        self.control.update(fields)
        temporary=self.state/'control.new'
        temporary.write_text(json.dumps(self.control))
        temporary.replace(self.state/'control.json')

    def events(self):
        try:
            return json.loads((self.state/'trace.json').read_text())
        except (FileNotFoundError,json.JSONDecodeError):
            return []

    def launched(self):
        return next((e for e in self.events() if e['kind']=='launched'),None)

    def record(self):
        return json.loads((self.state/'create-ap.json').read_text())

    def service(self):
        return running('--test-create-ap',self.radio,self.config,self.state,ROOT/'tests/fake_create_ap.sh')

    def clean(self,test):
        test.assertFalse((self.state/'create-ap.json').exists())
        test.assertFalse(list(self.state.glob('create-ap-*.conf')))
        engine=self.state/'engine'
        if engine.exists():
            test.assertTrue(all(p.name.endswith('FOREIGN1') for p in engine.iterdir()))


@contextlib.contextmanager
def bench(**options):
    with tempfile.TemporaryDirectory(prefix='relayforge-ap-') as directory:
        context=Bench(directory,options)
        try:
            yield context
        finally:
            # An assertion failure must not strand this non-networking simulator.
            context.update(fail_cleanup=False,ignore_stop=False)
            record=context.state/'create-ap.json'
            if record.exists():
                data=json.loads(record.read_text())
                process=data.get('process')
                if process:
                    path=Path(f'/proc/{process["pid"]}')
                    try:
                        start=(path/'stat').read_text().rsplit(')',1)[1].split()[19]
                        argv=(path/'cmdline').read_bytes()
                        if start==process['start'] and b'fake_create_ap.sh' in argv and str(context.state).encode() in argv:
                            os.kill(process['pid'],signal.SIGUSR1)
                    except (FileNotFoundError,ProcessLookupError):
                        pass


class CreateApTests(unittest.TestCase):
    def test_readiness_clients_idempotency_and_secret_free_ownership(self):
        with bench(client=True,foreign=True) as context,context.service() as (bus,_):
            started=decode_reply('Start',call(bus,'Start'))
            self.assertTrue(started['success'],started)
            self.assertEqual(started['status']['backend'],'create_ap')
            self.assertEqual(started['status']['iface'],'apfake0')
            record=context.record()
            self.assertNotIn('supervisor-secret',json.dumps(record))
            self.assertNotIn(b'supervisor-secret',Path(f'/proc/{record["process"]["pid"]}/cmdline').read_bytes())
            snapshot=next(context.state.glob('create-ap-*.conf'))
            self.assertEqual(snapshot.stat().st_mode&0o777,0o600)
            self.assertIn('supervisor-secret',snapshot.read_text())
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            self.assertEqual(len([e for e in context.events() if e['kind']=='launched']),1)
            wait_for(lambda:len(decode_reply('GetClients',call(bus,'GetClients')))==1)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            context.clean(self)
            self.assertTrue(any(p.name.endswith('FOREIGN1') for p in (context.state/'engine').iterdir()))

    def test_failure_before_pid_partial_resources_wrong_ready_and_output_flood(self):
        for option in ('fail_before_pid','fail_after_resources','wrong_ready','flood','bad_channel'):
            with self.subTest(option=option),bench(**{option:True}) as context,context.service() as (bus,_):
                reply=decode_reply('Start',call(bus,'Start'))
                self.assertFalse(reply['success'])
                self.assertNotIn('credential-must-not-leak',reply['error'])
                self.assertNotIn('supervisor-secret',reply['error'])
                context.clean(self)

    def test_startup_deadline_and_forced_termination_recover_owned_resources(self):
        for options in (dict(no_ready=True),dict(ignore_stop=True)):
            with self.subTest(options=options),bench(**options) as context,context.service() as (bus,_):
                result=decode_reply('Start',call(bus,'Start'))
                if options.get('no_ready'):
                    self.assertFalse(result['success'])
                else:
                    self.assertTrue(result['success'])
                    self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                context.clean(self)

    def test_stop_cancels_start_before_readiness_and_status_remains_responsive(self):
        with bench(no_ready=True) as context,context.service() as (bus,_):
            replies=[]
            def start():
                other=connect()
                try:
                    replies.append(decode_reply('Start',call(other,'Start')))
                finally:
                    other.close_sync(None)
            thread=threading.Thread(target=start)
            thread.start()
            wait_for(lambda:context.launched() is not None)
            before=time.monotonic()
            self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
            self.assertLess(time.monotonic()-before,.5)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(replies[0]['success'])
            context.clean(self)

    def test_cleanup_failure_keeps_journal_and_private_config_for_retry(self):
        with bench() as context,context.service() as (bus,_):
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            context.update(fail_cleanup=True)
            self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
            self.assertTrue((context.state/'create-ap.json').exists())
            self.assertTrue(list(context.state.glob('create-ap-*.conf')))
            with self.assertRaises(GLib.Error):
                call(bus,'Start')
            context.update(fail_cleanup=False)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            context.clean(self)

    def test_process_death_recovers_the_live_orphan_on_restart(self):
        with bench() as context:
            with context.service() as (bus,process):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                pid=context.record()['process']['pid']
                process.kill()
                process.wait(5)
            self.assertTrue(Path(f'/proc/{pid}').exists())
            with context.service() as (bus,_):
                context.clean(self)
                self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])

    def test_unexpected_helper_exit_upstream_change_and_channel_change_stop_sharing(self):
        for failure in ('process','upstream','channel'):
            with self.subTest(failure=failure),bench() as context,context.service() as (bus,_):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                if failure=='process':
                    os.kill(context.record()['process']['pid'],signal.SIGKILL)
                elif failure=='upstream':
                    path=context.state/'live-radio.json'
                    value=json.loads(path.read_text())
                    value['link']=value['link'].replace('2412','2437')
                    path.write_text(json.dumps(value))
                else:
                    context.update(bad_channel=True)
                wait_for(lambda:decode_reply('GetStatus',call(bus,'GetStatus'))['state']=='failed',seconds=8)
                context.clean(self)

    def test_gate_without_durable_parent_ack_never_executes_helper(self):
        with bench() as context:
            result=subprocess.run([str(BINARY),'--create-ap-exec',str(ROOT/'tests/fake_create_ap.sh'),str(context.config),str(context.state/'unowned.pid'),'2.4','1'],stdin=subprocess.DEVNULL,capture_output=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(context.events())
            self.assertFalse((context.state/'engine').exists())

    def test_native_mode_declines_band_changes_and_wrong_backend(self):
        with bench() as context,context.service() as (bus,_):
            reply=decode_reply('SwitchBandAndReconnect',call(bus,'SwitchBandAndReconnect',GLib.Variant('(s)',('5',))))
            self.assertFalse(reply['success'])
            with self.assertRaises(GLib.Error):
                call(bus,'SetConfig',GLib.Variant('(s)',('{"BACKEND":"networkmanager"}',)))
            context.config.write_text(context.config.read_text().replace('BACKEND=create_ap','BACKEND=networkmanager'))
            self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
            self.assertFalse(context.events())

    def test_unchanged_desktop_async_client_receives_create_ap_signals(self):
        with bench(client=True) as context,context.service():
            ready,signals,replies=[],[],[]
            client=ServiceClient(lambda error:ready.append(error),lambda name,data:signals.append((name,data)))
            try:
                wait_for(lambda:bool(ready))
                client.call('Start',lambda value,error:replies.append((value,error)))
                wait_for(lambda:bool(replies))
                self.assertIsNone(replies[0][1])
                self.assertTrue(replies[0][0]['success'])
                wait_for(lambda:any(n=='ClientsChanged' and len(v)==1 for n,v in signals))
                client.call('Stop',lambda value,error:replies.append((value,error)))
                wait_for(lambda:len(replies)==2 and any(n=='StatusChanged' and v['state']=='idle' for n,v in signals))
                context.clean(self)
            finally:
                client.close()

    def test_changed_or_missing_resource_owner_blocks_recovery_until_restored(self):
        for change in ('owner','missing'):
            with self.subTest(change=change),bench() as context,context.service() as (bus,_):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                context.update(fail_cleanup=True)
                self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                snapshot=next((context.state/'engine').glob('*/recovery.json'))
                original=snapshot.read_bytes()
                previous_recoveries=sum(e['kind']=='recover-request' for e in context.events())
                if change=='missing':
                    snapshot.unlink()
                else:
                    data=json.loads(original)
                    data['owner']['starttime']+='0'
                    snapshot.write_text(json.dumps(data))
                context.update(fail_cleanup=False)
                self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                self.assertTrue((context.state/'create-ap.json').exists())
                self.assertEqual(sum(e['kind']=='recover-request' for e in context.events()),previous_recoveries)
                snapshot.write_bytes(original)
                snapshot.chmod(0o600)
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                context.clean(self)

    def test_termination_cleans_owned_helper_and_resources(self):
        with bench() as context:
            with context.service() as (bus,process):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                process.terminate()
                self.assertEqual(process.wait(5),0)
            context.clean(self)

    def test_start_rejects_upstream_band_switch_and_country_override_without_execution(self):
        for patch in ({'FREQ_BAND':'5'},{'COUNTRY':'US'},{'SHARE_METHOD':'bridge'},{'NO_VIRT':'1'}):
            with self.subTest(patch=patch),bench() as context,context.service() as (bus,_):
                self.assertTrue(decode_reply('SetConfig',call(bus,'SetConfig',GLib.Variant('(s)',(json.dumps(patch),)))))
                self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
                self.assertFalse(context.events())
                context.clean(self)
