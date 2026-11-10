"""Native bridge ownership against temporary interfaces and disposable daemons."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import unittest
from test_ap_runtime import bench,private,CHILD
import test_ap_runtime as runtime
from test_ap_network import device,read,write
from test_ap_devices import prepare as devices,finish,iface
from test_ap_daemons import running as daemons,wait_for
from test_create_ap import bench as supervised
from test_ap_instance import prepare as instance_prepare,running as instance_running,invoke as instance_invoke
from test_dbus import BINARY,ROOT,call,decode_reply,running as service_running
from test_dbus import connect,GLib
import test_networkmanager as nm
import test_suspend as suspend
from test_radio import capture


def add_bridge(root):
    path=root/'network-state.json';state=json.loads(path.read_text())
    state['interfaces']['brtest0']=device(9,'02:00:00:00:00:09',kind='bridge',addresses=['192.0.2.1/24'])
    state['interfaces']['brtest0']['address_assignment']=3
    private(path,state)
    net=root/'net/brtest0';net.mkdir()
    (net/'ifindex').write_text('9\n');(net/'address').write_text('02:00:00:00:00:09\n')
    return state


def prepare(b,configure=True,plan=True):
    devices(b);before=add_bridge(b.root)
    b.fields.update(SHARE_METHOD='bridge',INTERNET_IFACE='brtest0',BRIDGE_IFACE='brtest0',NO_DNS='1')
    b.save()
    config=dict(WIFI_IFACE='demo0',INTERNET_IFACE='brtest0',SHARE_METHOD='bridge',SSID='Bridge fixture',PASSPHRASE='bridge-secret-not-in-journal',FREQ_BAND='2.4',CHANNEL='1')
    marker=Path(b.fields['DAEMON_PIDFILE'])
    private(marker.with_suffix('.conf'),'\n'.join(k+'='+v for k,v in config.items())+'\n')
    private(marker,str(os.getpid())+'\n')
    (b.root/'bin').mkdir(exist_ok=True)
    for name in ('hostapd','dnsmasq'):
        shutil.copyfile(CHILD,b.root/'bin'/name);(b.root/'bin'/name).chmod(0o700)
    radio=capture();radio['link']='Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Upstream\n\tfreq: 2412\n'
    private(b.root/'live-radio.json',radio);private(b.root/'radio-module','test_driver')
    if plan:b.invoke('bridge-prepare');b.invoke('radio-check')
    if configure:
        b.invoke('interface-create');b.invoke('device-release')
        b.invoke('daemons-configure',None,'1','2.4','nl80211');b.invoke('interface-configure')
    return before


def intent(b):return json.loads((b.directory/'bridge.json').read_text())


def service(c,enabled=True):
    args=[c.radio,c.config,c.state,ROOT/'tests/native_create_ap.sh']
    if enabled:args.append('--allow-existing-bridge')
    return service_running('--test-create-ap',*args)


class BridgeTransactions(unittest.TestCase):
    def assert_preserved(self,b,before):
        after=read(b)
        self.assertEqual(after['interfaces'],before['interfaces'])
        self.assertEqual(after.get('routes',[]),before.get('routes',[]))
        self.assertEqual(after['global'],before['global'])
        self.assertEqual(after['forwarding'],before['forwarding'])
        self.assertEqual(after['nm']['devices'],before['nm']['devices'])
        for args in after.get('calls',[]):
            self.assertNotIn('sysctl',args);self.assertNotIn('address',args)
            self.assertNotIn('flush',args)
            self.assertFalse(args[:2]==['NetworkManager','demo0'])
            self.assertFalse(args[:4]==['ip','link','set','brtest0'])

    def test_bridge_only_renders_hostapd_and_attaches_owned_child(self):
        with bench() as b:
            before=prepare(b)
            name=iface(b);state=read(b)
            self.assertEqual(state['interfaces'][name]['master'],'brtest0')
            self.assertEqual(state['interfaces'][name]['addresses'],[])
            self.assertEqual(intent(b)['phase'],'Attached')
            self.assertEqual((b.directory/'bridge.json').stat().st_mode&0o777,0o600)
            self.assertNotIn('bridge-secret',(b.directory/'bridge.json').read_text())
            self.assertIn('bridge=brtest0\n',(b.directory/'hostapd.conf').read_text())
            self.assertFalse((b.directory/'dnsmasq.conf').exists())
            self.assertIsNone(json.loads((b.directory/'daemons.json').read_text())['account'])
            with daemons(b):
                self.assertEqual(set(json.loads((b.directory/'daemons.json').read_text())['children']),{'hostapd'})
            finish(b);finish(b)
            self.assertEqual(intent(b)['phase'],'Clean');self.assert_preserved(b,before)

    def test_bridge_configuration_is_idempotent_and_uses_no_subnet_lease(self):
        with bench() as b:
            before=prepare(b);calls=read(b)['calls']
            b.invoke('interface-configure');self.assertEqual(read(b)['calls'],calls)
            b.invoke('forwarding-enable',ok=False)
            self.assertFalse((b.engine/'forwarding.json').exists())
            b.invoke('stop-children');finish(b);self.assert_preserved(b,before)

    def test_non_bridge_down_or_mastered_upstream_is_rejected_before_creation(self):
        for change in ('kind','up','master','missing','auto-mac'):
            with self.subTest(change=change),bench() as b:
                prepare(b,False);state=read(b)
                if change=='missing':state['interfaces'].pop('brtest0');shutil.rmtree(b.root/'net/brtest0')
                elif change=='master':state['interfaces']['brtest0']['master']='foreign0'
                elif change=='up':state['interfaces']['brtest0']['up']=False
                elif change=='auto-mac':state['interfaces']['brtest0']['address_assignment']=1
                else:state['interfaces']['brtest0']['kind']='managed'
                write(b,state);before=(b.root/'network-state.json').read_bytes()
                b.invoke('interface-create',ok=False)
                self.assertEqual((b.root/'network-state.json').read_bytes(),before)

    def test_missing_opt_in_journal_never_creates_an_ap(self):
        with bench() as b:
            prepare(b,False);(b.directory/'bridge.json').unlink()
            before=read(b);b.invoke('interface-create',ok=False)
            self.assertEqual(read(b),before)

    def test_attach_and_detach_failures_and_lost_replies_recover(self):
        for fault in ('fail_at','lose_at'):
            for phase in ('attach','detach','delete'):
                with self.subTest(fault=fault,phase=phase),bench() as b:
                    before=prepare(b,False)
                    b.invoke('interface-create');b.invoke('device-release')
                    b.invoke('daemons-configure',None,'1','2.4','nl80211')
                    if phase!='attach':b.invoke('interface-configure')
                    state=read(b);state[fault]=state['mutations']+(2 if phase=='delete' else 1);write(b,state)
                    if phase=='attach':b.invoke('interface-configure',ok=False)
                    else:b.invoke('interface-remove',ok=False)
                    b.invoke('stop-children');finish(b);finish(b)
                    self.assert_preserved(b,before)

    def test_foreign_changes_retain_evidence_and_do_not_mutate_network(self):
        for change in ('bridge-mac','bridge-index','parent-phy','child-index','master','address','scope'):
            with self.subTest(change=change),bench() as b:
                prepare(b);name=iface(b);state=read(b)
                if change=='bridge-mac':
                    state['interfaces']['brtest0']['identity']['mac']='02:00:00:00:00:99';b.interface('brtest0',9,'02:00:00:00:00:99')
                elif change=='bridge-index':
                    state['interfaces']['brtest0']['identity']['ifindex']=99;b.interface('brtest0',99,'02:00:00:00:00:09')
                elif change=='parent-phy':state['phys']['demo0']=4
                elif change=='child-index':
                    state['interfaces'][name]['identity']['ifindex']=99;b.interface(name,99,state['interfaces'][name]['identity']['mac'])
                elif change=='master':state['interfaces'][name]['master']='foreign0'
                elif change=='address':state['interfaces'][name]['addresses']=['198.51.100.3/24']
                else:
                    record=intent(b);record['scope']['inode']+=1;private(b.directory/'bridge.json',record)
                write(b,state)
                b.invoke('stop-children',ok=change not in ('child-index','master','address'))
                before=(b.root/'network-state.json').read_bytes()
                b.invoke('interface-remove',ok=False)
                self.assertEqual((b.root/'network-state.json').read_bytes(),before)
                self.assertTrue((b.directory/'bridge.json').exists())

    def test_hostapd_already_detached_child_is_cleaned_without_remastering(self):
        with bench() as b:
            before=prepare(b);b.invoke('stop-children')
            state=read(b);state['interfaces'][iface(b)].pop('master');write(b,state)
            calls=len(read(b)['calls']);finish(b)
            self.assertFalse(any('nomaster' in args for args in read(b)['calls'][calls:]))
            self.assert_preserved(b,before)

    def test_early_creation_reply_loss_and_prelaunch_cleanup_preserve_upstream(self):
        for fault in ('fail_at','lose_at'):
            with self.subTest(fault=fault),bench() as b:
                before=prepare(b,False);state=read(b);state[fault]=1;write(b,state)
                b.invoke('interface-create',ok=False)
                b.invoke('stop-children');finish(b);self.assert_preserved(b,before)

    def test_durable_setup_boundaries_recover_after_interruption(self):
        for point in ('ChildOwned','AttachRequested','Attached'):
            with self.subTest(point=point),bench() as b:
                before=prepare(b,False)
                b.invoke('interface-create');b.invoke('device-release')
                b.invoke('daemons-configure',None,'1','2.4','nl80211')
                private(b.root/'bridge-control.json',{'after':point})
                b.invoke('interface-configure',ok=False)
                self.assertEqual(intent(b)['phase'],point)
                b.invoke('stop-children');finish(b);finish(b)
                self.assert_preserved(b,before)

    def test_durable_cleanup_boundaries_recover_after_interruption(self):
        for point in ('DetachRequested','detached','deleted','Clean'):
            with self.subTest(point=point),bench() as b:
                before=prepare(b)
                private(b.root/'bridge-control.json',{'after':point})
                b.invoke('interface-remove',ok=False)
                self.assertTrue((b.directory/'bridge.json').exists())
                finish(b);finish(b);self.assert_preserved(b,before)

    def test_prepared_boundary_can_clean_up_without_creating_a_child(self):
        with bench() as b:
            before=prepare(b,False,False)
            private(b.root/'bridge-control.json',{'after':'Prepared'})
            b.invoke('bridge-prepare',ok=False)
            self.assertEqual(intent(b)['phase'],'Prepared')
            b.invoke('stop-children');finish(b);self.assert_preserved(b,before)

    def test_attachment_without_write_ahead_authorization_is_not_adopted(self):
        with bench() as b:
            prepare(b,False);b.invoke('interface-create');b.invoke('device-release')
            b.invoke('daemons-configure',None,'1','2.4','nl80211')
            private(b.root/'bridge-control.json',{'after':'ChildOwned'})
            b.invoke('interface-configure',ok=False)
            state=read(b);state['interfaces'][iface(b)]['master']='brtest0';write(b,state)
            b.invoke('interface-configure',ok=False)
            b.invoke('stop-children');before=read(b)
            b.invoke('interface-remove',ok=False)
            self.assertEqual(read(b),before)
            self.assertFalse(intent(b)['attach_authorized'])

    def test_unexplained_detachment_before_stop_retains_evidence(self):
        with bench() as b:
            prepare(b);state=read(b);state['interfaces'][iface(b)].pop('master');write(b,state)
            b.invoke('stop-children',ok=False)
            b.invoke('interface-remove',ok=False)
            self.assertEqual(read(b),state);self.assertEqual(intent(b)['phase'],'Attached')

    def test_corrupt_bridge_record_is_retained_without_network_cleanup(self):
        with bench() as b:
            prepare(b);record=intent(b);record['unexpected']='foreign data';private(b.directory/'bridge.json',record)
            before=read(b)
            b.invoke('stop-children',ok=False);b.invoke('interface-remove',ok=False)
            self.assertEqual(read(b),before);self.assertEqual(intent(b),record)


class BridgeInstances(unittest.TestCase):
    def prepare(self,c):
        marker,config,args=instance_prepare(c,dict(SHARE_METHOD='bridge',INTERNET_IFACE='brtest0'))
        before=add_bridge(c.state)
        args.append('--allow-existing-bridge')
        return marker,config,args,before

    def test_instance_opt_in_is_required_and_unsupported_no_virt_is_rejected(self):
        for change in ('flag','no-virt'):
            with self.subTest(change=change),supervised() as c:
                marker,config,args,before=self.prepare(c)
                if change=='flag':args.remove('--allow-existing-bridge')
                else:private(config,config.read_text()+'NO_VIRT=1\n')
                result=instance_invoke(c,args)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(json.loads((c.state/'network-state.json').read_text()),before)

    def test_real_instance_stop_retires_bridge_journal_and_preserves_admin_bridge(self):
        with supervised() as c:
            marker,_,args,before=self.prepare(c)
            with instance_running(c,args,marker) as process:
                directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                self.assertEqual(json.loads((directory/'bridge.json').read_text())['phase'],'Attached')
                self.assertEqual(set(json.loads((directory/'daemons.json').read_text())['children']),{'hostapd'})
                process.send_signal(signal.SIGUSR1);process.wait(8);self.assertEqual(process.returncode,0)
            self.assertFalse(marker.exists());self.assertFalse(list((c.state/'engine').glob('create_ap.*.conf.*')))
            after=json.loads((c.state/'network-state.json').read_text())
            self.assertEqual(after['interfaces'],before['interfaces']);self.assertEqual(after['forwarding'],before['forwarding'])
            self.assertEqual(len(json.loads((c.state/'firewall-state.json').read_text())['rules']),1)

    def test_supervisor_opt_in_and_restart_recover_live_bridge_children(self):
        with supervised() as c:
            runtime.NativeRuntimeLifecycleTests.prepare(c);before=add_bridge(c.state)
            private(c.config,c.config.read_text()+'SHARE_METHOD=bridge\nINTERNET_IFACE=brtest0\n')
            with service(c) as (bus,process):
                result=decode_reply('Start',call(bus,'Start'));self.assertTrue(result['success'],result)
                self.assertTrue(c.record()['bridge'])
                process.kill();process.wait(5)
            with service(c,False) as (bus,_):
                self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
                self.assertFalse((c.state/'create-ap.json').exists())
            after=json.loads((c.state/'network-state.json').read_text())
            self.assertEqual(after['interfaces'],before['interfaces']);self.assertEqual(after['forwarding'],before['forwarding'])

    def test_missing_bridge_reason_is_visible_before_helper_launch(self):
        with supervised() as c:
            runtime.NativeRuntimeLifecycleTests.prepare(c)
            private(c.config,c.config.read_text()+'SHARE_METHOD=bridge\nINTERNET_IFACE=brmissing0\n')
            before=(c.state/'network-state.json').read_bytes()
            with service(c) as (bus,_):
                capabilities=decode_reply('GetCapabilities',call(bus,'GetCapabilities'))
                self.assertTrue(capabilities['existing_bridge']['enabled'])
                self.assertFalse(capabilities['startup_preflight']['success'])
                self.assertIn('existing administrator bridge',capabilities['startup_preflight']['error'])
                result=decode_reply('Start',call(bus,'Start'))
                self.assertFalse(result['success']);self.assertIn('existing administrator bridge',result['error'])
                self.assertFalse((c.state/'create-ap.json').exists())
            self.assertEqual((c.state/'network-state.json').read_bytes(),before)

    def setup_service(self,c):
        runtime.NativeRuntimeLifecycleTests.prepare(c);before=add_bridge(c.state)
        private(c.config,c.config.read_text()+'SHARE_METHOD=bridge\nINTERNET_IFACE=brtest0\n')
        return before

    def test_stop_cancels_pre_readiness_bridge_setup_and_preserves_settings(self):
        with supervised() as c:
            before=self.setup_service(c);private(c.state/'daemon-controls.json',{'ready':False})
            with service(c) as (bus,_):
                result=[]
                def start():
                    other=connect()
                    try:result.append(decode_reply('Start',call(other,'Start')))
                    except BaseException as error:result.append(error)
                    finally:other.close_sync(None)
                thread=threading.Thread(target=start);thread.start()
                wait_for(lambda:any(json.loads(p.read_text())['phase']=='Attached' for p in (c.state/'engine').glob('*/bridge.json')))
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                thread.join(5);self.assertFalse(thread.is_alive());self.assertIsInstance(result[0],dict)
                self.assertFalse(result[0]['success'])
                self.assertFalse((c.state/'create-ap.json').exists())
            after=json.loads((c.state/'network-state.json').read_text())
            self.assertEqual(after['interfaces'],before['interfaces']);self.assertEqual(after['forwarding'],before['forwarding'])

    def test_sleep_and_resume_recreate_only_owned_child_on_same_upstream(self):
        with nm.bench() as (_,_fake),supervised() as c:
            before=self.setup_service(c)
            for path in (c.radio,c.state/'live-radio.json'):
                private(path,json.loads(path.read_text()) | {'link':'Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Test upstream\n\tfreq: 2412\n'})
            with suspend.logind(c.state),service(c) as (bus,_):
                result=decode_reply('Start',call(bus,'Start'));self.assertTrue(result['success'],result)
                suspend.power(bus,True)
                wait_for(lambda:decode_reply('GetStatus',call(bus,'GetStatus'))['state']=='suspended')
                self.assertEqual(json.loads((c.state/'network-state.json').read_text())['interfaces'],before['interfaces'])
                suspend.power(bus,False)
                wait_for(lambda:decode_reply('GetStatus',call(bus,'GetStatus'))['active'],seconds=13)
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            self.assertEqual(json.loads((c.state/'network-state.json').read_text())['interfaces'],before['interfaces'])

    def test_nm_replacement_blocks_cleanup_until_owned_provider_returns(self):
        with supervised() as c:
            before=self.setup_service(c)
            with service(c) as (bus,_):
                result=decode_reply('Start',call(bus,'Start'));self.assertTrue(result['success'],result)
                path=c.state/'network-state.json';state=json.loads(path.read_text());state['nm']['owner']=':1.4444';private(path,state)
                self.assertFalse(decode_reply('Stop',call(bus,'Stop')))
                self.assertTrue((c.state/'create-ap.json').exists())
                state=json.loads(path.read_text())
                self.assertEqual(state['interfaces']['brtest0'],before['interfaces']['brtest0'])
                state['nm']['owner']=before['nm']['owner'];private(path,state)
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            self.assertEqual(json.loads(path.read_text())['interfaces'],before['interfaces'])

    def test_active_settings_cannot_redirect_frozen_bridge_cleanup(self):
        with supervised() as c:
            before=self.setup_service(c)
            with service(c) as (bus,_):
                result=decode_reply('Start',call(bus,'Start'));self.assertTrue(result['success'],result)
                self.assertTrue(decode_reply('SetConfig',call(bus,'SetConfig',GLib.Variant('(s)',('{"INTERNET_IFACE":"foreign0"}',)))))
                self.assertEqual(decode_reply('GetConfig',call(bus,'GetConfig'))['INTERNET_IFACE'],'foreign0')
                state=json.loads((c.state/'network-state.json').read_text())
                self.assertTrue(any(v.get('master')=='brtest0' for v in state['interfaces'].values()))
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                self.assertEqual(json.loads((c.state/'network-state.json').read_text())['interfaces'],before['interfaces'])
                self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])

    def test_hostapd_exec_failure_recovers_attachment_without_network_side_effects(self):
        with supervised() as c:
            before=self.setup_service(c);private(c.state/'daemon-controls.json',{'fail_child':'hostapd'})
            with service(c) as (bus,_):
                self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
            after=json.loads((c.state/'network-state.json').read_text())
            self.assertEqual(after['interfaces'],before['interfaces']);self.assertEqual(after['forwarding'],before['forwarding'])


if __name__=='__main__':unittest.main()
