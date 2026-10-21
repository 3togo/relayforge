"""Exercise the real compatibility pipe adapter with a hardware-free engine.

The pinned GLib operation scheduler is reused; no networking subprocess runs.
"""
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

ENGINE = '''
from pathlib import Path
import json, sys, time
sys.path.insert(0, {operations!r})
from operations import Operations
from gi.repository import GLib

class WifiHotspotDaemon:
    def __init__(self, connection):
        self._operations = Operations()
    def _dispatch_method_call(self, method, params, invocation):
        def work():
            if method == 'Start':
                self._emit_signal('StatusChanged', json.dumps({{'active':False,'state':'connecting'}}))
                for _ in range(100):
                    if self._operations.cancelled.is_set():
                        return json.dumps({{'success':False}})
                    time.sleep(.01)
                return json.dumps({{'success':True}})
            if method == 'GetConfig':
                return json.dumps({{'SSID':'fixture'}})
            return True
        def finished(value, error):
            if error:
                invocation.return_error_literal('failed')
            else:
                invocation.return_value(GLib.Variant('(s)' if isinstance(value,str) else '(b)', (value,)))
        self._operations.submit(work, finished, urgent=method=='Stop', cancel=method=='Stop')
    def method_stop(self):
        self._emit_signal('StatusChanged',json.dumps({{'active':False,'state':'idle'}}))
        Path(__file__).with_suffix('.trace').write_text('stopped')
        return True
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='relayforge-worker-')
        root = Path(self.directory.name)
        daemon = root / 'daemon'
        daemon.mkdir()
        self.trace = daemon / 'wifi-hotspot-daemon.trace'
        (daemon / 'wifi-hotspot-daemon.py').write_text(ENGINE.format(operations=str(ROOT / 'vendor/wifi-relay/daemon')))
        self.process = subprocess.Popen(['/usr/bin/python3', '-I', str(ROOT / 'compat/worker.py'), str(root)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        bufsize=0, env={**os.environ, 'DBUS_SYSTEM_BUS_ADDRESS': os.environ['DBUS_SESSION_BUS_ADDRESS']})
        self.buffer = b''
        self.assertEqual(self.frame(), {'ready': 1})

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()
        self.directory.cleanup()

    def frame(self):
        deadline = time.monotonic() + 5
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise AssertionError('Worker frame timed out')
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise AssertionError('Unexpected EOF: ' + self.process.stderr.read().decode())
                self.buffer += chunk
            line, self.buffer = self.buffer.split(b'\n', 1)
            return json.loads(line)

    def send(self, request):
        self.process.stdin.write(json.dumps(request).encode() + b'\n')
        self.process.stdin.flush()

    def test_private_rpc_preserves_wire_values(self):
        self.send({'id': 1, 'method': 'GetConfig', 'params': None})
        self.assertEqual(self.frame(), {'id': 1, 'result': {'SSID': 'fixture'}})

    def test_stop_interrupts_pending_start(self):
        self.send({'id': 1, 'method': 'Start'})
        self.assertEqual(self.frame()['signal'], 'StatusChanged')
        self.send({'id': 2, 'method': 'Stop'})
        self.assertEqual(self.frame(), {'id': 1, 'result': {'success': False}})
        self.assertEqual(self.frame(), {'id': 2, 'result': True})

    def test_parent_eof_cancels_and_cleans_up(self):
        self.send({'id': 1, 'method': 'Start'})
        self.assertEqual(self.frame()['signal'], 'StatusChanged')
        self.process.stdin.close()
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertEqual(self.trace.read_text(), 'stopped')

    def test_invalid_input_is_not_echoed_and_triggers_cleanup(self):
        self.process.stdin.write(b'{"secret":"must-never-appear-in-diagnostics"}\n')
        self.process.stdin.flush()
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertNotIn(b'must-never-appear', self.process.stderr.read())
        self.assertEqual(self.trace.read_text(), 'stopped')

    def test_closed_parent_output_does_not_interrupt_cleanup(self):
        self.send({'id': 1, 'method': 'Start'})
        self.assertEqual(self.frame()['signal'], 'StatusChanged')
        self.process.stdout.close()
        self.process.stdin.close()
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertEqual(self.trace.read_text(), 'stopped')
