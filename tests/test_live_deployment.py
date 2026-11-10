"""Live handover via a private D-Bus manager and real Rust demo processes."""
import contextlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import unittest
from test_deployment import image,SERVICE,STATE,SECRET,staging
from test_dbus import BINARY,ROOT,connect,call,decode_reply,wait_for,GLib,Gio,running

LIVE=STATE+'/live.json'
GATE='/io/github/relayforge/Deployment';INTERFACE='io.github.relayforge.Deployment'
def invoke(c,action,payload=None,ok=True,mode='--test-deploy-live',root=None):
    args=[str(BINARY),mode,action,'--root',str(root or c.root)]
    if payload is not None:args+=['--payload',str(payload)]
    result=subprocess.run(args,text=True,capture_output=True,timeout=40)
    if ok and result.returncode:raise AssertionError(result.stderr)
    if not ok and not result.returncode:raise AssertionError('Unexpected live deployment success')
    if SECRET in result.stdout+result.stderr:raise AssertionError('Credential leaked')
    return result
def control(c,**changes):
    target=c.path/'manager-control.json'
    target.write_text(json.dumps(changes))
def events(c):
    try:return json.loads((c.path/'manager-events.json').read_text())
    except FileNotFoundError:return []
def maintenance(bus):
    return bus.call_sync('io.github.erhanzeyrek.WifiHotspot',GATE,INTERFACE,'GetMaintenance',None,None,Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
@contextlib.contextmanager
def manager(c):
    with subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/fake_systemd.py'),str(c.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as process:
        bus=connect()
        try:
            readable,_,_=select.select([process.stdout],[],[],5)
            if not readable or process.stdout.readline()!=b'ready\n':raise AssertionError('Manager fixture not ready')
            if (c.root/SERVICE).exists():
                def ready():
                    try:call(bus,'GetStatus');return True
                    except GLib.Error:return False
                wait_for(ready,seconds=5)
            yield bus
        finally:
            bus.close_sync(None)
            if process.poll() is None:process.terminate()
            _,stderr=process.communicate(timeout=8)
            if b'Traceback' in stderr:raise AssertionError(stderr.decode())

class LiveDeploymentTests(unittest.TestCase):
    def test_fresh_install_refuses_inactive_units_that_outrank_runtime_masks(self):
        for name in ['relayforge-create-ap.service','wifi-hotspot-daemon.service']:
            with self.subTest(unit=name),image() as c,manager(c):
                override=c.root/'etc/systemd/system'/name
                override.parent.mkdir(parents=True);override.write_text('administrator unit retained\n')
                before=c.files();invoke(c,'install',c.payload,ok=False)
                self.assertEqual(c.files(),before);self.assertFalse(events(c))
                self.assertFalse((c.root/STATE).exists());self.assertEqual(override.read_text(),'administrator unit retained\n')

    def test_target_without_maintenance_protocol_is_rejected_before_activation(self):
        with image() as c,manager(c):
            old=c.path/'old-binary';old.write_bytes(b'\x7fELF\x02\x01\x01'+b'\0'*100);old.chmod(0o755)
            payload=c.path/'old-payload';staging.stage(old,payload,'create_ap')
            invoke(c,'install',payload,ok=False)
            self.assertFalse((c.root/STATE).exists());self.assertFalse(events(c))
            self.assertEqual(subprocess.check_output([str(BINARY),'--deployment-protocol'],text=True).strip(),'relayforge:live-maintenance:v1')

    def test_ready_recovery_restarts_an_exited_idle_replacement(self):
        with image() as c,manager(c) as bus:
            c.fault('live-ready');invoke(c,'install',c.payload,ok=False)
            pid=bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetConnectionUnixProcessID',GLib.Variant('(s)',('io.github.erhanzeyrek.WifiHotspot',)),None,Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
            os.kill(pid,signal.SIGTERM)
            def stopped():
                return not bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','NameHasOwner',GLib.Variant('(s)',('io.github.erhanzeyrek.WifiHotspot',)),None,Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
            wait_for(stopped)
            c.fault(None);invoke(c,'recover');self.assertFalse(maintenance(bus));c.preserved(self)

    def test_ready_receipt_deletion_is_not_silently_adopted(self):
        with image() as c,manager(c):
            c.fault('live-ready');invoke(c,'install',c.payload,ok=False)
            (c.root/STATE/'state.json').unlink();before=c.files();count=len(events(c))
            c.fault(None);invoke(c,'recover',ok=False)
            self.assertEqual(c.files(),before);self.assertEqual(len(events(c)),count)

    def test_incomplete_pre_install_snapshot_requires_offline_rollback(self):
        with image() as c:
            shutil.copytree(c.payload/'usr',c.root/'usr',dirs_exist_ok=True)
            (c.root/'usr/lib/relayforge/native/create_ap').unlink()
            c.invoke('install',c.payload)
            with manager(c):
                before=c.files();invoke(c,'rollback',ok=False)
                self.assertFalse((c.root/LIVE).exists());self.assertFalse(events(c))
                self.assertEqual(c.files(),before)

    def test_live_install_upgrade_rollback_and_empty_baseline_preserve_desktop(self):
        for backend in ['create_ap','networkmanager']:
            with self.subTest(backend=backend),image(backend) as c,manager(c) as bus:
                baseline=c.files();plan=invoke(c,'plan',c.payload)
                self.assertEqual(json.loads(plan.stdout)['sharing'],'never restarted')
                self.assertEqual(c.files(),baseline);self.assertFalse((c.root/STATE).exists())
                invoke(c,'install',c.payload);v1=c.files()
                self.assertFalse(maintenance(bus));self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
                invoke(c,'install',c.stage('v2'));v2=c.files();self.assertNotEqual(v1[SERVICE],v2[SERVICE])
                invoke(c,'rollback');self.assertEqual(c.files(),v1)
                invoke(c,'rollback');self.assertEqual(c.files(),v2)
                self.assertFalse((c.root/LIVE).exists());c.preserved(self)
                # No enable/disable operation and no managed unit outside the fixed allowlist.
                self.assertTrue(all(name in ['MaskUnitFiles','UnmaskUnitFiles','Reload','StartUnit','StopUnit'] for name,_ in events(c)))
        with image() as c,manager(c):
            baseline=c.files();invoke(c,'install',c.payload);invoke(c,'rollback')
            self.assertEqual(c.files(),baseline)
            invoke(c,'rollback');self.assertIsNotNone(c.files()[SERVICE]);c.preserved(self)

    def test_every_handover_phase_recovers_with_idle_replacement(self):
        for phase in ['live-journal','live-masked','live-stop-intent','live-stopped','file-0','file-7','commit','live-files','live-starting','live-started','live-ready']:
            with self.subTest(phase=phase),image() as c,manager(c) as bus:
                c.fault(phase);invoke(c,'install',c.payload,ok=False)
                self.assertTrue((c.root/LIVE).exists())
                c.invoke('recover',ok=False)
                c.fault(None);invoke(c,'recover')
                self.assertFalse((c.root/LIVE).exists());self.assertFalse(maintenance(bus))
                self.assertEqual((c.root/SERVICE).read_bytes(),(c.payload/SERVICE).read_bytes())
                invoke(c,'recover');c.preserved(self)

    def test_pending_upgrade_abort_direction_survives_second_crash(self):
        for phase in ['live-journal','live-files','live-started']:
            with self.subTest(phase=phase),image() as c,manager(c) as bus:
                invoke(c,'install',c.payload);before=c.files()
                c.fault(phase);invoke(c,'install',c.stage('v2'),ok=False)
                c.fault('live-abort-journal');invoke(c,'rollback',ok=False)
                c.fault(None);invoke(c,'recover');self.assertEqual(c.files(),before)
                self.assertFalse(maintenance(bus))
                invoke(c,'rollback');self.assertTrue(all(v is None for v in c.files().values()));c.preserved(self)

    def test_maintenance_blocks_desktop_start_and_settings_but_keeps_reads(self):
        with image() as c:
            c.invoke('install',c.payload)
            with manager(c) as bus:
                c.fault('live-masked');invoke(c,'install',c.stage('v2'),ok=False)
                self.assertTrue(maintenance(bus));self.assertFalse(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
                with self.assertRaises(GLib.Error):call(bus,'Start')
                with self.assertRaises(GLib.Error):call(bus,'SetConfig',GLib.Variant('(s)',(json.dumps({'SSID':'rejected'}),)))
                self.assertEqual(c.config.read_bytes(),c.original_config)
                c.fault(None);invoke(c,'recover');self.assertFalse(maintenance(bus));c.preserved(self)

    def test_active_sharing_blocks_handover_before_service_mutation(self):
        with image() as c:
            c.invoke('install',c.payload)
            with manager(c) as bus:
                self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
                before=c.files();invoke(c,'install',c.stage('v2'),ok=False)
                self.assertEqual(c.files(),before);self.assertFalse((c.root/LIVE).exists())
                self.assertFalse(events(c));self.assertTrue(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
                call(bus,'Stop');c.preserved(self)

    def test_readiness_failure_allows_explicit_rollback(self):
        with image() as c,manager(c) as bus:
            invoke(c,'install',c.payload);before=c.files()
            control(c,unhealthy=True);invoke(c,'install',c.stage('v2'),ok=False)
            self.assertTrue((c.root/LIVE).exists())
            control(c);invoke(c,'rollback');self.assertEqual(c.files(),before)
            self.assertFalse(maintenance(bus));c.preserved(self)

    def test_lost_stop_or_start_reply_can_recover(self):
        for method in ['StopUnit','StartUnit']:
            with self.subTest(method=method),image() as c,manager(c) as bus:
                invoke(c,'install',c.payload)
                control(c,lost_reply=method);invoke(c,'install',c.stage('v2'),ok=False)
                control(c);invoke(c,'recover');self.assertFalse(maintenance(bus));c.preserved(self)

    def test_interrupted_jobs_timeout_without_mutation_then_recover(self):
        for method in ['StartUnit', 'StopUnit']:
            with self.subTest(method=method), image() as c, manager(c) as bus:
                invoke(c, 'install', c.payload)
                control(c, hold_job=method, lost_reply=method)
                invoke(c, 'install', c.stage('v2'), ok=False)
                control(c, hold_job=method)
                before = c.files()
                before_events = events(c)
                result = invoke(c, 'recover', ok=False)
                self.assertIn('Service jobs did not settle; handover retained', result.stderr)
                self.assertEqual(c.files(), before)
                self.assertEqual(events(c), before_events)
                self.assertTrue((c.root/LIVE).exists())
                control(c)
                invoke(c, 'recover')
                self.assertFalse((c.root/LIVE).exists())
                self.assertFalse(maintenance(bus))
                c.preserved(self)

    def test_stop_intent_recovery_refuses_unrecognized_processes_and_jobs(self):
        for condition, error in [
                ('bad_pid', 'Unrecognized native service generation'),
                ('unexpected_job', 'Unrecognized service job')]:
            with self.subTest(condition=condition), image() as c, manager(c) as bus:
                invoke(c, 'install', c.payload)
                c.fault('live-stop-intent')
                invoke(c, 'install', c.stage('v2'), ok=False)
                c.fault(None)
                before = c.files()
                before_events = events(c)
                control(c, **{condition: True})
                result = invoke(c, 'recover', ok=False)
                self.assertIn(error, result.stderr)
                self.assertEqual(c.files(), before)
                self.assertEqual(events(c), before_events)
                self.assertTrue((c.root/LIVE).exists())
                control(c)
                invoke(c, 'recover')
                self.assertFalse(maintenance(bus))
                c.preserved(self)

    def test_masks_jobs_overrides_and_network_evidence_fail_before_mutation(self):
        for condition in ['mask','job','override','network']:
            with self.subTest(condition=condition),image() as c:
                c.invoke('install',c.payload)
                with manager(c):
                    if condition=='mask':control(c,unit_states={'relayforge.service':'masked'})
                    elif condition=='job':control(c,job=True)
                    elif condition=='override':control(c,overrides=['/etc/systemd/system/relayforge-create-ap.service.d/custom.conf'])
                    else:(c.root/'run/relayforge/pending.json').write_text('retained')
                    before=c.files();invoke(c,'install',c.stage('v2'),ok=False)
                    self.assertEqual(c.files(),before);self.assertFalse((c.root/LIVE).exists());self.assertFalse(events(c));c.preserved(self)

    def test_foreign_owner_and_fixture_live_root_are_rejected(self):
        with image() as c,manager(c),running('--demo'):
            invoke(c,'install',c.payload,ok=False)
            self.assertFalse((c.root/LIVE).exists());self.assertFalse(events(c))
            invoke(c,'install',c.payload,root='/',ok=False)
            invoke(c,'install',c.payload,mode='--deploy-live',ok=False)

    def test_changed_bus_manager_requires_review_without_recovery_mutation(self):
        with image() as c:
            with manager(c):
                c.fault('live-journal');invoke(c,'install',c.payload,ok=False)
            c.fault(None)
            with manager(c):
                before=c.files();invoke(c,'recover',ok=False);self.assertEqual(c.files(),before)
                self.assertEqual(json.loads(invoke(c,'status').stdout)['phase'],'prepared')

    def test_ready_recovery_does_not_stop_a_new_desktop_request(self):
        with image() as c,manager(c) as bus:
            c.fault('live-ready');invoke(c,'install',c.payload,ok=False)
            # Simulate a successful gate release whose reply/receipt deletion was lost.
            bus.call_sync('io.github.erhanzeyrek.WifiHotspot',GATE,INTERFACE,'SetMaintenance',GLib.Variant('(b)',(False,)),None,Gio.DBusCallFlags.NONE,5000,None)
            self.assertTrue(decode_reply('Start',call(bus,'Start'))['success'])
            count=len([v for v in events(c) if v[0]=='StopUnit'])
            c.fault(None);invoke(c,'recover')
            self.assertTrue(decode_reply('GetStatus',call(bus,'GetStatus'))['active'])
            self.assertEqual(len([v for v in events(c) if v[0]=='StopUnit']),count)
            call(bus,'Stop');c.preserved(self)
