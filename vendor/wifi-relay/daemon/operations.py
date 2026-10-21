"""Serialize backend work outside the D-Bus main context, with urgent cancellation."""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import threading

from gi.repository import GLib


class Operations:
    def __init__(self):
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wifi-relay')
        self.busy = False
        self.closed = False
        self.pending = deque()
        self.deferred = deque()

    def submit(self, work, finished, *, urgent=False, cancel=False, defer=False):
        """Main-context API. Urgent Stop/sleep work runs after cancellation unwinds."""
        if (self.closed or (self.busy and not urgent and not defer)
                or len(self.pending) + len(self.deferred) >= 32):
            return False
        if cancel:
            self.cancelled.set()
        if self.busy:
            (self.pending if urgent else self.deferred).append((work, finished))
        else:
            if not cancel:
                self.cancelled.clear()
            self._launch(work, finished)
        return True

    def _launch(self, work, finished):
        self.busy = True
        future = self.executor.submit(work)
        future.add_done_callback(lambda result: GLib.idle_add(self._finish, result, finished))

    def _finish(self, future, finished):
        try:
            value, error = future.result(), None
        except Exception as exc:
            value, error = None, exc
        # Keep busy while delivering replies: reentrant callbacks must not start
        # new work ahead of an already queued Stop or sleep notification.
        try:
            finished(value, error)
        finally:
            if self.pending:
                self._launch(*self.pending.popleft())
            elif self.deferred:
                self._launch(*self.deferred.popleft())
            else:
                self.busy = False
        return GLib.SOURCE_REMOVE

    def close(self):
        self.closed = True
        self.cancelled.set()
        for queue in (self.pending, self.deferred):
            while queue:
                _work, finished = queue.popleft()
                finished(None, RuntimeError('Hotspot service is shutting down.'))
        self.executor.shutdown(wait=False)
