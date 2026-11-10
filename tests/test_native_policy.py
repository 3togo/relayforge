"""Administrator policy observations and owned cleanup; no host networking writes."""
import json
from pathlib import Path
import unittest
import test_ap_runtime as runtime
import test_ap_daemons as daemons
import test_ap_bridge as bridge
import test_ap_firewall as firewall
import test_ap_radio_plan as radio_plan
from test_ap_network import read
from test_create_ap import bench as supervised
from test_dbus import call, decode_reply

REG = 'phy#0\ncountry US: DFS-FCC\n\t(2402 - 2472 @ 40), (N/A, 20), (N/A)\n'


def policy(root, **patch):
    path = root/'policy-state.json'
    data = json.loads(path.read_text()); data.update(patch)
    temporary = root/'policy-state.new'; runtime.private(temporary, data); temporary.replace(path)


class NativePolicyTests(unittest.TestCase):
    def test_country_constraint_is_visible_without_mutating_config_or_requesting_a_hint(self):
        with supervised() as c:
            c.config.write_text(c.config.read_text()+'COUNTRY=US\n'); original=c.config.read_bytes()
            policy(c.state, regulatory=REG)
            with c.service() as (bus,_):
                caps=decode_reply('GetCapabilities',call(bus,'GetCapabilities'))
                self.assertTrue(caps['startup_preflight']['success'],caps)
                self.assertEqual(caps['native_policy']['country'],'require-existing-radio-domain')
                self.assertFalse(caps['native_policy']['regulatory_mutation'])
                self.assertFalse(caps['native_policy']['pptp_module_loading'])
                result=decode_reply('Start',call(bus,'Start'));self.assertTrue(result['success'],result)
                self.assertTrue(decode_reply('Stop',call(bus,'Stop')))
                self.assertEqual(c.config.read_bytes(),original); c.clean(self)

    def test_country_mismatch_or_ambiguous_observation_refuses_before_helper_launch(self):
        for observation in [REG.replace('US','GB'),REG.replace('phy#0','global'),REG+REG,REG.replace('phy#0','phy#1')]:
            with self.subTest(observation=observation),supervised() as c:
                c.config.write_text(c.config.read_text()+'COUNTRY=US\n')
                policy(c.state,regulatory=observation)
                with c.service() as (bus,_):
                    caps=decode_reply('GetCapabilities',call(bus,'GetCapabilities'))
                    self.assertFalse(caps['startup_preflight']['success'])
                    self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
                    self.assertIsNone(c.launched());c.clean(self)

    def test_active_firewalld_explains_backend_choice_without_launching_or_stopping_it(self):
        with supervised() as c:
            policy(c.state,firewalld_active=True); original=(c.state/'policy-state.json').read_bytes()
            with c.service() as (bus,_):
                caps=decode_reply('GetCapabilities',call(bus,'GetCapabilities'))
                self.assertFalse(caps['startup_preflight']['success'])
                self.assertIn('leave firewalld running',caps['startup_preflight']['error'])
                result=decode_reply('Start',call(bus,'Start'))
                self.assertFalse(result['success']);self.assertIn('NetworkManager',result['error'])
                self.assertIsNone(c.launched());self.assertEqual((c.state/'policy-state.json').read_bytes(),original)
                c.clean(self)

    def test_late_firewalld_appearance_invalidates_an_earlier_capability_permit(self):
        with supervised() as c,c.service() as (bus,_):
            self.assertTrue(decode_reply('GetCapabilities',call(bus,'GetCapabilities'))['startup_preflight']['success'])
            policy(c.state,firewalld_active=True)
            self.assertFalse(decode_reply('Start',call(bus,'Start'))['success'])
            self.assertIsNone(c.launched());c.clean(self)

    def test_country_is_bound_to_radio_baseline_and_never_rendered_as_a_hostapd_hint(self):
        with runtime.bench() as b:
            policy(b.root,regulatory=REG)
            _,before=daemons.prepare(b,{'COUNTRY':'US'})
            proof=json.loads((b.directory/'radio.json').read_text())['regulatory_hash']
            self.assertEqual(len(proof),64)
            host=(b.directory/'hostapd.conf').read_text()
            self.assertNotIn('country_code=',host);self.assertNotIn('ieee80211d=',host)
            self.assertFalse(any('modprobe' in c or 'reg' in c for c in read(b).get('calls',[])))
            b.invoke('stop-children');daemons.finish(b)
            self.assertEqual(read(b)['interfaces'],before['interfaces'])

    def test_regulatory_changes_before_launch_cannot_replace_the_baseline(self):
        with runtime.bench() as b:
            policy(b.root,regulatory=REG);daemons.prepare(b,{'COUNTRY':'US'})
            proof=(b.directory/'radio.json').read_bytes()
            policy(b.root,regulatory=REG.replace('20)','10)'))
            b.invoke('daemons-run',ok=False)
            self.assertEqual((b.directory/'radio.json').read_bytes(),proof)
            self.assertFalse(daemons.manifest(b)['children'])
            b.invoke('stop-children');daemons.finish(b)

    def test_country_or_rule_changes_during_running_stop_owned_daemons_and_allow_cleanup(self):
        for observation in [REG.replace('US','GB'),REG.replace('20)','10)'),REG.replace('phy#0','phy#1')]:
            with self.subTest(observation=observation),runtime.bench() as b:
                policy(b.root,regulatory=REG);_,before=daemons.prepare(b,{'COUNTRY':'US'})
                proof=(b.directory/'radio.json').read_bytes()
                with daemons.running(b) as process:
                    policy(b.root,regulatory=observation);process.wait(5)
                    self.assertNotEqual(process.returncode,0)
                self.assertTrue(daemons.manifest(b)['cleaned'])
                self.assertEqual((b.directory/'radio.json').read_bytes(),proof)
                daemons.finish(b);self.assertEqual(read(b)['interfaces'],before['interfaces'])

    def test_missing_unsafe_or_corrupt_policy_never_falls_back_to_host_observations(self):
        for change in ['missing','public','symlink','malformed','unknown']:
            with self.subTest(change=change),runtime.bench() as b:
                radio_plan.prepare(b);path=b.root/'policy-state.json';before=read(b)
                if change=='missing':path.unlink()
                elif change=='public':path.chmod(0o644)
                elif change=='symlink':path.rename(b.root/'old-policy');path.symlink_to(b.root/'old-policy')
                elif change=='malformed':runtime.private(path,'bad-json')
                else:policy(b.root,unknown=True)
                b.invoke('radio-check',ok=False)
                self.assertFalse((b.directory/'radio.json').exists());self.assertEqual(read(b),before)

    def test_firewalld_between_firewall_phases_blocks_insertions_but_not_owned_cleanup(self):
        with firewall.bench() as b:
            b.invoke('firewall-add',None,'nat');before=firewall.state(b)
            policy(b.root,firewalld_active=True)
            b.invoke('firewall-add',None,'dns',ok=False)
            after=firewall.state(b)
            self.assertEqual(after['rules'],before['rules'])
            self.assertEqual(sum('-I' in c for c in after['calls']),sum('-I' in c for c in before['calls']))
            b.invoke('firewall-remove');self.assertFalse(firewall.state(b)['rules'])

    def test_firewalld_appearance_during_running_stops_only_owned_children(self):
        with runtime.bench() as b:
            _,before=daemons.prepare(b)
            with daemons.running(b) as process:
                policy(b.root,firewalld_active=True);process.wait(5)
                self.assertNotEqual(process.returncode,0)
            daemons.finish(b);self.assertEqual(read(b)['interfaces'],before['interfaces'])

    def test_existing_bridge_without_local_firewall_resources_preserves_firewalld(self):
        with runtime.bench() as b:
            policy(b.root,firewalld_active=True)
            before=bridge.prepare(b)
            b.invoke('stop-children');b.invoke('interface-remove');b.invoke('device-restore');b.invoke('network-check')
            self.assertEqual(read(b)['interfaces'],before['interfaces'])
            self.assertTrue(json.loads((b.root/'policy-state.json').read_text())['firewalld_active'])

    def test_restart_recovery_does_not_require_current_country_or_firewall_startup_permission(self):
        with supervised() as c:
            runtime.NativeRuntimeLifecycleTests.prepare(c)
            c.config.write_text(c.config.read_text()+'COUNTRY=US\n');policy(c.state,regulatory=REG)
            with runtime.NativeRuntimeLifecycleTests.service(c) as (bus,process):
                result=decode_reply('Start',call(bus,'Start'));self.assertTrue(result['success'],result)
                identity=json.loads(next((c.state/'engine').glob('*/hostapd.identity')).read_text())
                process.kill();process.wait(5)
            policy(c.state,regulatory=REG.replace('US','GB'),firewalld_active=True)
            with runtime.NativeRuntimeLifecycleTests.service(c) as (bus,_):
                self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
                self.assertFalse((c.state/'create-ap.json').exists())
                self.assertFalse(list((c.state/'engine').glob('create_ap.demo0.conf.*')))
                self.assertFalse(runtime.reference.same_process(identity))
            state=json.loads((c.state/'network-state.json').read_text())
            self.assertEqual(set(state['interfaces']),{'demo0'})
            self.assertEqual(state['global'],0)
            self.assertEqual(len(json.loads((c.state/'firewall-state.json').read_text())['rules']),1)


if __name__ == '__main__':
    unittest.main()
