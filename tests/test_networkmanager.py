"""Native backend tests on a disposable bus and fully simulated kernel."""
import contextlib
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading
import time
import unittest
from test_dbus import ROOT, BINARY, call, connect, decode_reply, running, wait_for, GLib
from test_radio import capture


class Bench:
    def __init__(self, directory, options):
        self.path=Path(directory)
        self.state=self.path/'ownership'
        self.state.mkdir(mode=0o700)
        self.control=dict(options)
        self.update()
        self.capture=capture()
        self.capture['link']='Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Test upstream\n\tfreq: 2412\n'
        (self.path/'capture.json').write_text(json.dumps(self.capture))
        self.config=self.path/'config'
        self.config.write_text('WIFI_IFACE=demo0\nINTERNET_IFACE=demo0\nBACKEND=networkmanager\nSSID=Native Test\nPASSPHRASE=must-never-appear-in-diagnostics\n')
        self.config.chmod(0o600)

    def update(self, **options):
        self.control.update(options)
        temporary=self.path/'control.new'
        temporary.write_text(json.dumps(self.control))
        temporary.replace(self.path/'control.json')

    def trace(self):
        try:
            return json.loads((self.path/'trace.json').read_text())
        except (FileNotFoundError,json.JSONDecodeError):
            return {}

    def events(self, kind):
        return [e for e in self.trace().get('events',[]) if e['kind']==kind]

    def service(self):
        return running('--test-networkmanager', self.path/'capture.json', self.config, self.state)

    def assert_clean(self, test):
        wait_for(lambda: not self.trace().get('interfaces') and not self.trace().get('profile_ids'), seconds=6)
        test.assertFalse((self.state/'networkmanager.json').exists())
        test.assertEqual(self.trace()['foreign_deletes'],0)
        test.assertEqual(self.trace()['parent_writes'],0)


@contextlib.contextmanager
def bench(**options):
    with tempfile.TemporaryDirectory(prefix='relayforge-nm-') as directory:
        context=Bench(directory, options)
        with subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/fake_networkmanager.py'),directory],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE) as fake:
            try:
                ready,_,_=select.select([fake.stdout],[],[],5)
                if not ready or fake.stdout.readline()!=b'ready\n':
                    raise AssertionError('Fake NetworkManager did not become ready')
                yield context,fake
            finally:
                fake.terminate()
                _,stderr=fake.communicate(timeout=5)
                if b'Traceback' in stderr:
                    raise AssertionError(stderr.decode())


