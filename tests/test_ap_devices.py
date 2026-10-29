"""Native create_ap allocation and NM ownership with a simulated kernel only."""
import contextlib
import json
import os
from pathlib import Path
import select
import subprocess
import unittest
from test_ap_runtime import bench, private
from test_ap_network import device, read, write
from test_dbus import ROOT


def prepare(b,physical=False,nm=True):
    if physical:
        b.fields.update(WIFI_IFACE='dedicated0',NO_VIRT='1')
        b.interface('dedicated0',8,'02:00:00:00:00:08')
    b.save()
    interfaces={'demo0':device(7,'02:00:00:00:00:07',connected=True,addresses=['192.0.2.10/24'])}
    if physical: interfaces['dedicated0']=device(8,'02:00:00:00:00:08')
    state=dict(interfaces=interfaces,phys={name:0 for name in interfaces},
               **{'global':0},forwarding={'all':0,'default':0,**{name:0 for name in interfaces}},
               nm=dict(owner=':1.999',devices={name:dict(path=f'/org/freedesktop/NetworkManager/Devices/{v["identity"]["ifindex"]}',managed=True,state=100 if name=='demo0' else 30,active=name=='demo0') for name,v in interfaces.items()}) if nm else None)
    write(b,state)
    return state


def snapshot(b): return json.loads(b.snapshot.read_text())
def iface(b): return snapshot(b)['fields']['WIFI_IFACE']
def journal(b): return json.loads((b.directory/'device.json').read_text())

def finish(b):
    b.invoke('interface-remove');b.invoke('device-restore');b.invoke('network-check')


@contextlib.contextmanager
def manager(b,**control):
    private(b.root/'nm-control.json',control)
    with subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/fake_ap_devices.py'),str(b.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as fake:
        try:
            ready,_,_=select.select([fake.stdout],[],[],5)
            if not ready or fake.stdout.readline()!=b'ready\n': raise AssertionError('Fake NM did not become ready')
            yield fake
        finally:
            fake.terminate();_,errors=fake.communicate(timeout=5)
            if b'Traceback' in errors: raise AssertionError(errors.decode())


