"""Private daemon rendering and real gated child tests; no system daemons/network."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from test_ap_runtime import bench,private,CHILD,staging
from test_ap_devices import prepare as devices,finish,iface
from test_ap_network import read,write
from test_dbus import BINARY,ROOT
from test_radio import capture


def prepare(b,patch=None,configure=True):
    before=devices(b)
    config=dict(WIFI_IFACE='demo0',INTERNET_IFACE='demo0',SSID='Rust café # AP',PASSPHRASE=' spaced=password# ',FREQ_BAND='2.4',CHANNEL='1')
    config.update(patch or {})
    b.fields.update(NO_DNS='1' if config.get('NO_DNS')=='1' or config.get('NO_DNSMASQ')=='1' else '0',SHARE_METHOD=config.get('SHARE_METHOD','nat'),GATEWAY=config.get('GATEWAY','192.168.12.1'))
    b.save()
    path=Path(b.fields['DAEMON_PIDFILE']).with_suffix('.conf')
    private(path,'\n'.join(k+'='+v for k,v in config.items())+'\n')
    private(Path(b.fields['DAEMON_PIDFILE']),str(os.getpid())+'\n')
    (b.root/'bin').mkdir(exist_ok=True)
    for name in ['hostapd','dnsmasq']:
        shutil.copyfile(CHILD,b.root/'bin'/name);(b.root/'bin'/name).chmod(0o700)
    radio=capture()
    # A connected parent shares the exact frozen channel, with no RF operations.
    channel=int(config['CHANNEL']);band=config['FREQ_BAND']
    frequency=(2484 if channel==14 else 2407+5*channel) if band=='2.4' else 5000+5*channel
    radio['phy']=radio['phy'].replace('* 5180 MHz [36] (20.0 dBm) (no IR)', '* 5180 MHz [36] (20.0 dBm)')
    radio['link']=f'Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Upstream\n\tfreq: {frequency}\n'
    private(b.root/'live-radio.json',radio);private(b.root/'radio-module','test_driver')
    b.invoke('radio-check')
    b.invoke('interface-create');b.invoke('device-release')
    if configure:
        b.invoke('daemons-configure',None,config['CHANNEL'],config['FREQ_BAND'],'nl80211')
        b.invoke('interface-configure')
    return config,before


def manifest(b): return json.loads((b.directory/'daemons.json').read_text())
def leases(b): return b.root/'leases'/Path(b.fields['DAEMON_PIDFILE']).stem.removeprefix('create-ap-')
def ready(b): return Path(b.fields['DAEMON_PIDFILE']+'.ready')

def wait_for(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while not predicate():
        if time.monotonic()>deadline: raise AssertionError('Daemon fixture deadline exceeded')
        time.sleep(.01)


@contextlib.contextmanager
def running(b,expect_ready=True):
    process=subprocess.Popen([str(BINARY),'--test-ap-runtime',str(b.root),'daemons-run',str(b.directory)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:
        if expect_ready:
            wait_for(lambda:ready(b).exists() or process.poll() is not None)
            if process.poll() is not None: raise AssertionError(process.communicate()[1])
        yield process
    finally:
        b.invoke('stop-children')
        process.wait(5)
        process.communicate(timeout=5)


class DaemonTests(unittest.TestCase):
    def test_configuration_is_private_literal_and_owned_interface_only(self):
        with bench() as b:
            config,before=prepare(b)
            host=(b.directory/'hostapd.conf').read_text();dns=(b.directory/'dnsmasq.conf').read_text()
            self.assertIn('ssid2='+config['SSID'].encode().hex()+'\n',host)
            self.assertIn('wpa_passphrase='+config['PASSPHRASE']+'\n',host)
            self.assertIn('interface='+iface(b)+'\n',host);self.assertIn('interface='+iface(b)+'\n',dns)
            self.assertIn('bind-dynamic\n',dns);self.assertIn('no-hosts\n',dns)
            for name in ['hostapd.conf','dnsmasq.conf','daemons.json']:
                self.assertEqual((b.directory/name).stat().st_mode&0o777,0o600)
            self.assertNotIn(config['PASSPHRASE'],(b.directory/'daemons.json').read_text())
            self.assertEqual(leases(b).stat().st_mode&0o777,0o700)
            self.assertEqual((leases(b)/'dnsmasq.leases').stat().st_mode&0o777,0o600)
            b.invoke('daemons-configure',None,1,'2.4','nl80211')
            b.invoke('stop-children');finish(b)
            self.assertEqual(read(b)['interfaces'],before['interfaces'])
            self.assertFalse(leases(b).exists())

    def test_wpa_bands_and_optional_radio_flags_preserve_supported_settings(self):
        for patch,expected in [({'WPA_VERSION':'1'},'wpa=1\n'),({'WPA_VERSION':'3'},'wpa_key_mgmt=WPA-PSK SAE\n'),({'USE_PSK':'1','PASSPHRASE':'a'*64},'wpa_psk='+'a'*64+'\n'),({'FREQ_BAND':'5','CHANNEL':'36','IEEE80211N':'1','IEEE80211AC':'1','IEEE80211AX':'1'},'hw_mode=a\n')]:
            with self.subTest(patch=patch),bench() as b:
                prepare(b,patch);text=(b.directory/'hostapd.conf').read_text();self.assertIn(expected,text)
                if patch.get('WPA_VERSION')=='3':self.assertIn('ieee80211w=1\n',text)
                if patch.get('IEEE80211N')=='1':
                    for value in ['ieee80211n=1','ieee80211ac=1','ieee80211ax=1','wmm_enabled=1','ht_capab=[HT40+]']:self.assertIn(value+'\n',text)
                b.invoke('stop-children')

    def test_dns_disabled_dhcp_disabled_and_custom_resolver_render(self):
        for patch in [{'NO_DNS':'1'},{'NO_DNSMASQ':'1'},{'DHCP_DNS':'1.1.1.1,9.9.9.9','ETC_HOSTS':'1'},{'SHARE_METHOD':'none'}]:
            with self.subTest(patch=patch),bench() as b:
                prepare(b,patch);m=manifest(b)
                if patch.get('NO_DNSMASQ')=='1':
                    self.assertIsNone(m['account']);self.assertFalse((b.directory/'dnsmasq.conf').exists());self.assertFalse(leases(b).exists())
                else:
                    text=(b.directory/'dnsmasq.conf').read_text()
                    self.assertIn('option:dns-server,'+patch.get('DHCP_DNS','192.168.12.1')+'\n',text)
                    if patch.get('ETC_HOSTS')=='1': self.assertNotIn('no-hosts\n',text)
                with running(b):
                    self.assertEqual(set(manifest(b)['children']),{'hostapd'} if patch.get('NO_DNSMASQ')=='1' else {'hostapd','dnsmasq'})

    def test_child_processes_are_foreground_recorded_before_exec_and_stoppable(self):
        with bench() as b:
            config,before=prepare(b)
            with running(b):
                m=manifest(b);self.assertEqual(set(m['children']),{'hostapd','dnsmasq'})
                for name,birth in m['children'].items():
                    record=json.loads((b.directory/(name+'.identity')).read_text())
                    self.assertEqual(record['pid'],birth['pid'])
                    args=Path(f'/proc/{birth["pid"]}/cmdline').read_bytes().decode().split('\0')
                    self.assertNotIn(config['PASSPHRASE'],' '.join(args))
                    if name=='dnsmasq':self.assertIn('--keep-in-foreground',args);self.assertIn('--pid-file=',args)
                self.assertIn('192.168.12.2 Rust-client',b.invoke('daemons-leases').stdout)
                self.assertEqual(read(b)['interfaces']['demo0'],before['interfaces']['demo0'])
                b.invoke('daemons-run',ok=False)
            self.assertTrue(manifest(b)['cleaned']);self.assertFalse(leases(b).exists())

    def test_supervisor_death_recovers_pending_births_even_without_identity_files(self):
        with bench() as b:
            prepare(b)
            with running(b) as process:
                children=manifest(b)['children'];process.kill();process.wait(5)
                for name in children:(b.directory/(name+'.identity')).unlink()
                b.invoke('stop-children')
                for birth in children.values():
                    self.assertFalse(Path(f'/proc/{birth["pid"]}/cmdline').read_bytes() if Path(f'/proc/{birth["pid"]}/cmdline').exists() else b'')

    def test_failed_child_readiness_deadline_and_output_flood_keep_cleanup_evidence(self):
        for controls in [dict(fail_child='hostapd'),dict(fail_child='dnsmasq'),dict(ready=False),dict(flood_child='hostapd'),dict(exit_child='dnsmasq',exit_ms=100)]:
            with self.subTest(controls=controls),bench() as b:
                config,_=prepare(b);private(b.root/'daemon-controls.json',controls)
                with running(b,expect_ready=False) as process:
                    process.wait(5);out,error=process.communicate(timeout=5)
                    self.assertNotEqual(process.returncode,0);self.assertNotIn(config['PASSPHRASE'],out+error)
                    self.assertFalse(manifest(b)['cleaned'])
                self.assertTrue(manifest(b)['cleaned'])

    def test_closed_output_is_not_a_false_daemon_exit(self):
        with bench() as b:
            prepare(b);private(b.root/'daemon-controls.json',dict(close_output=True))
            with running(b) as process:
                time.sleep(.1);self.assertIsNone(process.poll())

    def test_changed_config_frozen_input_or_driver_plan_refuses_launch(self):
        for change in ['file','secret','channel','driver']:
            with self.subTest(change=change),bench() as b:
                prepare(b)
                if change=='file':private(b.directory/'hostapd.conf','interface=demo0\n')
                elif change=='secret':
                    path=Path(b.fields['DAEMON_PIDFILE']).with_suffix('.conf');private(path,path.read_text().replace('spaced=password#','different-password'))
                elif change=='channel':b.invoke('daemons-configure',None,36,'5','nl80211',ok=False)
                else:b.invoke('daemons-configure',None,1,'2.4','rtl871xdrv',ok=False)
                if change in ['file','secret']:b.invoke('daemons-run',ok=False)
                self.assertFalse(manifest(b)['children']);b.invoke('stop-children')

    def test_gate_without_parent_ack_or_recorded_birth_never_executes(self):
        with bench() as b:
            prepare(b)
            for ack in ['',chr(1)]:
                result=subprocess.run([str(BINARY),'--test-ap-runtime',str(b.root),'daemon-exec',str(b.directory),'hostapd'],input=ack,text=True,capture_output=True,timeout=5)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse((b.directory/'hostapd.conf.ready').exists())
            b.invoke('stop-children')

    def test_lease_symlinks_and_foreign_files_block_cleanup_without_deletion(self):
        for kind in ['symlink','foreign','replacement']:
            with self.subTest(kind=kind),bench() as b:
                prepare(b);directory=leases(b);file=directory/'dnsmasq.leases'
                if kind=='symlink':
                    file.unlink();private(b.root/'foreign','keep');file.symlink_to(b.root/'foreign')
                elif kind=='foreign':private(directory/'foreign','keep')
                else:
                    directory.rename(b.root/'original-leases');directory.mkdir(mode=0o700);private(directory/'dnsmasq.leases','keep')
                b.invoke('stop-children',ok=False)
                self.assertFalse(manifest(b)['cleaned'])
                if kind=='symlink':self.assertEqual((b.root/'foreign').read_text(),'keep');file.unlink();private(file,'')
                elif kind=='foreign':self.assertEqual((directory/'foreign').read_text(),'keep');(directory/'foreign').unlink()
                else: self.assertEqual(file.read_text(),'keep');file.unlink();directory.rmdir();(b.root/'original-leases').rename(directory)
                b.invoke('stop-children')

    def test_prior_lease_directory_is_never_adopted_after_failed_configuration(self):
        with bench() as b:
            prepare(b,configure=False);root=b.root/'leases';root.mkdir(mode=0o711);root.chmod(0o711);directory=leases(b);directory.mkdir(mode=0o700);private(directory/'foreign','keep')
            for _ in range(2):b.invoke('daemons-configure',None,1,'2.4','nl80211',ok=False)
            b.invoke('stop-children',ok=False)
            self.assertEqual((directory/'foreign').read_text(),'keep')
            directory.rename(b.root/'preserved-foreign');b.invoke('stop-children')

    def test_unrecorded_owner_or_interface_drift_cannot_publish_readiness(self):
        with bench() as b:
            prepare(b);s=read(b);name=iface(b);s['interfaces'][name]['identity']['ifindex']+=20;write(b,s)
            b.interface(name,s['interfaces'][name]['identity']['ifindex'],s['interfaces'][name]['identity']['mac'])
            b.invoke('daemons-run',ok=False);self.assertFalse(ready(b).exists())
            b.invoke('stop-children')

    def test_renderer_matches_pinned_shell_semantics_without_running_daemons(self):
        import shlex
        from collections import Counter
        source=(ROOT/'vendor/wifi-relay/daemon/create_ap').read_text()
        defaults='CHANNEL=default\n'+source.split('CHANNEL=default\n',1)[1].split('CONFIG_OPTS=(',1)[0]
        reference=source.split('# hostapd config\n',1)[1].split('# initialize WiFi interface\n',1)[0]
        for patch in [{},{'WPA_VERSION':'1'},{'WPA_VERSION':'3','HIDDEN':'1','ISOLATE_CLIENTS':'1'},{'FREQ_BAND':'5','CHANNEL':'36','IEEE80211N':'1','IEEE80211AC':'1','IEEE80211AX':'1'},{'DHCP_DNS':'1.1.1.1,9.9.9.9','ETC_HOSTS':'1','MAC_FILTER':'1'},{'USE_PSK':'1','PASSPHRASE':'A'*64}]:
            with self.subTest(patch=patch),bench() as b:
                config,_=prepare(b,patch);directory=b.root/'reference';directory.mkdir(mode=0o700)
                values=dict(config,CONFDIR=str(directory),WIFI_IFACE=iface(b),DRIVER='nl80211')
                script=b.root/'reference-render.sh'
                private(script,'umask 077\n'+defaults+'\n'+ '\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\ndnsmasq() { echo "Dnsmasq version 2.90"; }\nversion_cmp() { return 0; }\nget_mtu() { :; }\n'+reference)
                result=subprocess.run(['/bin/bash',str(script)],capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                native=(b.directory/'hostapd.conf').read_text().splitlines();legacy=(directory/'hostapd.conf').read_text().splitlines()
                native=[('ssid='+bytes.fromhex(v[6:]).decode()) if v.startswith('ssid2=') else v.replace(str(b.directory),str(directory)) for v in native]
                self.assertEqual(Counter(native),Counter(legacy))
                native=(b.directory/'dnsmasq.conf').read_text().splitlines();legacy=(directory/'dnsmasq.conf').read_text().splitlines()
                self.assertEqual(Counter(v for v in native if not v.startswith('interface=')),Counter(legacy))
                b.invoke('stop-children')

    def test_installed_dnsmasq_parser_accepts_config_without_starting_server(self):
        binary=shutil.which('dnsmasq')
        if binary is None:self.skipTest('dnsmasq syntax checker is unavailable')
        with bench() as b:
            prepare(b)
            import pwd,grp
            result=subprocess.run([binary,'--test','--keep-in-foreground','--pid-file=','--port=0','--user='+pwd.getpwuid(os.getuid()).pw_name,'--group='+grp.getgrgid(os.getgid()).gr_name,'--dhcp-leasefile='+str(leases(b)/'dnsmasq.leases'),'--conf-file='+str(b.directory/'dnsmasq.conf')],text=True,capture_output=True,timeout=5,env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin',LC_ALL='C'))
            self.assertEqual(result.returncode,0,result.stderr)
            b.invoke('stop-children')
