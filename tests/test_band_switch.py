"""Explicit reassociation on a private bus and simulated radio only."""
import json
import threading
import unittest
from test_suspend import logind, power
from test_dbus import BINARY, call, connect, decode_reply, GLib, running, wait_for
from test_networkmanager import bench


def service(c, enabled=True):
    args=[c.path/'capture.json', c.config, c.state]
    if enabled:args.append('--allow-band-switch')
    return running('--test-networkmanager', *args)


def switch(bus, target='2.4'):
    return decode_reply('SwitchBandAndReconnect',call(bus,'SwitchBandAndReconnect',GLib.Variant('(s)',(target,))))


def pending(c):return c.state/'band-switch/band-switch.json'


class NativeBandSwitchTests(unittest.TestCase):
    def test_default_mode_never_scans_or_reassociates(self):
        with bench(band_switch=True) as (c,_),service(c,False) as (bus,_):
            self.assertFalse(switch(bus)['success'])
            caps=decode_reply('GetCapabilities',call(bus,'GetCapabilities'))
            self.assertFalse(caps['upstream_band_switch']['enabled'])
            self.assertFalse(c.events('band-scan'))
            self.assertFalse(c.events('band-activation'))

    def test_strongest_same_ssid_permitted_candidate_then_idle_not_hotspot(self):
        with bench(band_switch=True) as (c,_),service(c) as (bus,_):
            self.assertTrue(switch(bus)['success'])
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2])
            self.assertFalse(pending(c).exists())
            status=decode_reply('GetStatus',call(bus,'GetStatus'))
            self.assertEqual(status['state'],'idle')
            self.assertFalse(status['active']);self.assertFalse(status['desired_active'])
            self.assertFalse(c.events('activation-request'))
            self.assertEqual(c.trace()['foreign_deletes'],0)
            self.assertNotIn('must-never-appear-in-diagnostics',json.dumps(c.trace()))

    def test_matching_current_band_is_a_no_op(self):
        with bench(band_switch=True) as (c,_),service(c) as (bus,_):
            self.assertTrue(switch(bus,'5')['success'])
            self.assertFalse(c.events('band-activation'))
            self.assertFalse(pending(c).exists())

    def test_freshness_ssid_profile_pin_and_malformed_bssid_fail_before_activation(self):
        for option in ('stale_scan','foreign_ssid','pinned_band','bad_bssid'):
            with self.subTest(option=option),bench(band_switch=True,**{option:True}) as (c,_),service(c) as (bus,_):
                self.assertFalse(switch(bus)['success'])
                self.assertFalse(c.events('band-activation'))
                self.assertFalse(pending(c).exists())

    def test_wrong_association_restores_original_without_profile_writes(self):
        with bench(band_switch=True,wrong_association=True) as (c,_),service(c) as (bus,_):
            result=switch(bus)
            self.assertFalse(result['success'])
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2,1])
            self.assertFalse(pending(c).exists())
            self.assertEqual(c.trace()['parent_writes'],0)

    def start_async(self,c,bus):
        result=[]
        def request():
            other=connect()
            try:result.append(switch(other))
            except BaseException as error:result.append(error)
            finally:other.close_sync(None)
        thread=threading.Thread(target=request);thread.start()
        wait_for(lambda:bool(c.events('band-activation')))
        return thread,result

    def test_stop_cancels_and_restores_before_reporting_clean(self):
        with bench(band_switch=True,hang_switch=True) as (c,_),service(c) as (bus,_):
            thread,result=self.start_async(c,bus)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            thread.join(5)
            self.assertFalse(thread.is_alive());self.assertIsInstance(result[0],dict)
            self.assertFalse(result[0]['success'])
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2,1])
            self.assertFalse(pending(c).exists())

    def test_external_changes_never_trigger_reconnection_or_discard_evidence(self):
        for option in ('foreign_active','foreign_ap','profile_changed','parent_replaced'):
            with self.subTest(option=option),bench(band_switch=True,hang_switch=True) as (c,_),service(c) as (bus,_):
                thread,result=self.start_async(c,bus)
                c.update(**{option:True})
                thread.join(5)
                self.assertFalse(thread.is_alive());self.assertIsInstance(result[0],dict)
                self.assertFalse(result[0]['success']);self.assertIn('journal retained',result[0]['error'])
                self.assertTrue(pending(c).exists())
                self.assertEqual(len(c.events('band-activation')),1)
                self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                with self.assertRaises(GLib.Error):call(bus,'Start')

    def test_ambiguous_activation_retains_write_ahead_record_and_blocks_restart(self):
        with bench(band_switch=True,ambiguous_activation=True) as (c,_):
            with service(c) as (bus,_):
                result=switch(bus)
                self.assertFalse(result['success']);self.assertIn('journal retained',result['error'])
                record=json.loads(pending(c).read_text())
                self.assertEqual(record['phase']['stage'],'Prepared')
                self.assertNotIn('PASSPHRASE',pending(c).read_text())
                self.assertNotIn('Test upstream',pending(c).read_text())
            with self.assertRaisesRegex(AssertionError,'journal retained'):
                with service(c):pass
            self.assertEqual(len(c.events('band-activation')),1)

    def test_reused_activation_reply_is_not_adopted_as_owned(self):
        with bench(band_switch=True,reused_active_reply=True) as (c,_),service(c) as (bus,_):
            result=switch(bus)
            self.assertFalse(result['success']);self.assertIn('journal retained',result['error'])
            self.assertEqual(json.loads(pending(c).read_text())['phase']['stage'],'Prepared')
            self.assertEqual(len(c.events('band-activation')),1)

    def test_confirmed_activation_crash_restores_on_restart_even_without_opt_in(self):
        with bench(band_switch=True,hang_switch=True) as (c,_):
            with service(c) as (bus,process):
                thread,result=self.start_async(c,bus)
                wait_for(lambda:pending(c).exists() and json.loads(pending(c).read_text())['phase']['stage']=='Switching')
                process.kill();process.wait(5);thread.join(5)
            c.update(hang_switch=False)
            with service(c,False) as (bus,_):
                self.assertFalse(pending(c).exists())
                self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2,1])

    def test_failed_restore_preserves_unknown_phase_and_stop_does_not_retry_blindly(self):
        with bench(band_switch=True,wrong_association=True,rollback_fails=True) as (c,_),service(c) as (bus,_):
            self.assertFalse(switch(bus)['success'])
            self.assertEqual(json.loads(pending(c).read_text())['phase']['stage'],'RestorePrepared')
            self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2,1])

    def test_exec_stop_post_recovers_owned_switches_and_reports_ambiguous_intents(self):
        from test_recover_all import recover
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous), bench(
                    band_switch=True, hang_switch=True, ambiguous_activation=ambiguous) as (c, _):
                with service(c) as (bus, process):
                    if ambiguous:
                        self.assertFalse(switch(bus)['success'])
                    else:
                        thread, result = self.start_async(c, bus)
                        wait_for(lambda: pending(c).exists() and
                                 json.loads(pending(c).read_text())['phase']['stage'] == 'Switching')
                    process.kill(); process.wait(5)
                    if not ambiguous:
                        thread.join(5)
                c.update(hang_switch=False)
                result = recover('--test-networkmanager', c.path/'capture.json', c.config, c.state)
                self.assertEqual(result.returncode == 0, not ambiguous, result.stderr)
                self.assertEqual(pending(c).exists(), ambiguous)
                expected = [2] if ambiguous else [2, 1]
                self.assertEqual([e['point'] for e in c.events('band-activation')], expected)

    def test_sleep_cancels_switch_and_restores_without_starting_a_hotspot(self):
        with bench(band_switch=True,hang_switch=True) as (c,_),logind(c.path),service(c) as (bus,_):
            thread,result=self.start_async(c,bus)
            power(bus,True)
            thread.join(5)
            self.assertFalse(thread.is_alive());self.assertIsInstance(result[0],dict)
            self.assertFalse(result[0]['success'])
            wait_for(lambda:decode_reply('GetStatus',call(bus,'GetStatus'))['state']=='suspended')
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2,1])
            self.assertFalse(pending(c).exists())
            power(bus,False)
            self.assertFalse(c.events('activation-request'))

    def test_provider_replacement_cannot_adopt_the_previous_journal(self):
        import select
        import subprocess
        from test_dbus import ROOT
        with bench(band_switch=True,hang_switch=True) as (c,fake):
            with service(c) as (bus,process):
                thread,result=self.start_async(c,bus)
                wait_for(lambda:pending(c).exists() and json.loads(pending(c).read_text())['phase']['stage']=='Switching')
                process.kill();process.wait(5);thread.join(5)
            saved=pending(c).read_bytes()
            fake.terminate();fake.wait(5)
            with subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/fake_networkmanager.py'),str(c.path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as replacement:
                try:
                    ready,_,_=select.select([replacement.stdout],[],[],5)
                    self.assertTrue(ready);self.assertEqual(replacement.stdout.readline(),b'ready\n')
                    with self.assertRaisesRegex(AssertionError,'identity changed|journal retained'):
                        with service(c):pass
                    self.assertEqual(pending(c).read_bytes(),saved)
                    self.assertFalse(c.events('band-activation'))
                finally:
                    replacement.terminate();replacement.communicate(timeout=5)

    def test_active_hotspot_blocks_band_switch_before_any_scan(self):
        with bench(band_switch=True) as (c,_),service(c) as (bus,_):
            started=decode_reply('Start',call(bus,'Start'))
            self.assertTrue(started['success'],started)
            result=switch(bus)
            self.assertFalse(result['success']);self.assertIn('Stop sharing',result['error'])
            self.assertFalse(c.events('band-scan'))
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))

    def test_stop_during_final_probe_cannot_be_reported_as_success(self):
        with bench(band_switch=True,hold_band_probe=True) as (c,_),service(c) as (bus,_):
            thread,result=self.start_async(c,bus)
            wait_for(lambda:bool(c.events('radio-held')))
            stopped=[]
            def stop():
                other=connect()
                try:stopped.append(decode_reply('Stop',call(other,'Stop')))
                finally:other.close_sync(None)
            stop_thread=threading.Thread(target=stop);stop_thread.start()
            wait_for(lambda:decode_reply('GetStatus',call(bus,'GetStatus'))['state']=='stopping')
            c.update(hold_band_probe=False)
            thread.join(5);stop_thread.join(5)
            self.assertEqual(stopped,[True]);self.assertFalse(result[0]['success'])
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2,1])
            self.assertFalse(pending(c).exists())

    def test_crash_after_restore_reply_verifies_without_another_activation(self):
        with bench(band_switch=True,wrong_association=True,hold_restore_probe=True) as (c,_):
            with service(c) as (bus,process):
                thread,result=self.start_async(c,bus)
                wait_for(lambda:pending(c).exists() and json.loads(pending(c).read_text())['phase']['stage']=='Restoring')
                process.kill();process.wait(5);thread.join(5)
            c.update(hold_restore_probe=False)
            with service(c,False):
                self.assertFalse(pending(c).exists())
            self.assertEqual([e['point'] for e in c.events('band-activation')],[2,1])

    def test_corrupted_journal_refuses_startup_without_modifying_it(self):
        with bench(band_switch=True,ambiguous_activation=True) as (c,_):
            with service(c) as (bus,_):self.assertFalse(switch(bus)['success'])
            record=json.loads(pending(c).read_text());record['unexpected']='foreign metadata'
            content=json.dumps(record);pending(c).write_text(content)
            with self.assertRaisesRegex(AssertionError,'Invalid native ownership journal'):
                with service(c):pass
            self.assertEqual(pending(c).read_text(),content)
            self.assertEqual(len(c.events('band-activation')),1)


if __name__=='__main__':unittest.main()
