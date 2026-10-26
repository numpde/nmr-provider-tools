"""Hold the real entry transaction without replacing the store's ownership machinery."""

import threading


class EntryWriteBarrier:
    def __init__(self, original):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.original = original
        self.calls = 0
        self.failure = None

    def write(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.reached.set()
            if not self.release.wait(3):
                raise TimeoutError("Test entry persistence was not released")
            if self.failure is not None:
                raise self.failure
        return self.original(*args, **kwargs)
