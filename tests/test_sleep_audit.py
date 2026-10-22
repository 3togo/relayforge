"""Deterministic sleep races on a private bus; never touches system networking."""
import json
import subprocess
import threading
import time
import unittest
from test_dbus import BINARY,call,connect,decode_reply,wait_for,GLib,fake_polkit,running
from test_suspend import logind,power,status,start,events
import test_networkmanager as nm


def control(c,**fields):
    path=c.path/'logind-control.json'
    current=json.loads(path.read_text()) if path.exists() else {}
    current.update(fields)
    temporary=path.with_suffix('.new');temporary.write_text(json.dumps(current));temporary.replace(path)


def peek(bus):
    try:return status(bus)
    except GLib.Error:return {}


def launch():
    result=[]
    def task():
        bus=connect()
        try:result.append(decode_reply('Start',call(bus,'Start')))
        except Exception as error:result.append(error)
        finally:bus.close_sync(None)
    thread=threading.Thread(target=task);thread.start()
    return thread,result


class SleepAuditTests(unittest.TestCase):
    def test_stop_cancels_start_awaiting_authorization(self):
        held=[]
        with fake_polkit(pending=held),running('--demo-polkit') as (bus,_):
            thread,result=launch();wait_for(lambda:bool(held))
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            held[0].return_value(GLib.Variant('((bba{ss}))',((True,False,{}),)))
            thread.join(2);self.assertFalse(thread.is_alive())
            self.assertIsInstance(result[0],dict);self.assertFalse(result[0]['success'])
            self.assertFalse(status(bus)['active']);self.assertFalse(status(bus)['desired_active'])

    def test_stop_cancels_start_already_waiting_in_operation_queue(self):
        with nm.bench(hang_activation=True) as (c,_),logind(c.path),c.service() as (bus,_):
            first,r1=launch();wait_for(lambda:bool(c.events('activation-request')))
            second,r2=launch();time.sleep(.15)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            c.update(hang_activation=False)
            first.join(4);second.join(4)
            self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
            self.assertEqual(len(r1),1);self.assertEqual(len(r2),1)
            self.assertIsInstance(r1[0],dict);self.assertIsInstance(r2[0],dict)
            self.assertFalse(r1[0]['success']);self.assertFalse(r2[0]['success'])
            self.assertEqual(len(c.events('activation-request')),1)
            self.assertFalse(status(bus)['desired_active']);c.assert_clean(self)

    def test_stop_cancels_post_start_probe_and_never_returns_stale_success(self):
        with nm.bench(hold_post_start_probe=True) as (c,_),logind(c.path),c.service() as (bus,_):
            thread,result=launch();wait_for(lambda:bool(c.events('radio-held')))
            before=time.monotonic();call(bus,'Stop');thread.join(1)
            self.assertFalse(thread.is_alive());self.assertLess(time.monotonic()-before,1.5)
            self.assertIsInstance(result[0],dict);self.assertFalse(result[0]['success'])
            c.update(hold_post_start_probe=False)
            self.assertFalse(status(bus)['active']);self.assertFalse(status(bus)['desired_active']);c.assert_clean(self)

    def test_start_is_not_advertised_before_initial_sleep_snapshot(self):
        with nm.bench() as (c,_):
            control(c,initial_sleeping=True,hold_property=True)
            with logind(c.path),subprocess.Popen([str(BINARY),'--test-networkmanager',str(c.path/'capture.json'),str(c.config),str(c.state)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as process:
                bus=connect()
                try:
                    wait_for(lambda:'hold_property-held' in events(c.path))
                    with self.assertRaises(GLib.Error):call(bus,'Start')
                    self.assertFalse(c.events('activation-request'))
                    control(c,hold_property=False)
                    wait_for(lambda:peek(bus).get('state')=='suspended')
                    self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
                    self.assertFalse(status(bus)['desired_active']);c.assert_clean(self)
                finally:
                    control(c,hold_property=False);process.terminate();process.communicate(timeout=5);bus.close_sync(None)

    def test_wake_start_waits_for_inhibitor_and_stop_during_rearm_still_unlocks(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            start(bus);power(bus,True);wait_for(lambda:status(bus)['state']=='suspended')
            control(c,hold_inhibit=True);power(bus,False)
            wait_for(lambda:'hold_inhibit-held' in events(c.path))
            self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
            call(bus,'Stop');control(c,hold_inhibit=False)
            wait_for(lambda:events(c.path).count('inhibit')==2)
            start(bus);self.assertEqual(len(c.events('activation-request')),2)
            call(bus,'Stop');c.assert_clean(self)

    def test_failed_wake_inhibitor_cancels_intent_then_can_reconnect(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            start(bus);power(bus,True);wait_for(lambda:status(bus)['state']=='suspended')
            control(c,deny_inhibit=True);power(bus,False)
            wait_for(lambda:not status(bus)['desired_active'])
            self.assertEqual(len(c.events('activation-request')),1);c.assert_clean(self)
            control(c,deny_inhibit=False)
            wait_for(lambda:events(c.path).count('inhibit')==2,seconds=9)
            time.sleep(.6);self.assertFalse(status(bus)['active'])
            start(bus);call(bus,'Stop');c.assert_clean(self)

    def test_logind_loss_during_sleep_keeps_start_blocked_until_trusted_wake(self):
        with nm.bench() as (c,_),c.service() as (bus,_):
            with logind(c.path) as login:
                wait_for(lambda:'inhibit' in events(c.path),seconds=9)
                time.sleep(.1);start(bus);power(bus,True)
                wait_for(lambda:status(bus)['state']=='suspended')
                login.terminate();login.wait(3)
                wait_for(lambda:not status(bus)['desired_active'])
                self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
                c.assert_clean(self)
            with logind(c.path):
                wait_for(lambda:'inhibit' in events(c.path),seconds=9)
                time.sleep(.6);start(bus);call(bus,'Stop');c.assert_clean(self)

    def test_external_settings_edit_during_final_probe_cancels_resume(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            start(bus);power(bus,True);wait_for(lambda:status(bus)['state']=='suspended')
            baseline=len(c.events('radio-read'));power(bus,False)
            wait_for(lambda:len(c.events('radio-read'))>=baseline+5,seconds=10)
            c.update(hold_radio=True);wait_for(lambda:bool(c.events('radio-held')))
            # Finish a valid observation only after its stability window matured.
            time.sleep(2.1)
            c.config.write_text(c.config.read_text().replace('SSID=Native Test','SSID=External edit'))
            c.update(hold_radio=False)
            wait_for(lambda:not status(bus)['desired_active'])
            self.assertFalse(status(bus)['active']);self.assertEqual(len(c.events('activation-request')),1)
            c.assert_clean(self)

    def test_second_sleep_cancels_resume_and_retains_only_original_intent(self):
        with nm.bench() as (c,_),logind(c.path),c.service() as (bus,_):
            start(bus);power(bus,True);wait_for(lambda:status(bus)['state']=='suspended')
            c.update(hang_activation=True);power(bus,False)
            wait_for(lambda:len(c.events('activation-request'))==2,seconds=13)
            power(bus,True);wait_for(lambda:status(bus)['state']=='suspended')
            self.assertTrue(status(bus)['desired_active']);c.assert_clean(self)
            c.update(hang_activation=False);power(bus,False)
            wait_for(lambda:status(bus)['active'],seconds=13)
            self.assertEqual(len(c.events('activation-request')),3)
            call(bus,'Stop');c.assert_clean(self)
