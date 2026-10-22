"""Rust-owned network transactions against a private simulated kernel only."""
import json
import fcntl
import shutil
import unittest
from test_ap_runtime import bench, private


def device(index, mac, kind='managed', up=True, connected=False, addresses=None):
    return dict(identity=dict(ifindex=index,mac=mac),kind=kind,up=up,connected=connected,addresses=addresses or [])


def prepare(b, physical=False, global_value=0):
    b.fields.update(WIFI_IFACE='aptest0',VWIFI_IFACE='' if physical else 'aptest0',
                    NO_VIRT='1' if physical else '0',PHYSICAL_CONFIGURED='1' if physical else '0',
                    NEW_MACADDR='' if physical else '02:00:00:00:00:08')
    b.interface('aptest0',8,'02:00:00:00:00:08')
    b.save()
    state=dict(interfaces={
        'demo0':device(7,'02:00:00:00:00:07',connected=True,addresses=['192.0.2.10/24']),
        'aptest0':device(8,'02:00:00:00:00:08',addresses=['fe80::8/64'])},
        global_value=global_value)
    state['global']=state.pop('global_value')
    state['forwarding']=dict(all=global_value,default=0,demo0=0,aptest0=1)
    private(b.root/'network-state.json',state)
    return state


def read(b):
    return json.loads((b.root/'network-state.json').read_text())


def write(b,state):
    private(b.root/'network-state.json',state)