class DeviceOwnershipTests(unittest.TestCase):
    def test_new_virtual_device_is_explicitly_released_during_nm_discovery(self):
        with bench() as b:
            before=prepare(b);s=read(b);s['nm_bus']=True;write(b,s)
            with manager(b,initializing_virtual=True):
                b.invoke('interface-create');name=iface(b)
                b.invoke('device-release')
                b.invoke('interface-configure')
                self.assertFalse(read(b)['nm']['devices'][name]['managed'])
                finish(b)
                self.assertEqual(read(b)['interfaces'],before['interfaces'])
                self.assertEqual(read(b)['nm']['devices']['demo0'],before['nm']['devices']['demo0'])

    def test_creation_journal_snapshot_and_structured_command_preserve_parent(self):
        with bench() as b:
            before=prepare(b);result=b.invoke('interface-create');name=iface(b);s=read(b)
            self.assertTrue(name.startswith('rfap'));self.assertLessEqual(len(name),15)
            self.assertIn('WIFI_IFACE=',result.stdout)
            self.assertEqual(s['interfaces']['demo0'],before['interfaces']['demo0'])
            self.assertEqual(s['calls'][0],['iw','dev','demo0','interface','add',name,'type','__ap','addr',journal(b)['mac']])
            self.assertTrue(journal(b)['mac'].startswith('02:'))
            self.assertEqual(journal(b)['created'],s['interfaces'][name]['identity'])
            self.assertEqual(snapshot(b)['interfaces'][name],journal(b)['created'])
            self.assertEqual((b.directory/'device.json').stat().st_mode&0o777,0o600)
            b.invoke('interface-create');self.assertEqual(read(b)['calls'],s['calls'])
            b.invoke('interface-configure',ok=False)
            b.invoke('device-release');b.invoke('interface-configure')
            b.invoke('device-restore',ok=False)
            finish(b)
            self.assertEqual(read(b)['interfaces'],before['interfaces'])
            self.assertTrue(journal(b)['restored'])
            self.assertFalse([v for v in read(b)['calls'] if v[:2]==['NetworkManager','demo0']])

    def test_creation_failure_or_lost_reply_is_reconciled_without_leaks(self):
        for fault in ['fail_at','lose_at']:
            with self.subTest(fault=fault),bench() as b:
                before=prepare(b);s=read(b);s[fault]=1;write(b,s)
                b.invoke('interface-create',ok=False)
                self.assertTrue((b.directory/'device.json').exists())
                b.invoke('device-fields')
                finish(b)
                self.assertEqual(read(b)['interfaces'],before['interfaces'])
                self.assertTrue(journal(b)['restored'])

    def test_release_failure_or_lost_reply_preserves_baseline_for_stop(self):
        for fault in ['fail_at','lose_at']:
            with self.subTest(fault=fault),bench() as b:
                before=prepare(b,physical=True);b.invoke('interface-create');s=read(b);s[fault]=1;write(b,s)
                b.invoke('device-release',ok=False)
                self.assertTrue(journal(b)['nm']['baseline'])
                b.invoke('device-restore')
                self.assertTrue(read(b)['nm']['devices']['dedicated0']['managed'])
                self.assertEqual(read(b)['interfaces'],before['interfaces'])

    def test_dedicated_adapter_restores_mode_and_managed_state(self):
        for managed in [False,True]:
            with self.subTest(managed=managed),bench() as b:
                before=prepare(b,physical=True);before['nm']['devices']['dedicated0'].update(managed=managed,state=30 if managed else 10);write(b,before)
                b.invoke('interface-create');b.invoke('device-release');b.invoke('device-release')
                self.assertFalse(read(b)['nm']['devices']['dedicated0']['managed'])
                # The adapter saves this flag before configuring a dedicated interface.
                data=snapshot(b);data['fields']['PHYSICAL_CONFIGURED']='1';private(b.snapshot,data)
                b.invoke('interface-configure');b.invoke('device-restore',ok=False)
                b.invoke('interface-remove');b.invoke('device-restore');b.invoke('device-restore');b.invoke('network-check')
                self.assertEqual(read(b)['interfaces'],before['interfaces'])
                self.assertEqual(read(b)['nm']['devices']['dedicated0']['managed'],managed)
                sets=[v for v in read(b).get('calls',[]) if v[0]=='NetworkManager']
                self.assertEqual(len(sets),2 if managed else 0)

    def test_restore_failure_and_lost_reply_allow_retry_without_new_disconnect(self):
        for fault in ['fail_at','lose_at']:
            with self.subTest(fault=fault),bench() as b:
                prepare(b,physical=True);b.invoke('interface-create');b.invoke('device-release')
                s=read(b);s[fault]=s['mutations']+1;write(b,s)
                b.invoke('device-restore',ok=False)
                s=read(b)
                if s['nm']['devices']['dedicated0']['managed']:
                    s['nm']['devices']['dedicated0'].update(active=True,state=100)
                    s['interfaces']['dedicated0']['connected']=True
                    write(b,s)
                b.invoke('device-restore');b.invoke('network-check')
                self.assertTrue(read(b)['nm']['devices']['dedicated0']['managed'])

    def test_upstream_or_other_active_device_cannot_be_unmanaged(self):
        for physical in [False,True]:
            with self.subTest(physical=physical),bench() as b:
                prepare(b,physical=physical);b.invoke('interface-create');name=iface(b);s=read(b)
                if physical: s['interfaces'][name]['connected']=True
                else: s['nm']['devices'][name].update(active=True,state=100)
                write(b,s);b.invoke('device-release',ok=False)
                self.assertEqual(read(b),s)
                self.assertIsNone(journal(b)['nm'])

    def test_parent_and_child_identity_changes_block_mutation(self):
        for change in ['parent','index','mac','phy']:
            with self.subTest(change=change),bench() as b:
                prepare(b);b.invoke('interface-create');name=iface(b);s=read(b)
                if change=='parent':
                    s['interfaces']['demo0']['identity']['ifindex']=88;b.interface('demo0',88,'02:00:00:00:00:07')
                    write(b,s);b.invoke('interface-create',ok=False)
                else:
                    if change=='index': s['interfaces'][name]['identity']['ifindex']=88
                    elif change=='mac': s['interfaces'][name]['identity']['mac']='02:00:00:00:00:88'
                    else: s['phys'][name]=9
                    b.interface(name,s['interfaces'][name]['identity']['ifindex'],s['interfaces'][name]['identity']['mac'])
                    write(b,s);b.invoke('device-release',ok=False);b.invoke('interface-remove',ok=False)
                self.assertEqual(read(b),s)

    def test_networkmanager_restart_and_device_path_reuse_block_physical_restore(self):
        for change in ['owner','path']:
            with self.subTest(change=change),bench() as b:
                prepare(b,physical=True);b.invoke('interface-create');b.invoke('device-release');s=read(b)
                if change=='owner': s['nm']['owner']=':1.1000'
                else: s['nm']['devices']['dedicated0']['path']+='_reused'
                write(b,s);b.invoke('device-restore',ok=False)
                self.assertEqual(read(b),s)
                self.assertFalse(journal(b)['restored'])

    def test_missing_networkmanager_device_is_not_assumed_unmanaged(self):
        with bench() as b:
            prepare(b);b.invoke('interface-create');s=read(b);s['nm']['devices'].pop(iface(b));write(b,s)
            b.invoke('device-release',ok=False)
            self.assertEqual(read(b),s)
            finish(b)

    def test_without_networkmanager_creation_and_cleanup_still_work(self):
        with bench() as b:
            before=prepare(b,nm=False);b.invoke('interface-create');b.invoke('device-release')
            b.invoke('interface-configure');finish(b)
            self.assertEqual(read(b)['interfaces'],before['interfaces'])

    def test_dead_owner_cannot_create_or_release_but_can_be_cleaned(self):
        with bench() as b:
            before=prepare(b);b.snapshot.unlink()
            with b.child() as child:
                b.save(pid=child.pid);b.invoke('interface-create');child.terminate();child.wait(3)
                b.invoke('interface-create',ok=False);b.invoke('device-release',ok=False)
                finish(b)
                self.assertEqual(read(b)['interfaces'],before['interfaces'])

    def test_unsafe_or_changed_journal_cannot_control_devices(self):
        for kind in ['symlink','unknown','marker']:
            with self.subTest(kind=kind),bench() as b:
                prepare(b);b.invoke('interface-create');s=read(b);path=b.directory/'device.json'
                if kind=='symlink': path.rename(b.root/'foreign');path.symlink_to(b.root/'foreign')
                else:
                    data=journal(b);data['unexpected' if kind=='unknown' else 'marker']='foreign';private(path,data)
                b.invoke('device-release',ok=False);b.invoke('device-restore',ok=False)
                self.assertEqual(read(b),s)

    def test_private_bus_uses_exact_managed_property_and_restores_only_dedicated(self):
        with bench() as b:
            prepare(b,physical=True);s=read(b);s['nm_bus']=True;write(b,s)
            with manager(b):
                b.invoke('interface-create');b.invoke('device-release');b.invoke('device-restore')
                self.assertTrue(read(b)['nm']['devices']['dedicated0']['managed'])
                calls=[v for v in read(b)['calls'] if v[0]=='D-Bus']
                self.assertEqual(calls,[['D-Bus','dedicated0','Managed','false'],['D-Bus','dedicated0','Managed','true']])
                self.assertTrue(journal(b)['nm']['owner'].startswith(':1.'))
                self.assertTrue(read(b)['nm']['devices']['demo0']['managed'])

    def test_private_bus_failed_query_and_denied_set_keep_intent_without_guessing(self):
        for option in ['fail_query','deny_set','lose_set','hang_query']:
            with self.subTest(option=option),bench() as b:
                prepare(b,physical=True);s=read(b);s['nm_bus']=True;write(b,s)
                with manager(b,**{option:True}):
                    b.invoke('interface-create');b.invoke('device-release',ok=False)
                    private(b.root/'nm-control.json',{})
                    b.invoke('device-restore')
                    self.assertTrue(read(b)['nm']['devices']['dedicated0']['managed'])

    def test_private_bus_absent_owner_is_distinguished_from_failed_query(self):
        with bench() as b:
            prepare(b,nm=False);s=read(b);s['nm_bus']=True;write(b,s)
            b.invoke('interface-create');b.invoke('device-release');finish(b)

    def test_networkmanager_reclaim_blocks_configuration_and_removal(self):
        for state in [0,40,100]:
            with self.subTest(state=state),bench() as b:
                prepare(b);b.invoke('interface-create');b.invoke('device-release');s=read(b)
                s['nm']['devices'][iface(b)].update(managed=True,state=state);write(b,s)
                b.invoke('interface-configure',ok=False);b.invoke('interface-remove',ok=False)
                self.assertEqual(read(b),s)

    def test_private_bus_virtual_creation_release_and_deletion_need_no_parent_writes(self):
        with bench() as b:
            before=prepare(b);s=read(b);s['nm_bus']=True;write(b,s)
            with manager(b):
                b.invoke('interface-create');name=iface(b);b.invoke('device-release')
                b.invoke('interface-configure');finish(b)
                self.assertEqual(read(b)['interfaces'],before['interfaces'])
                self.assertEqual([v for v in read(b)['calls'] if v[0]=='D-Bus'],[['D-Bus',name,'Managed','false']])

    def test_private_bus_owner_restart_does_not_restore_through_new_owner(self):
        with bench() as b:
            prepare(b,physical=True);s=read(b);s['nm_bus']=True;write(b,s)
            with manager(b):
                b.invoke('interface-create');b.invoke('device-release')
            with manager(b):
                s=read(b);b.invoke('device-restore',ok=False)
                self.assertEqual(read(b),s)
                self.assertFalse(journal(b)['restored'])

    def test_allocation_lock_and_historical_state_prevent_new_mutations(self):
        import fcntl
        with bench() as b:
            before=prepare(b);lock=b.engine/'.allocation.lock';private(lock,'')
            with lock.open('r+') as handle:
                fcntl.flock(handle,fcntl.LOCK_EX);b.invoke('interface-create',ok=False)
            self.assertEqual(read(b),before)
            b.fields['ADDED_UNMANAGED']=' demo0 ';b.save()
            b.invoke('interface-create',ok=False);b.invoke('device-restore',ok=False);b.invoke('network-check',ok=False)
            self.assertEqual(read(b),before)

    def test_generated_creation_adapter_keeps_shell_and_rust_snapshot_in_sync(self):
        import shlex
        from test_ap_runtime import staging,BINARY
        from test_ap_radio_plan import prepare as radio_prepare
        with bench() as b:
            radio_prepare(b);before=read(b);b.snapshot.unlink()
            generated=staging.native_engine((ROOT/'vendor/wifi-relay/daemon/create_ap').read_bytes()).decode()
            functions='save_recovery_state() {'+generated.split('save_recovery_state() {',1)[1].split('HAVEGED_WATCHDOG_PID=',1)[0]
            start=generated.index('"$RUNTIME_HELPER" --create-ap-runtime radio-check "$CONFDIR" || die\n')
            end=generated.index('\nmutex_lock\necho $WIFI_IFACE',start)
            shim=b.root/'fixture-runtime'
            shim.write_text('#!/bin/bash\nshift\nexec '+shlex.quote(str(BINARY))+' --test-ap-runtime '+shlex.quote(str(b.root))+' "$@"\n');shim.chmod(0o700)
            assignments='\n'.join(k+'='+shlex.quote(v) for k,v in b.fields.items())
            script='set -e\nRUNTIME_HELPER='+shlex.quote(str(shim))+'\n'+assignments+'\nIP_ADDRS=()\nROUTE_ADDRS=()\n'+functions+'\ndie() { exit 1; }\nsave_recovery_state\n'+generated[start:end]
            for action in ['device-release','interface-configure','interface-remove','device-restore','verify-cleanup']:
                script+='\n"$RUNTIME_HELPER" --create-ap-runtime '+action+' "$CONFDIR"\n'
            result=subprocess.run(['/bin/bash','-c',script],text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(journal(b)['restored'])
            self.assertEqual(read(b)['interfaces'],before['interfaces'])
            self.assertEqual(snapshot(b)['fields']['WIFI_IFACE'],journal(b)['iface'])

    def test_renamed_created_interface_is_retained_for_review(self):
        import shutil
        with bench() as b:
            prepare(b);b.invoke('interface-create');name=iface(b);s=read(b)
            s['interfaces']['renamed0']=s['interfaces'].pop(name)
            s['phys']['renamed0']=s['phys'].pop(name)
            s['forwarding']['renamed0']=s['forwarding'].pop(name)
            write(b,s);shutil.rmtree(b.root/'net'/name)
            identity=s['interfaces']['renamed0']['identity'];b.interface('renamed0',identity['ifindex'],identity['mac'])
            b.invoke('interface-remove',ok=False);b.invoke('device-restore',ok=False)
            self.assertEqual(read(b),s)
            self.assertFalse(journal(b)['restored'])
