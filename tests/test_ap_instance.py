"""Real Rust instance CLI and disposable children; no networking or system bus."""
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import shutil
import subprocess
import unittest
import test_ap_runtime as runtime
from test_ap_runtime import private
from test_ap_daemons import wait_for
from test_create_ap import bench
from test_dbus import BINARY


def prepare(context,patch=None):
    runtime.NativeRuntimeLifecycleTests.prepare(context)
    marker=context.state/('create-ap-'+'b'*32+'.pid')
    config=marker.with_suffix('.conf')
    values=dict(WIFI_IFACE='demo0',INTERNET_IFACE='demo0',SSID='Rust $(id) café',PASSPHRASE=' literal # $() password ',FREQ_BAND='2.4',CHANNEL='1')
    values.update(patch or {})
    private(config,'\n'.join(k+'='+v for k,v in values.items())+'\n')
    args=['--config',str(config),'--pidfile',str(marker),'--freq-band','2.4','-c','1','--daemon','--no-haveged']
    return marker,config,args


def invoke(context,args,**kwargs):
    return subprocess.run([str(BINARY),'--test-ap-instance',str(context.state),*args],text=True,capture_output=True,timeout=10,**kwargs)


@contextlib.contextmanager
def running(context,args,marker):
    process=subprocess.Popen([str(BINARY),'--test-ap-instance',str(context.state),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        wait_for(lambda:Path(str(marker)+'.ready').exists() or process.poll() is not None)
        if process.poll() is not None:raise AssertionError(process.communicate()[1])
        yield process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGUSR1)
            try:process.wait(8)
            except subprocess.TimeoutExpired:process.kill();process.wait(3)
        process.communicate(timeout=3)
        for directory in (context.state/'engine').glob('create_ap.*.conf.*'):
            # Recovery quiesces recorded children even when foreign files block deletion.
            invoke(context,['--recover',str(directory)])


class InstanceTests(unittest.TestCase):
    def test_stale_owned_control_socket_is_removed_but_live_or_foreign_entries_are_retained(self):
        for kind in ['stale','live','foreign','file','symlink']:
            with self.subTest(kind=kind),bench() as c:
                marker,_,args=prepare(c)
                with running(c,args,marker) as process:
                    directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                    iface=json.loads((directory/'recovery.json').read_text())['fields']['WIFI_IFACE']
                    control=directory/'hostapd_ctrl';control.mkdir(mode=0o700)
                    target=control/('foreign' if kind=='foreign' else iface)
                    listener=None
                    if kind=='file':private(target,'keep')
                    elif kind=='symlink':target.symlink_to(c.config)
                    else:
                        listener=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
                        fd=os.open(control,os.O_RDONLY|os.O_DIRECTORY)
                        try:listener.bind(f'/proc/self/fd/{fd}/{target.name}')
                        finally:os.close(fd)
                        if kind!='live':listener.close();listener=None
                    try:
                        process.send_signal(signal.SIGUSR1);process.wait(8)
                        if kind=='stale':
                            self.assertEqual(process.returncode,0,process.stderr.read())
                            self.assertFalse(directory.exists())
                        else:
                            self.assertNotEqual(process.returncode,0)
                            self.assertTrue(target.is_symlink() or target.exists())
                            if listener:listener.close();listener=None
                            target.unlink()
                            result=invoke(c,['--recover',str(directory)])
                            self.assertEqual(result.returncode,0,result.stderr)
                    finally:
                        if listener:listener.close()

    def test_native_options_reject_unknown_repeated_missing_and_noncanonical_values(self):
        for change in ['unknown','duplicate','missing','value','channel']:
            with self.subTest(change=change),bench() as c:
                marker,config,args=prepare(c);before=(c.state/'network-state.json').read_bytes()
                if change=='unknown':args+=['--country','password-secret']
                elif change=='duplicate':args+=['--config','password-secret']
                elif change=='missing':args.remove('--no-haveged')
                elif change=='value':args=['--config']
                else:args[7]='01'
                result=invoke(c,args);self.assertNotEqual(result.returncode,0)
                self.assertNotIn('password-secret',result.stderr)
                self.assertEqual((c.state/'network-state.json').read_bytes(),before)
                self.assertFalse((c.state/'engine').exists());self.assertFalse(marker.exists())

    def test_frozen_plan_credentials_and_private_path_are_checked_before_publication(self):
        for change in ['plan','credentials','symlink','mode','path']:
            with self.subTest(change=change),bench() as c:
                marker,config,args=prepare(c)
                if change=='plan':args[7]='6'
                elif change=='credentials':private(config,'\n'.join(v for v in config.read_text().splitlines() if not v.startswith('PASSPHRASE='))+'\n')
                elif change=='symlink':config.rename(c.state/'foreign-config');config.symlink_to(c.state/'foreign-config')
                elif change=='mode':config.chmod(0o644)
                else:args[1]=str(c.config)
                result=invoke(c,args);self.assertNotEqual(result.returncode,0)
                self.assertFalse((c.state/'engine').exists());self.assertFalse(marker.exists())

    def test_native_instance_literal_config_private_markers_and_all_stop_signals(self):
        for sig in [signal.SIGUSR1,signal.SIGTERM,signal.SIGINT,signal.SIGUSR2]:
            with self.subTest(signal=sig),bench() as c:
                marker,_,args=prepare(c);before=json.loads((c.state/'network-state.json').read_text())
                with running(c,args,marker) as process:
                    directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                    self.assertEqual(directory.stat().st_mode&0o777,0o700)
                    self.assertEqual(marker.stat().st_mode&0o777,0o600)
                    self.assertEqual(Path(str(marker)+'.ready').read_text().strip(),str(process.pid))
                    host=(directory/'hostapd.conf').read_text()
                    self.assertIn('ssid2='+'Rust $(id) café'.encode().hex()+'\n',host)
                    self.assertIn('wpa_passphrase= literal # $() password \n',host)
                    argv=Path(f'/proc/{process.pid}/cmdline').read_bytes()
                    self.assertIn(b'--test-ap-instance',argv);self.assertNotIn(b'password',argv)
                    result=invoke(c,['--recover',str(directory)])
                    self.assertNotEqual(result.returncode,0);self.assertIsNone(process.poll())
                    process.send_signal(sig);process.wait(8)
                    self.assertEqual(process.returncode,1 if sig==signal.SIGUSR2 else 0)
                    self.assertFalse(directory.exists());self.assertFalse(marker.exists());self.assertFalse(Path(str(marker)+'.ready').exists())
                after=json.loads((c.state/'network-state.json').read_text())
                self.assertEqual(after['interfaces'],before['interfaces']);self.assertEqual(after['nm']['devices'],before['nm']['devices'])
                self.assertEqual(len(json.loads((c.state/'firewall-state.json').read_text())['rules']),1)

    def test_allocation_never_overlays_foreign_directory_and_never_publishes_incomplete_snapshot(self):
        with bench() as c:
            marker,_,args=prepare(c);engine=c.state/'engine';engine.mkdir(mode=0o700)
            foreign=engine/'create_ap.demo0.conf.bbbbbbbb';foreign.mkdir(mode=0o700);private(foreign/'foreign','keep')
            before=(c.state/'network-state.json').read_bytes()
            result=invoke(c,args);self.assertNotEqual(result.returncode,0)
            self.assertEqual((foreign/'foreign').read_text(),'keep');self.assertFalse((foreign/'recovery.json').exists())
            self.assertFalse(list(engine.glob('.pending-*')));self.assertEqual((c.state/'network-state.json').read_bytes(),before)
            self.assertFalse(marker.exists())

    def test_disabled_services_and_unshared_mode_dispatch_only_their_owned_phases(self):
        for patch,rules,children in [({'NO_DNS':'1'},5,2),({'NO_DNSMASQ':'1'},4,1),({'SHARE_METHOD':'none'},6,2)]:
            with self.subTest(patch=patch),bench() as c:
                marker,_,args=prepare(c,patch)
                with running(c,args,marker) as process:
                    directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                    self.assertEqual(len(json.loads((directory/'daemons.json').read_text())['children']),children)
                    self.assertEqual(len(json.loads((c.state/'firewall-state.json').read_text())['rules']),rules)
                    if patch.get('SHARE_METHOD')=='none':self.assertFalse((c.state/'engine/forwarding.json').exists())
                    process.send_signal(signal.SIGUSR1);process.wait(8)
                    self.assertEqual(process.returncode,0,process.communicate(timeout=3)[1])

    def test_dedicated_adapter_restores_interface_and_managed_state(self):
        from test_radio import capture
        with bench() as c:
            marker,_,args=prepare(c,dict(NO_VIRT='1',INTERNET_IFACE='eth0'))
            path=c.state/'network-state.json';before=json.loads(path.read_text())
            before['interfaces']['demo0'].update(connected=False,addresses=[])
            before['nm']['devices']['demo0'].update(active=False,state=30)
            before['interfaces']['eth0']=dict(identity=dict(ifindex=8,mac='02:00:00:00:00:08'),kind='managed',up=True,connected=True,addresses=['192.0.2.10/24'])
            before['forwarding']['eth0']=0
            (c.state/'net/eth0').mkdir();(c.state/'net/eth0/ifindex').write_text('8\n');(c.state/'net/eth0/address').write_text('02:00:00:00:00:08\n')
            private(path,before);private(c.state/'live-radio.json',capture())
            with running(c,args,marker) as process:
                directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                self.assertEqual(json.loads((directory/'recovery.json').read_text())['fields']['PHYSICAL_CONFIGURED'],'1')
                process.send_signal(signal.SIGUSR1);process.wait(8);self.assertEqual(process.returncode,0)
            after=json.loads(path.read_text());self.assertEqual(after['interfaces'],before['interfaces'])
            self.assertEqual(after['nm']['devices'],before['nm']['devices'])

    def test_unknown_symlink_hardlink_or_occupied_control_directory_retains_evidence(self):
        for change in ['unknown','symlink','hardlink','control']:
            with self.subTest(change=change),bench() as c:
                marker,_,args=prepare(c)
                with running(c,args,marker) as process:
                    directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                    if change=='unknown':target=directory/'foreign';private(target,'keep')
                    elif change=='control':
                        target=directory/'hostapd_ctrl';target.mkdir(mode=0o700);private(target/'foreign','keep')
                    else:
                        target=directory/'wifi_iface';original=target.read_text();target.unlink();private(c.state/'foreign','keep')
                        if change=='symlink':target.symlink_to(c.state/'foreign')
                        else:os.link(c.state/'foreign',target)
                    process.send_signal(signal.SIGUSR1);process.wait(8)
                    self.assertNotEqual(process.returncode,0);self.assertTrue((directory/'recovery.json').exists())
                    self.assertTrue(marker.exists());self.assertTrue(target.exists())
                    self.assertEqual(set(json.loads((c.state/'network-state.json').read_text())['interfaces']),{'demo0'})
                    if change=='control':(target/'foreign').unlink();target.rmdir()
                    else:
                        target.unlink()
                        if change in ['symlink','hardlink']:
                            self.assertEqual((c.state/'foreign').read_text(),'keep');private(target,original)
                    result=invoke(c,['--recover',str(directory)]);self.assertEqual(result.returncode,0,result.stderr)
                    self.assertFalse(directory.exists());self.assertFalse(marker.exists())

    def test_killed_instance_recovers_recorded_children_and_resources(self):
        from test_ap_runtime import reference
        with bench() as c:
            marker,_,args=prepare(c)
            with running(c,args,marker) as process:
                directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                identities=[json.loads(path.read_text()) for path in directory.glob('*.identity')]
                process.kill();process.wait(5)
                result=invoke(c,['--recover',str(directory)]);self.assertEqual(result.returncode,0,result.stderr)
                self.assertFalse(directory.exists());self.assertFalse(marker.exists())
                for identity in identities:self.assertFalse(reference.same_process(identity))
                self.assertEqual(set(json.loads((c.state/'network-state.json').read_text())['interfaces']),{'demo0'})

    def test_copied_snapshot_does_not_adopt_replacement_directory(self):
        with bench() as c:
            marker,_,args=prepare(c)
            with running(c,args,marker) as process:
                directory=next((c.state/'engine').glob('create_ap.*.conf.*'));original=c.state/'engine/.original'
                directory.rename(original);shutil.copytree(original,directory)
                try:
                    process.send_signal(signal.SIGUSR1);process.wait(8)
                    self.assertNotEqual(process.returncode,0)
                    result=invoke(c,['--recover',str(directory)])
                    self.assertNotEqual(result.returncode,0);self.assertIn('directory identity changed',result.stderr)
                    self.assertTrue((directory/'recovery.json').exists());self.assertTrue(marker.exists())
                finally:
                    shutil.rmtree(directory);original.rename(directory)
                    result=invoke(c,['--recover',str(directory)])
                    self.assertEqual(result.returncode,0,result.stderr)

    def test_changed_owner_marker_blocks_housekeeping_until_explicitly_restored(self):
        with bench() as c:
            marker,_,args=prepare(c)
            with running(c,args,marker) as process:
                directory=next((c.state/'engine').glob('create_ap.*.conf.*'))
                private(marker,'999999\n');process.send_signal(signal.SIGUSR1);process.wait(8)
                self.assertNotEqual(process.returncode,0);self.assertEqual(marker.read_text(),'999999\n')
                self.assertTrue((directory/'recovery.json').exists())
                private(marker,str(process.pid)+'\n')
                result=invoke(c,['--recover',str(directory)]);self.assertEqual(result.returncode,0,result.stderr)

    def test_elf_execution_gate_uses_native_entrypoint_and_does_not_select_fixture_from_environment(self):
        with bench() as c:
            marker,config,_=prepare(c)
            helper=c.state/'native-helper';shutil.copyfile(BINARY,helper);helper.chmod(0o700)
            args=[str(BINARY),'--create-ap-exec',str(helper),str(config),str(marker),'2.4','1']
            for ack in ['',chr(1)]:
                result=subprocess.run(args,input=ack,text=True,capture_output=True,timeout=5,env=dict(os.environ,RELAYFORGE_TEST_STATE=str(c.state)))
                self.assertNotEqual(result.returncode,0)
                if ack:self.assertIn('requires root',result.stderr)
                self.assertFalse(marker.exists());self.assertFalse((c.state/'engine').exists())
