#!/usr/bin/python3
"""Private pipe adapter for pinned Wi-Fi Relay networking; never owns a bus name.

The Rust parent authorizes all requests. EOF and signals cancel work and stop the
owned backend. stdout is exclusively bounded JSON protocol, diagnostics use stderr.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading

output = sys.stdout
sys.stdout = sys.stderr
write_lock = threading.Lock()
output_alive = True
LIMIT = 1024 * 1024


def emit(value):
    global output_alive
    encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    if len(encoded.encode()) + 1 > LIMIT:
        raise RuntimeError('Worker output exceeds protocol limit')
    with write_lock:
        if not output_alive:
            return
        try:
            output.write(encoded + '\n')
            output.flush()
        except (BrokenPipeError, OSError):
            # Parent death must never interrupt backend resource cleanup.
            output_alive = False
            sink = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(sink, output.fileno())
            finally:
                os.close(sink)


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    sys.path.insert(0, str(root / 'daemon'))
    spec = importlib.util.spec_from_file_location('relay_bridge_daemon', root / 'daemon/wifi-hotspot-daemon.py')
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)
    from gi.repository import GLib, Gio
    # Require the existing protected config, without development/user fallbacks.
    daemon.get_config_path = lambda for_write=False: '/etc/wifi-hotspot.conf'
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    engine = daemon.WifiHotspotDaemon(connection)
    loop = GLib.MainLoop()
    closing = False
    cleanup_failed = False

    def event(name, data):
        value = json.loads(data)
        if name == 'StatusChanged':
            engine._status_snapshot = value
        emit({'signal': name, 'data': value})

    engine._emit_signal = event

    def shutdown(*_):
        nonlocal closing
        if not closing:
            closing = True
            engine._operations.cancelled.set()
            def stopped(value, error):
                nonlocal cleanup_failed
                if error or value is not True:
                    cleanup_failed = True
                    print('Backend cleanup failed during worker shutdown.', file=sys.stderr)
                engine._operations.close()
                loop.quit()
            def enqueue_stop():
                if engine._operations.submit(engine.method_stop, stopped, urgent=True, cancel=True):
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE
            if enqueue_stop() == GLib.SOURCE_CONTINUE:
                GLib.timeout_add(100, enqueue_stop)
        return GLib.SOURCE_REMOVE

    def dispatch(request):
        if closing:
            return GLib.SOURCE_REMOVE
        request_id = request['id']
        method = request['method']

        class Invocation:
            def return_value(self, variant):
                value = variant.unpack()[0]
                if isinstance(value, str):
                    value = json.loads(value)
                emit({'id': request_id, 'result': value})

            def return_error_literal(self, *_):
                emit({'id': request_id, 'error': 'Backend operation rejected'})

        params = request.get('params')
        if method == 'GetStatus':
            def status_finished(value, error):
                if error:
                    Invocation().return_error_literal()
                else:
                    emit({'id': request_id, 'result': value})
            if not engine._operations.submit(engine._get_status_dict, status_finished, defer=True):
                Invocation().return_error_literal()
            return GLib.SOURCE_REMOVE
        if method == 'SwitchBandAndReconnect':
            parameters = GLib.Variant('(s)', (params,))
        else:
            parameters = GLib.Variant('()', ())
        engine._dispatch_method_call(method, parameters, Invocation())
        return GLib.SOURCE_REMOVE

    def read():
        try:
            allowed = {'Start', 'Stop', 'GetStatus', 'GetClients',
                       'SwitchBandAndReconnect', 'PrepareFirewall'}
            while True:
                line = sys.stdin.buffer.readline(LIMIT + 1)
                if not line:
                    break
                if len(line) > LIMIT or not line.endswith(b'\n'):
                    raise ValueError('Invalid worker frame')
                request = json.loads(line)
                if (not isinstance(request, dict) or type(request.get('id')) is not int
                        or request['id'] <= 0 or request.get('method') not in allowed):
                    raise ValueError('Invalid worker request')
                GLib.idle_add(dispatch, request)
        except Exception:
            print('Worker input failed; stopping owned networking.', file=sys.stderr)
        finally:
            GLib.idle_add(shutdown)

    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(shutdown))
    signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(shutdown))
    emit({'ready': 1})
    threading.Thread(target=read, daemon=True).start()
    loop.run()
    return 1 if cleanup_failed else 0


if __name__ == '__main__':
    sys.exit(main())
