"""Sleep/resume integration: fake logind/NM, real Rust, no system networking."""
import contextlib
import json
from pathlib import Path
import select
import subprocess
import threading
import time
import unittest
from test_dbus import ROOT,call,connect,decode_reply,wait_for,GLib,Gio
import test_networkmanager as nm
import test_create_ap as ap
import test_ap_runtime as runtime

LOGIN='org.freedesktop.login1';PATH='/org/freedesktop/login1';MANAGER=LOGIN+'.Manager'

@contextlib.contextmanager
def logind(directory):
    with subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/fake_logind.py'),str(directory)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as process:
        try:
            ready,_,_=select.select([process.stdout],[],[],5)
            if not ready or process.stdout.readline()!=b'ready\n':raise AssertionError('Fake logind not ready')
            yield process
        finally:
            if process.poll() is None:process.terminate()
            _,stderr=process.communicate(timeout=5)
            if b'Traceback' in stderr:raise AssertionError(stderr.decode())

def power(bus,sleeping):
    bus.call_sync(LOGIN,PATH,MANAGER,'SetSleep',GLib.Variant('(b)',(sleeping,)),None,GioFlags,5000,None)

GioFlags=Gio.DBusCallFlags.NONE

def status(bus):return decode_reply('GetStatus',call(bus,'GetStatus'))
def start(bus):
    result=decode_reply('Start',call(bus,'Start'))
    if not result['success']:raise AssertionError(result)
    return result

def events(directory):
    try:return json.loads((directory/'logind.json').read_text())['events']
    except (FileNotFoundError,json.JSONDecodeError):return []