class NetworkTransactions(unittest.TestCase):
    def test_virtual_setup_and_cleanup_preserve_upstream(self):
        with bench() as b:
            before=prepare(b)
            b.invoke('interface-configure')
            s=read(b)
            self.assertEqual(s['interfaces']['demo0'],before['interfaces']['demo0'])
            self.assertEqual(s['interfaces']['aptest0']['addresses'],['fe80::8/64','192.168.12.1/24'])
            self.assertEqual([v[0] for v in s['calls']],['ip','iw','ip'])
            self.assertNotIn('flush',str(s['calls']))
            b.invoke('interface-configure')
            self.assertEqual(read(b)['calls'],s['calls'])
            self.assertEqual((b.directory/'interface.json').stat().st_mode&0o777,0o600)
            b.invoke('network-check',ok=False)
            b.invoke('interface-remove')
            b.invoke('interface-remove')
            b.invoke('network-check')
            self.assertTrue(json.loads((b.directory/'interface.json').read_text())['restored'])
            self.assertNotIn('aptest0',read(b)['interfaces'])
            self.assertEqual(read(b)['interfaces']['demo0'],before['interfaces']['demo0'])

    def test_physical_restoration_and_new_station_connection(self):
        with bench() as b:
            before=prepare(b,physical=True)
            b.invoke('interface-configure')
            b.invoke('interface-remove')
            self.assertEqual(read(b)['interfaces'],before['interfaces'])
            s=read(b);s['interfaces']['aptest0'].update(connected=True,addresses=['198.51.100.4/24']);write(b,s)
            b.invoke('interface-remove')
            b.invoke('network-check')
            self.assertEqual(read(b),s)

    def test_connected_or_configured_ap_is_rejected_before_mutation(self):
        for changes in [dict(connected=True),dict(addresses=['198.51.100.3/24']),dict(kind='monitor')]:
            with self.subTest(changes=changes),bench() as b:
                s=prepare(b);s['interfaces']['aptest0'].update(changes);write(b,s)
                b.invoke('interface-configure',ok=False)
                self.assertEqual(read(b),s)
                self.assertFalse((b.directory/'interface.json').exists())

    def test_upstream_cannot_be_configured(self):
        with bench() as b:
            s=prepare(b);b.fields.update(WIFI_IFACE='demo0',VWIFI_IFACE='demo0');b.save()
            b.invoke('interface-configure',ok=False)
            self.assertEqual(read(b),s)

    def test_setup_failure_and_lost_reply_at_every_mutation_are_recoverable(self):
        for fault in ['fail_at','lose_at']:
            for index in range(1,4):
                with self.subTest(fault=fault,index=index),bench() as b:
                    prepare(b);s=read(b);s[fault]=index;write(b,s)
                    b.invoke('interface-configure',ok=False)
                    self.assertTrue((b.directory/'interface.json').exists())
                    b.invoke('interface-remove')
                    b.invoke('network-check')
                    self.assertNotIn('aptest0',read(b)['interfaces'])

    def test_physical_restore_failure_at_every_mutation_can_retry(self):
        for fault in ['fail_at','lose_at']:
            for offset in range(1,4):
                with self.subTest(fault=fault,offset=offset),bench() as b:
                    before=prepare(b,physical=True);b.invoke('interface-configure')
                    s=read(b);s[fault]=s['mutations']+offset;write(b,s)
                    b.invoke('interface-remove',ok=False)
                    b.invoke('interface-remove')
                    b.invoke('network-check')
                    self.assertEqual(read(b)['interfaces'],before['interfaces'])

    def test_changed_identity_or_foreign_address_blocks_cleanup(self):
        for change in ['index','mac','address','connected']:
            with self.subTest(change=change),bench() as b:
                prepare(b);b.invoke('interface-configure');s=read(b);dev=s['interfaces']['aptest0']
                if change=='index': dev['identity']['ifindex']=99
                elif change=='mac': dev['identity']['mac']='02:00:00:00:00:99'
                elif change=='address': dev['addresses'].append('198.51.100.3/24')
                else: dev['connected']=True
                write(b,s)
                b.interface('aptest0',dev['identity']['ifindex'],dev['identity']['mac'])
                b.invoke('interface-remove',ok=False)
                self.assertEqual(read(b),s)

    def test_virtual_delete_lost_reply_can_retry(self):
        with bench() as b:
            prepare(b);b.invoke('interface-configure');s=read(b);s['lose_at']=s['mutations']+1;write(b,s)
            b.invoke('interface-remove',ok=False)
            b.invoke('interface-remove');b.invoke('network-check')

    def test_early_start_cleanup_never_deletes_parent(self):
        with bench() as b:
            s=prepare(b);b.fields['WIFI_IFACE']='demo0';b.save()
            s['interfaces'].pop('aptest0');s['forwarding'].pop('aptest0');write(b,s)
            shutil.rmtree(b.root/'net/aptest0')
            b.invoke('interface-remove')
            self.assertEqual(read(b),s)

    def test_missing_physical_journal_is_not_guessed(self):
        with bench() as b:
            s=prepare(b,physical=True)
            b.invoke('interface-remove',ok=False)
            b.invoke('network-check',ok=False)
            self.assertEqual(read(b),s)

    def test_forwarding_restores_mixed_baselines_in_global_first_order(self):
        for global_value in [0,1]:
            with self.subTest(global_value=global_value),bench() as b:
                before=prepare(b,global_value=global_value)
                b.invoke('forwarding-enable');b.invoke('network-check',ok=False)
                self.assertEqual(read(b)['global'],1)
                self.assertEqual(read(b)['forwarding']['demo0'],1)
                if global_value==0:
                    self.assertEqual(read(b)['calls'][0],['sysctl','global','1'])
                b.invoke('forwarding-release');b.invoke('forwarding-release');b.invoke('network-check')
                after=read(b)
                self.assertEqual(after['forwarding'],before['forwarding'])
                self.assertEqual(after['global'],before['global'])
                self.assertFalse((b.engine/'forwarding.json').exists())
                self.assertEqual(after['interfaces'],before['interfaces'])

    def test_forwarding_faults_remain_retryable(self):
        for fault in ['fail_at','lose_at']:
            for operation in ['enable','release']:
                for offset in [1,2]:
                    with self.subTest(fault=fault,operation=operation,offset=offset),bench() as b:
                        before=prepare(b,global_value=1 if operation=='enable' else 0)
                        if operation=='release': b.invoke('forwarding-enable')
                        s=read(b);s[fault]=s.get('mutations',0)+offset;write(b,s)
                        # With global already enabled, enable has only one write.
                        if operation=='enable' and offset==2: continue
                        b.invoke('forwarding-'+operation,ok=False)
                        self.assertTrue((b.engine/'forwarding.json').exists())
                        b.invoke('forwarding-release');b.invoke('network-check')
                        self.assertEqual(read(b)['global'],before['global'])
                        self.assertEqual(read(b)['forwarding'],before['forwarding'])

    def test_global_enable_failure_and_lost_reply_restore(self):
        for fault in ['fail_at','lose_at']:
            with self.subTest(fault=fault),bench() as b:
                before=prepare(b);s=read(b);s[fault]=1;write(b,s)
                b.invoke('forwarding-enable',ok=False)
                b.invoke('forwarding-release')
                self.assertEqual(read(b)['forwarding'],before['forwarding'])
                self.assertEqual(read(b)['global'],0)

    def test_shared_forwarding_is_restored_only_after_last_owner(self):
        with bench() as b:
            before=prepare(b);b.invoke('forwarding-enable')
            first=b.directory
            b.directory=b.engine/'create_ap.demo0.conf.IJKLMNOP';b.directory.mkdir(mode=0o700)
            b.snapshot=b.directory/'recovery.json';b.fields.update(CONFDIR=str(b.directory),RULE_COMMENT='wifi-relay-'+b.directory.name)
            b.save();b.invoke('forwarding-enable')
            b.invoke('forwarding-release',first)
            self.assertEqual(read(b)['global'],1)
            self.assertEqual(read(b)['forwarding']['demo0'],1)
            # Completed participants would normally be removed by the engine.
            shutil.rmtree(first)
            b.invoke('forwarding-release')
            self.assertEqual(read(b)['forwarding'],before['forwarding'])

    def test_external_forwarding_identity_change_blocks_restoration(self):
        with bench() as b:
            prepare(b);b.invoke('forwarding-enable');s=read(b)
            s['interfaces']['demo0']['identity']['ifindex']=44;write(b,s)
            b.interface('demo0',44,'02:00:00:00:00:07')
            b.invoke('forwarding-release',ok=False)
            self.assertEqual(read(b),s)
            self.assertTrue((b.engine/'forwarding.json').exists())

    def test_foreign_and_legacy_shared_state_block_forwarding(self):
        for legacy in [True,False]:
            with self.subTest(legacy=legacy),bench() as b:
                before=prepare(b)
                if legacy: private(b.common/'ip_forward','0\n')
                else: (b.engine/'create_ap.other0.conf.IJKLMNOP').mkdir(mode=0o700)
                b.invoke('forwarding-enable',ok=False)
                self.assertEqual(read(b),before)

    def test_journal_symlink_or_malformed_state_cannot_mutate(self):
        for name,action in [('interface.json','interface-configure'),('forwarding.json','forwarding-enable')]:
            for symlink in [False,True]:
                with self.subTest(name=name,symlink=symlink),bench() as b:
                    before=prepare(b);path=(b.directory if name=='interface.json' else b.engine)/name
                    if symlink:
                        private(b.root/'foreign','{}');path.symlink_to(b.root/'foreign')
                    else: private(path,dict(version=999))
                    b.invoke(action,ok=False)
                    self.assertEqual(read(b),before)

    def test_fixture_missing_state_never_uses_live_kernel(self):
        with bench() as b:
            prepare(b);(b.root/'network-state.json').unlink()
            b.invoke('interface-configure',ok=False)
            b.invoke('forwarding-enable',ok=False)

    def test_network_locks_exclude_orphan_transactions(self):
        for action,path,name in [('interface-configure','instance','.network.lock'),('forwarding-enable','engine','.forwarding.lock')]:
            with self.subTest(action=action),bench() as b:
                before=prepare(b)
                lock=(b.directory if path=='instance' else b.engine)/name
                private(lock,'')
                with lock.open('r+') as handle:
                    fcntl.flock(handle,fcntl.LOCK_EX)
                    result=b.invoke(action,ok=False)
                    self.assertIn('still running',result.stderr)
                self.assertEqual(read(b),before)

    def test_two_uplinks_preserve_other_owner_and_restore_original_flags(self):
        with bench() as b:
            before=prepare(b)
            before['interfaces']['eth0']=device(9,'02:00:00:00:00:09',kind='ethernet')
            before['forwarding']['eth0']=0
            write(b,before);b.interface('eth0',9,'02:00:00:00:00:09')
            b.invoke('forwarding-enable');first=b.directory
            b.directory=b.engine/'create_ap.demo0.conf.IJKLMNOP';b.directory.mkdir(mode=0o700)
            b.snapshot=b.directory/'recovery.json'
            b.fields.update(CONFDIR=str(b.directory),RULE_COMMENT='wifi-relay-'+b.directory.name,INTERNET_IFACE='eth0')
            b.save();b.invoke('forwarding-enable');b.invoke('forwarding-release',first)
            self.assertEqual(read(b)['forwarding']['demo0'],0)
            self.assertEqual(read(b)['forwarding']['eth0'],1)
            shutil.rmtree(first);b.invoke('forwarding-release')
            self.assertEqual(read(b)['forwarding'],before['forwarding'])

    def test_active_forwarding_change_is_not_silently_reenabled(self):
        with bench() as b:
            prepare(b);b.invoke('forwarding-enable');s=read(b)
            s['forwarding']['demo0']=0;write(b,s)
            b.invoke('forwarding-enable',ok=False)
            self.assertEqual(read(b),s)

    def test_enable_requires_live_owner_but_cleanup_survives_owner_exit(self):
        with bench() as b:
            before=prepare(b)
            b.snapshot.unlink()
            with b.child() as child:
                b.save(pid=child.pid);b.invoke('forwarding-enable')
                child.terminate();child.wait(3)
                b.invoke('forwarding-enable',ok=False)
                b.invoke('interface-configure',ok=False)
                b.invoke('forwarding-release')
                self.assertEqual(read(b)['forwarding'],before['forwarding'])
