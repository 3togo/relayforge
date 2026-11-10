"""Startup radio checks use private captures; never invoke a physical adapter."""
import json
from pathlib import Path
import subprocess
import unittest
from test_ap_runtime import bench, private
from test_ap_devices import prepare as devices
from test_ap_daemons import prepare as daemons
from test_ap_network import read
from test_radio import capture


def prepare(b, physical=False):
    devices(b)
    config=dict(WIFI_IFACE='demo0',INTERNET_IFACE='demo0',SSID='Private AP',PASSPHRASE='private-radio-password',FREQ_BAND='2.4',CHANNEL='1')
    radio=capture()
    if physical:
        config.update(NO_VIRT='1',INTERNET_IFACE='eth0')
        b.fields.update(NO_VIRT='1',INTERNET_IFACE='eth0')
        b.interface('eth0',9,'02:00:00:00:00:09')
        b.save()
    else:
        radio['link']='Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Private upstream\n\tfreq: 2412\n'
    frozen=Path(b.fields['DAEMON_PIDFILE']).with_suffix('.conf')
    private(frozen,'\n'.join(k+'='+v for k,v in config.items())+'\n')
    private(b.root/'live-radio.json',radio);private(b.root/'radio-module','test_driver')
    return frozen,radio


class RadioPlanTests(unittest.TestCase):
    def test_nl80211_driver_profiles_keep_explicit_security_and_ownership(self):
        for module in ['ath9k','mt7921e','iwlwifi','rtw88_8723de','rtl8192ce','rtl8xxxu','rtl_test']:
            with self.subTest(module=module),bench() as b:
                frozen,_=prepare(b);private(b.root/'radio-module',module)
                before=read(b);original=frozen.read_bytes()
                b.invoke('radio-check');b.invoke('radio-check')
                self.assertEqual(json.loads((b.directory/'radio.json').read_text())['module'],module)
                self.assertEqual(read(b),before);self.assertEqual(frozen.read_bytes(),original)

    def test_wpa3_raw_psk_rejected_before_radio_baseline_or_child_creation(self):
        with bench() as b:
            frozen,_=prepare(b);private(frozen,frozen.read_text()+'WPA_VERSION=3\nUSE_PSK=1\nPASSPHRASE='+'c'*64+'\n')
            before=read(b);original=frozen.read_bytes()
            result=b.invoke('radio-check',ok=False)
            self.assertIn('passphrase for SAE',result.stderr)
            self.assertEqual(read(b),before);self.assertEqual(frozen.read_bytes(),original)
            self.assertFalse((b.directory/'radio.json').exists())

    def test_baseline_is_private_idempotent_and_does_not_mutate_network(self):
        with bench() as b:
            _,radio=prepare(b);before=read(b);snapshot=b.snapshot.read_bytes()
            b.invoke('radio-check');path=b.directory/'radio.json';baseline=path.read_bytes()
            b.invoke('radio-check')
            self.assertEqual(path.read_bytes(),baseline)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(read(b),before);self.assertEqual(b.snapshot.read_bytes(),snapshot)
            for secret in ['private-radio-password','Private AP','Private upstream','aa:bb:cc:dd:ee:ff']:
                self.assertNotIn(secret,baseline.decode())
            self.assertEqual(json.loads(baseline)['phy'],0)

    def test_restricted_channels_unknown_output_and_missing_concurrency_fail_before_intent(self):
        for change in ['disabled','no IR','radar detection','unknown restriction','concurrency','identity','mode','link']:
            with self.subTest(change=change),bench() as b:
                _,radio=prepare(b);before=read(b)
                if change=='concurrency':radio['phy']=radio['phy'].replace('total <= 3','total <= 1')
                elif change=='identity':radio['info']=radio['info'].replace('wiphy 0','wiphy 1')
                elif change=='mode':radio['info']=radio['info'].replace('type managed','type monitor')
                elif change=='link':radio['link']='unrecognized\n'
                else:radio['phy']=radio['phy'].replace('2412.0 MHz [1] (20.0 dBm)',f'2412.0 MHz [1] (20.0 dBm) ({change})')
                private(b.root/'live-radio.json',radio);result=b.invoke('radio-check',ok=False)
                self.assertNotIn('private-radio-password',result.stderr)
                self.assertFalse((b.directory/'radio.json').exists());self.assertEqual(read(b),before)

    def test_driver_restrictions_decline_without_rewriting_configuration(self):
        for module in ['brcmfmac','8192cu','8192de','8723au','../driver','bad\nmodule']:
            with self.subTest(module=module),bench() as b:
                frozen,_=prepare(b);original=frozen.read_bytes();before=read(b)
                private(b.root/'radio-module',module);b.invoke('radio-check',ok=False)
                self.assertEqual(frozen.read_bytes(),original);self.assertEqual(read(b),before)
        with bench() as b:
            frozen,_=prepare(b);private(frozen,frozen.read_text()+'WPA_VERSION=1\n')
            private(b.root/'radio-module','rtl8192ce');b.invoke('radio-check',ok=False)
            self.assertTrue(frozen.read_text().endswith('WPA_VERSION=1\n'))
        with bench() as b:
            prepare(b,physical=True);private(b.root/'radio-module','brcmfmac')
            b.invoke('radio-check')

    def test_roaming_phy_driver_parent_and_frozen_changes_cannot_replace_baseline(self):
        for change in ['roam','phy','driver','parent','config','channel']:
            with self.subTest(change=change),bench() as b:
                frozen,radio=prepare(b);b.invoke('radio-check');before=(b.directory/'radio.json').read_bytes()
                if change=='roam':radio['link']=radio['link'].replace('aa:bb:cc:dd:ee:ff','aa:bb:cc:dd:ee:01')
                elif change=='phy':
                    radio['info']=radio['info'].replace('wiphy 0','wiphy 1');radio['phy']=radio['phy'].replace('phy0','phy1')
                elif change=='driver':private(b.root/'radio-module','other_driver')
                elif change=='parent':(b.root/'net/demo0/ifindex').write_text('42\n')
                elif change=='config':private(frozen,frozen.read_text().replace('Private AP','Changed AP'))
                else:radio['link']=radio['link'].replace('2412','2437')
                private(b.root/'live-radio.json',radio);b.invoke('radio-check',ok=False)
                self.assertEqual((b.directory/'radio.json').read_bytes(),before)

    def test_missing_unsafe_or_malformed_baseline_blocks_daemon_configuration(self):
        for change in ['missing','symlink','malformed','public']:
            with self.subTest(change=change),bench() as b:
                daemons(b,configure=False);path=b.directory/'radio.json'
                if change=='missing':path.unlink()
                elif change=='symlink':path.rename(b.root/'original-radio');path.symlink_to(b.root/'original-radio')
                elif change=='malformed':private(path,'{}')
                else:path.chmod(0o644)
                before=read(b);b.invoke('daemons-configure',None,1,'2.4','nl80211',ok=False)
                self.assertFalse((b.directory/'hostapd.conf').exists());self.assertFalse((b.directory/'daemons.json').exists())
                self.assertEqual(read(b),before)

    def test_late_roam_blocks_launch_before_any_daemon_birth(self):
        with bench() as b:
            daemons(b);radio=json.loads((b.root/'live-radio.json').read_text())
            radio['link']=radio['link'].replace('aa:bb:cc:dd:ee:ff','aa:bb:cc:dd:ee:01')
            private(b.root/'live-radio.json',radio);before=read(b)
            b.invoke('daemons-run',ok=False)
            self.assertFalse(json.loads((b.directory/'daemons.json').read_text())['children'])
            self.assertEqual(read(b),before);b.invoke('stop-children')

    def test_dead_owner_and_unsafe_capture_are_not_adopted(self):
        with bench() as b:
            prepare(b);(b.root/'live-radio.json').chmod(0o644);b.invoke('radio-check',ok=False)
            (b.root/'live-radio.json').chmod(0o600)
            with subprocess.Popen(['/bin/sleep','30']) as owner:
                # A dead identity is validated without signalling any bare PID.
                b.snapshot.unlink();b.save(pid=owner.pid);owner.terminate();owner.wait(3)
            b.invoke('radio-check',ok=False)
            self.assertFalse((b.directory/'radio.json').exists())