class SuspendTests(unittest.TestCase):
    def test_nm_sleep_releases_inhibitor_and_resumes_only_after_stability(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));start(bus)
            power(bus,True)
            wait_for(lambda:status(bus)['state']=='suspended')
            self.assertTrue(status(bus)['desired_active']);self.assertFalse(status(bus)['active'])
            c.assert_clean(self)
            wait_for(lambda:'released' in events(c.path))
            self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
            power(bus,False)
            time.sleep(1)
            self.assertEqual(len(c.events('activation-request')),1)
            wait_for(lambda:status(bus)['active'],seconds=13)
            self.assertEqual(len(c.events('activation-request')),2)
            self.assertGreaterEqual(events(c.path).count('inhibit'),2)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')));c.assert_clean(self)

    def test_stop_settings_and_other_profile_cancel_or_block_resume(self):
        for action in ['stop','settings','profile','missing','identity']:
            with self.subTest(action=action),nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
                wait_for(lambda:'inhibit' in events(c.path));start(bus);power(bus,True)
                wait_for(lambda:status(bus)['state']=='suspended')
                if action=='stop':call(bus,'Stop')
                elif action=='settings':call(bus,'SetConfig',GLib.Variant('(s)',(json.dumps({'SSID':'Changed'}),)))
                elif action=='profile':c.update(upstream_changed=True)
                elif action=='missing':c.update(upstream_missing=True)
                else:c.update(parent_replaced=True)
                power(bus,False);time.sleep(10)
                self.assertFalse(status(bus)['active']);self.assertEqual(len(c.events('activation-request')),1)
                if action in ['stop','settings']:self.assertFalse(status(bus)['desired_active'])
                call(bus,'Stop');c.assert_clean(self)

    def test_cleanup_failure_blocks_relaunch_and_stop_retry_cancels_intent(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));start(bus);c.update(cleanup_fails=True)
            power(bus,True);wait_for(lambda:status(bus)['state']=='failed')
            self.assertTrue((c.state/'networkmanager.json').exists())
            power(bus,False);time.sleep(9)
            self.assertEqual(len(c.events('activation-request')),1)
            c.update(cleanup_fails=False);call(bus,'Stop');time.sleep(1)
            self.assertFalse(status(bus)['desired_active']);c.assert_clean(self)

    def test_sleep_cancels_inflight_start_and_does_not_grant_resume(self):
        with nm.bench(hang_activation=True) as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));result=[]
            def launch():
                other=connect()
                try:result.append(decode_reply('Start',call(other,'Start')))
                finally:other.close_sync(None)
            thread=threading.Thread(target=launch);thread.start()
            wait_for(lambda:bool(c.events('activation-request')));power(bus,True)
            thread.join(5);self.assertFalse(thread.is_alive());self.assertFalse(result[0]['success'])
            wait_for(lambda:status(bus)['state']=='suspended');self.assertFalse(status(bus)['desired_active'])
            c.assert_clean(self);power(bus,False);time.sleep(9)
            self.assertEqual(len(c.events('activation-request')),1)

    def test_untrusted_sleep_signal_and_status_reads_never_start_or_stop(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path))
            power(bus,False)
            for _ in range(5):self.assertFalse(status(bus)['active'])
            self.assertFalse(c.events('activation-request'));start(bus)
            bus.emit_signal(None,PATH,MANAGER,'PrepareForSleep',GLib.Variant('(b)',(True,)))
            time.sleep(1);self.assertTrue(status(bus)['active'])
            call(bus,'Stop');c.assert_clean(self)

    def test_logind_loss_cancels_resume_and_cleans_owned_resources(self):
        with nm.bench() as (c,_),logind(c.path) as login,c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));start(bus)
            login.terminate();login.wait(3)
            wait_for(lambda:not status(bus)['active'],seconds=8)
            self.assertFalse(status(bus)['desired_active']);c.assert_clean(self)

    def test_no_previous_start_never_resumes_and_wake_returns_idle(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));power(bus,True)
            wait_for(lambda:status(bus)['state']=='suspended')
            self.assertFalse(status(bus)['desired_active']);power(bus,False)
            wait_for(lambda:status(bus)['state']=='idle')
            self.assertFalse(c.events('activation-request'));c.assert_clean(self)

    def test_malformed_sleep_signal_invalidates_intent(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));start(bus)
            bus.call_sync(LOGIN,PATH,MANAGER,'Malformed',None,None,GioFlags,5000,None)
            wait_for(lambda:not status(bus)['active'],seconds=8)
            self.assertFalse(status(bus)['desired_active']);c.assert_clean(self)

    def test_roaming_on_original_profile_uses_fresh_channel(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));start(bus);power(bus,True)
            wait_for(lambda:status(bus)['state']=='suspended');c.update(roamed=True)
            power(bus,False);wait_for(lambda:status(bus)['active'],seconds=13)
            self.assertEqual(len(c.events('activation-request')),2)
            call(bus,'Stop');c.assert_clean(self)

    def test_stop_cancels_automatic_activation_without_late_restart(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));start(bus);power(bus,True)
            wait_for(lambda:status(bus)['state']=='suspended');c.update(hang_activation=True)
            power(bus,False);wait_for(lambda:len(c.events('activation-request'))==2,seconds=13)
            before=time.monotonic();call(bus,'Stop')
            self.assertLess(time.monotonic()-before,2)
            c.update(hang_activation=False);time.sleep(3)
            self.assertFalse(status(bus)['desired_active']);self.assertFalse(status(bus)['active'])
            self.assertEqual(len(c.events('activation-request')),2);c.assert_clean(self)

    def test_failed_resume_retries_after_cleanup_and_backoff(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            wait_for(lambda:'inhibit' in events(c.path));start(bus);power(bus,True)
            wait_for(lambda:status(bus)['state']=='suspended');c.update(fail_activation=True)
            power(bus,False);wait_for(lambda:len(c.events('activation-request'))==2,seconds=13)
            wait_for(lambda:status(bus)['state']=='waiting')
            self.assertTrue(status(bus)['desired_active']);c.assert_clean(self)
            count=len(c.events('activation-request'));time.sleep(1)
            self.assertEqual(len(c.events('activation-request')),count)
            c.update(fail_activation=False)
            wait_for(lambda:status(bus)['active'],seconds=13)
            call(bus,'Stop');c.assert_clean(self)

    def test_create_ap_sleep_and_resume_keeps_desktop_contract(self):
        with ap.bench() as c:
            runtime.NativeRuntimeLifecycleTests.prepare(c)
            (c.root/'control.json').write_text('{}')
            with subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/fake_networkmanager.py'),str(c.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as fake:
                try:
                    ready,_,_=select.select([fake.stdout],[],[],5)
                    self.assertTrue(ready);self.assertEqual(fake.stdout.readline(),b'ready\n')
                    with logind(c.root),runtime.NativeRuntimeLifecycleTests.service(c) as (bus,_):
                        wait_for(lambda:'inhibit' in events(c.root));start(bus);power(bus,True)
                        wait_for(lambda:status(bus)['state']=='suspended');self.assertTrue(status(bus)['desired_active'])
                        self.assertFalse((c.state/'create-ap.json').exists());power(bus,False)
                        wait_for(lambda:status(bus)['active'],seconds=13)
                        self.assertEqual(status(bus)['backend'],'create_ap');call(bus,'Stop');self.assertFalse((c.state/'create-ap.json').exists())
                finally:
                    fake.terminate();fake.communicate(timeout=5)