class NativeNetworkManagerTests(unittest.TestCase):
    def test_capabilities_explain_native_option_restrictions_without_activation(self):
        with bench() as (context, _), context.service() as (bus, _):
            capabilities = decode_reply('GetCapabilities', call(bus, 'GetCapabilities'))
            self.assertTrue(capabilities['startup_preflight']['success'])
            self.assertEqual(capabilities['startup_preflight']['scope'], 'configuration-and-radio')
            self.assertTrue(decode_reply('SetConfig', call(bus, 'SetConfig',
                GLib.Variant('(s)', ('{"NO_DNS":"1"}',)))))
            capabilities = decode_reply('GetCapabilities', call(bus, 'GetCapabilities'))
            self.assertFalse(capabilities['startup_preflight']['success'])
            self.assertIn('overrides', capabilities['startup_preflight']['error'])
            self.assertEqual(context.events('activation-request'), [])
            self.assertEqual(context.events('interface-created'), [])

    def test_external_interface_and_native_interface_paths_activate_and_stop(self):
        for native in (False,True):
            with self.subTest(native=native),bench(native=native,client=True) as (context,_):
                with context.service() as (bus,process):
                    started=decode_reply('Start',call(bus,'Start'))
                    self.assertTrue(started['success'],started)
                    self.assertEqual(started['status']['backend'],'networkmanager')
                    self.assertTrue(started['status']['active'])
                    self.assertEqual(decode_reply('Start',call(bus,'Start'))['status']['iface'],started['status']['iface'])
                    wait_for(lambda:len(decode_reply('GetClients',call(bus,'GetClients')))==1)
                    self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                    self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                    context.assert_clean(self)
                    self.assertEqual(len(context.events('activation-request')),1)
                    self.assertEqual(len(context.events('interface-created')),0 if native else 1)
                    # The sole direct child must not be a Python networking worker.
                    children=Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().strip()
                    self.assertEqual(children,'')
                    self.assertNotIn('must-never-appear', (context.path/'trace.json').read_text())

    def test_partial_creation_and_activation_failures_clean_up(self):
        for option in ('fail_create_after','deny_activation','fail_activation','bad_ap'):
            with self.subTest(option=option),bench(**{option:True}) as (context,_):
                with context.service() as (bus,_):
                    result=decode_reply('Start',call(bus,'Start'))
                    self.assertFalse(result['success'])
                    self.assertNotIn('credentials',result['error'])
                    context.assert_clean(self)
                    self.assertTrue(decode_reply('Stop',call(bus,'Stop')))

    def test_stop_cancels_activation_without_late_start_or_unresponsive_status(self):
        with bench(hang_activation=True) as (context,_),context.service() as (bus,_):
            result=[]
            def start():
                other=connect()
                try:
                    result.append(decode_reply('Start',call(other,'Start')))
                finally:
                    other.close_sync(None)
            thread=threading.Thread(target=start)
            thread.start()
            wait_for(lambda:bool(context.events('activation-request')))
            before=time.monotonic()
            self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
            self.assertLess(time.monotonic()-before,.5)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(result[0]['success'])
            self.assertEqual(decode_reply('GetStatus',call(bus,'GetStatus'))['state'],'idle')
            context.assert_clean(self)

    def test_ambiguous_activation_reply_timeout_still_cleans_owned_resources(self):
        with bench(delay_reply=True) as (context,_),context.service() as (bus,_):
            result=decode_reply('Start',call(bus,'Start'))
            self.assertFalse(result['success'])
            context.assert_clean(self)

    def test_occupied_phy_and_missing_ap_capability_never_create_resources(self):
        for option in ('sibling','no_ap','subnet_collision'):
            with self.subTest(option=option),bench(**{option:True}) as (context,_),context.service() as (bus,_):
                self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
                self.assertFalse(context.events('interface-created'))
                self.assertFalse(context.events('activation-request'))
                context.assert_clean(self)

    def test_upstream_change_during_activation_and_monitoring_never_reconnects_parent(self):
        for during in (True,False):
            with self.subTest(during=during),bench(change_during_activation=during) as (context,_),context.service() as (bus,_):
                result=decode_reply('Start',call(bus,'Start'))
                if during:
                    self.assertFalse(result['success'])
                else:
                    self.assertTrue(result['success'])
                    context.update(upstream_changed=True)
                    wait_for(lambda:decode_reply('GetStatus',call(bus,'GetStatus'))['state']=='failed')
                context.assert_clean(self)
                self.assertEqual(context.trace()['parent_writes'],0)

    def test_failed_cleanup_is_retained_and_stop_can_retry(self):
        with bench() as (context,_),context.service() as (bus,_):
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            context.update(cleanup_fails=True)
            self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
            self.assertTrue((context.state/'networkmanager.json').exists())
            self.assertEqual(decode_reply('GetStatus',call(bus,'GetStatus'))['state'],'failed')
            with self.assertRaises(GLib.Error):
                call(bus,'Start')
            context.update(cleanup_fails=False)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            context.assert_clean(self)

    def test_reused_interface_identity_is_preserved_until_operator_resolves_it(self):
        with bench() as (context,_),context.service() as (bus,_):
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            context.update(tamper_interface=True)
            self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
            self.assertFalse(context.events('interface-deleted'))
            self.assertTrue((context.state/'networkmanager.json').exists())
            context.update(tamper_interface=False)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            context.assert_clean(self)

    def test_changed_profile_ownership_is_never_deleted(self):
        with bench(retain_profile=True) as (context,_),context.service() as (bus,_):
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            context.update(tamper_profile=True)
            self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
            self.assertFalse(context.events('profile-deleted'))
            self.assertTrue((context.state/'networkmanager.json').exists())
            context.update(tamper_profile=False,retain_profile=False)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            context.assert_clean(self)

    def test_process_death_and_restart_recovers_only_journaled_resources(self):
        with bench() as (context,_):
            with context.service() as (bus,process):
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                process.kill()
                process.wait(5)
            wait_for(lambda:not context.trace().get('profile_ids'))
            self.assertTrue((context.state/'networkmanager.json').exists())
            self.assertTrue(context.trace()['interfaces'])
            with context.service() as (bus,_):
                context.assert_clean(self)
                self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])

    def test_native_mode_config_and_band_controls_preserve_upstream(self):
        with bench() as (context,_),context.service() as (bus,_):
            with self.assertRaises(GLib.Error):
                call(bus,'SetConfig',GLib.Variant('(s)',('{"BACKEND":"create_ap"}',)))
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            result=decode_reply('SwitchBandAndReconnect',call(bus,'SwitchBandAndReconnect',GLib.Variant('(s)',('5',))))
            self.assertFalse(result['success'])
            self.assertTrue(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
            self.assertTrue(decode_reply('PrepareFirewall',call(bus,'PrepareFirewall')))
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            context.assert_clean(self)
            context.config.write_text(context.config.read_text().replace('BACKEND=networkmanager','BACKEND=create_ap'))
            self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
            self.assertEqual(len(context.events('activation-request')),1)
            context.assert_clean(self)

    def test_helper_ap_mode_restoration_changes_only_owned_child(self):
        with bench(unavailable=True) as (context,_),context.service() as (bus,_):
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            self.assertEqual(len(context.events('mode-restored')),1)
            self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            context.assert_clean(self)

    def test_sigterm_cleans_up_owned_resources(self):
        with bench() as (context,_),context.service() as (bus,process):
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            process.terminate()
            self.assertEqual(process.wait(5),0)
            context.assert_clean(self)

    def test_networkmanager_restart_recovers_helper_without_reactivating(self):
        with bench() as (context,fake),context.service() as (bus,_):
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            fake.kill()
            fake.wait(5)
            wait_for(lambda:decode_reply('GetStatus',call(bus,'GetStatus'))['state']=='failed',seconds=12)
            self.assertTrue((context.state/'networkmanager.json').exists())
            with subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/fake_networkmanager.py'),str(context.path)],
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE) as replacement:
                try:
                    ready,_,_=select.select([replacement.stdout],[],[],5)
                    self.assertTrue(ready)
                    self.assertEqual(replacement.stdout.readline(),b'ready\n')
                    self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                    context.assert_clean(self)
                    self.assertFalse(context.events('activation-request'))
                finally:
                    replacement.terminate()
                    replacement.communicate(timeout=5)

    def test_unchanged_async_desktop_client_receives_native_client_and_status_signals(self):
        from test_dbus import ServiceClient
        with bench(client=True) as (context,_),context.service():
            ready,signals,replies=[],[],[]
            client=ServiceClient(lambda error:ready.append(error),lambda name,data:signals.append((name,data)))
            try:
                wait_for(lambda:bool(ready))
                self.assertIsNone(ready[0])
                client.call('Start',lambda value,error:replies.append((value,error)))
                wait_for(lambda:bool(replies))
                self.assertIsNone(replies[0][1])
                self.assertTrue(replies[0][0]['success'])
                wait_for(lambda:any(name=='ClientsChanged' and len(data)==1 for name,data in signals))
                client.call('Stop',lambda value,error:replies.append((value,error)))
                wait_for(lambda:len(replies)==2 and any(name=='StatusChanged' and data['state']=='idle' for name,data in signals))
                self.assertTrue(replies[1][0])
                context.assert_clean(self)
            finally:
                client.close()
