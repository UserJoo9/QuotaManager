import aiosqlite.core

# Ensure all aiosqlite worker threads are daemon threads so unclosed connections
# in tests never block Python process shutdown on exit.
_orig_connection_init = aiosqlite.core.Connection.__init__

def _daemon_connection_init(self, *args, **kwargs):
    _orig_connection_init(self, *args, **kwargs)
    self._thread.daemon = True

aiosqlite.core.Connection.__init__ = _daemon_connection_init
